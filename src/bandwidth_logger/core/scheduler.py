"""Background scheduling through a systemd **user** timer.

Why systemd rather than a timer inside the application: the schedule has to
survive closing the window and has to come back after a reboot, and the
window must be able to report the scheduler's *real* state rather than
whatever the switch was last set to. A user timer gives all three, and
``systemctl --user`` answers questions about the next run and about failures
truthfully. The units are user-level -- nothing here installs a root service
or needs a password.

Two units are managed, both written to ``~/.config/systemd/user``:

``bandwidth-logger-test.service``
    A oneshot service that runs a single headless test.

``bandwidth-logger-test.timer``
    Triggers that service one interval after it was started, and one interval
    after each run. ``Persistent=false`` is deliberate: a machine that was
    asleep for two days must come back and run *one* test, not two days'
    worth of catch-up tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..storage.database import (
    SETTING_AUTO_ENABLED,
    SETTING_INTERVAL_MINUTES,
    SETTING_RUN_AFTER_LOGIN,
    Database,
)
from ..system import paths
from ..system.logging_setup import get_logger
from .models import (
    ErrorCategory,
    RunStatus,
    TestRun,
    TriggerType,
    parse_iso,
    to_local_iso,
    to_utc_iso,
    utc_now,
)

log = get_logger("scheduler")

SERVICE_UNIT = "bandwidth-logger-test.service"
TIMER_UNIT = "bandwidth-logger-test.timer"

MINIMUM_INTERVAL_MINUTES = 5
MAXIMUM_INTERVAL_MINUTES = 7 * 24 * 60

#: Presets offered in the interval selector, in minutes.
INTERVAL_PRESETS: tuple[tuple[int, str], ...] = (
    (5, "5 minutes"),
    (10, "10 minutes"),
    (15, "15 minutes"),
    (30, "30 minutes"),
    (60, "1 hour"),
    (120, "2 hours"),
    (240, "4 hours"),
    (360, "6 hours"),
    (720, "12 hours"),
    (1440, "24 hours"),
)

#: How far past the expected time a run has to be before the gap is recorded
#: as a delay. One whole extra interval plus a minute of slack, so ordinary
#: timer jitter is never reported as a missed run.
MISSED_RUN_SLACK = timedelta(minutes=1)

SYSTEMCTL_TIMEOUT = 20


class SchedulerError(RuntimeError):
    """The scheduler could not be inspected or changed."""


def validate_interval(minutes: int | str) -> int:
    """Check a proposed interval and return it as whole minutes.

    The five-minute floor exists to stop an accidental interval from
    hammering the connection and the test servers.
    """
    try:
        value = int(str(minutes).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("The interval must be a whole number of minutes.") from exc

    if value < MINIMUM_INTERVAL_MINUTES:
        raise ValueError(
            f"The shortest permitted interval is {MINIMUM_INTERVAL_MINUTES} minutes."
        )
    if value > MAXIMUM_INTERVAL_MINUTES:
        raise ValueError("The longest permitted interval is 7 days (10080 minutes).")
    return value


def describe_interval(minutes: int) -> str:
    """Wording used wherever an interval is shown."""
    for preset_minutes, label in INTERVAL_PRESETS:
        if preset_minutes == minutes:
            return label
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day{'s' if days != 1 else ''}"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{minutes} minutes"


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    """The scheduler's actual state, as reported by systemd.

    ``enabled`` is what the user asked for; ``timer_active`` is what is
    really happening. When they disagree the window shows "Scheduler error"
    and offers the detail rather than quietly claiming everything is fine.
    """

    supported: bool
    enabled: bool
    timer_active: bool
    timer_enabled_at_login: bool
    interval_minutes: int
    next_run_utc: datetime | None
    last_trigger_utc: datetime | None
    last_result: str | None
    detail: str = ""
    #: True when the next run was worked out from the recorded history rather
    #: than reported by systemd, so the display can say "about".
    next_run_estimated: bool = False

    @property
    def healthy(self) -> bool:
        if not self.enabled:
            return True
        return self.supported and self.timer_active

    def next_run_display(self) -> str:
        """Exact local date and time of the next test, or a plain reason."""
        if not self.enabled:
            return "Not scheduled"
        if not self.supported:
            return "Not scheduled (background scheduling unavailable)"
        if not self.timer_active:
            return "Not scheduled (scheduler error)"
        if self.next_run_utc is None:
            # The timer is active and counting down; only the time could not
            # be established. Saying "Not scheduled" here would be a plain
            # falsehood about work that is really queued.
            return "Scheduled (next run time unavailable)"

        shown = self.next_run_utc.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        # An estimate is labelled as one. Presenting a calculated time as if
        # systemd had reported it would be the same kind of overconfidence
        # that produced "Not scheduled" for a running timer.
        return f"about {shown}" if self.next_run_estimated else shown


class Scheduler:
    """Creates, updates and inspects the systemd user timer."""

    def __init__(self, database: Database, *, unit_dir: Path | None = None) -> None:
        self.database = database
        self.unit_dir = unit_dir or paths.systemd_user_unit_dir()

    # -- environment -------------------------------------------------------

    @staticmethod
    def systemctl_path() -> str | None:
        return shutil.which("systemctl")

    def is_supported(self) -> bool:
        """True when a user systemd instance is reachable.

        False inside containers and on systems without systemd; the
        application still runs manual tests there and says plainly that
        background scheduling is unavailable.
        """
        if self.systemctl_path() is None:
            return False
        completed = self._systemctl("--user", "is-system-running", check=False)
        if completed is None:
            return False
        # "degraded", "starting" and even "offline" still accept unit
        # commands; only a total failure to reach the user manager does not.
        return completed.returncode == 0 or bool(completed.stdout.strip())

    def _systemctl(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str] | None:
        binary = self.systemctl_path()
        if binary is None:
            if check:
                raise SchedulerError("systemctl is not available on this system.")
            return None
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv
                [binary, *arguments],
                capture_output=True,
                text=True,
                timeout=SYSTEMCTL_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if check:
                raise SchedulerError(f"Could not run systemctl: {exc}") from exc
            return None
        if check and completed.returncode != 0:
            message = (completed.stderr or completed.stdout).strip()
            raise SchedulerError(message or f"systemctl {' '.join(arguments)} failed.")
        return completed

    # -- unit files --------------------------------------------------------

    @staticmethod
    def runner_command() -> list[str]:
        """Absolute command the timer runs.

        Prefers the installed console script; falls back to the running
        interpreter so the scheduler also works from a source checkout.
        """
        script = shutil.which("bandwidth-logger-run")
        if script:
            return [script, "--scheduled"]
        return [os.path.realpath(sys.executable), "-m", "bandwidth_logger.cli", "--scheduled"]

    def _environment_lines(self) -> list[str]:
        """Pass through the data-directory override when one is in force."""
        override = os.environ.get("BANDWIDTH_LOGGER_HOME")
        return [f'Environment="BANDWIDTH_LOGGER_HOME={override}"'] if override else []

    def service_unit_text(self) -> str:
        command = " ".join(self.runner_command())
        environment = "\n".join(self._environment_lines())
        environment_block = f"{environment}\n" if environment else ""
        return f"""[Unit]
Description=Bandwidth Logger scheduled speed test
Documentation=man:bandwidth-logger(1)

[Service]
Type=oneshot
{environment_block}ExecStart={command}
# A failed test is an ordinary recorded outcome, not a unit failure: the
# runner stores the failure and exits 0 so one bad test never stops the timer.
"""

    def timer_unit_text(self, interval_minutes: int) -> str:
        return f"""[Unit]
Description=Bandwidth Logger automatic speed test every {describe_interval(interval_minutes)}

[Timer]
Unit={SERVICE_UNIT}
# First run one interval after the timer starts, then one interval after
# each completed run.
OnActiveSec={interval_minutes}min
OnUnitActiveSec={interval_minutes}min
AccuracySec=30s
# Never replay missed runs: a machine that was asleep resumes with a single
# test on the normal schedule instead of a burst of catch-up tests.
Persistent=false

[Install]
WantedBy=timers.target
"""

    def write_units(self, interval_minutes: int) -> None:
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        (self.unit_dir / SERVICE_UNIT).write_text(self.service_unit_text(), encoding="utf-8")
        (self.unit_dir / TIMER_UNIT).write_text(
            self.timer_unit_text(interval_minutes), encoding="utf-8"
        )
        self._systemctl("--user", "daemon-reload")

    def remove_units(self) -> None:
        for unit in (TIMER_UNIT, SERVICE_UNIT):
            path = self.unit_dir / unit
            if path.exists():
                path.unlink()
        self._systemctl("--user", "daemon-reload", check=False)

    # -- control -----------------------------------------------------------

    def apply(self, *, enabled: bool, interval_minutes: int, run_after_login: bool) -> SchedulerStatus:
        """Make the running timer match the requested settings.

        Called on every change, so enabling, changing the interval and
        toggling start-at-login all take one predictable path. Restarting the
        timer restarts the countdown from now, which is what "the new
        interval takes effect immediately" has to mean.
        """
        interval_minutes = validate_interval(interval_minutes)

        self.database.set_bool(SETTING_AUTO_ENABLED, enabled)
        self.database.set_int(SETTING_INTERVAL_MINUTES, interval_minutes)
        self.database.set_bool(SETTING_RUN_AFTER_LOGIN, run_after_login)

        if not self.is_supported():
            log.warning("systemd user instance unavailable; automatic testing cannot be scheduled")
            return self.status()

        if not enabled:
            # Stopping the timer cancels future runs. It deliberately does not
            # touch bandwidth-logger-test.service, so a test already in flight
            # finishes and is recorded.
            self._systemctl("--user", "stop", TIMER_UNIT, check=False)
            self._systemctl("--user", "disable", TIMER_UNIT, check=False)
            log.info("Automatic testing disabled")
            return self.status()

        self.write_units(interval_minutes)
        if run_after_login:
            self._systemctl("--user", "enable", TIMER_UNIT)
        else:
            self._systemctl("--user", "disable", TIMER_UNIT, check=False)
        self._systemctl("--user", "restart", TIMER_UNIT)
        log.info(
            "Automatic testing enabled every %s (start at login: %s)",
            describe_interval(interval_minutes),
            run_after_login,
        )
        return self.status()

    def reapply_from_settings(self) -> SchedulerStatus:
        """Re-assert the stored settings against systemd.

        Run at start-up so an upgrade that changed the unit text, or a unit
        file lost from the home directory, is repaired without the user
        having to touch the switch.
        """
        return self.apply(
            enabled=self.database.get_bool(SETTING_AUTO_ENABLED),
            interval_minutes=self.database.get_int(
                SETTING_INTERVAL_MINUTES, 30
            ),
            run_after_login=self.database.get_bool(SETTING_RUN_AFTER_LOGIN),
        )

    # -- inspection --------------------------------------------------------

    def status(self) -> SchedulerStatus:
        """Ask systemd what is actually scheduled."""
        enabled = self.database.get_bool(SETTING_AUTO_ENABLED)
        interval = self.database.get_int(SETTING_INTERVAL_MINUTES, 30)

        if not self.is_supported():
            return SchedulerStatus(
                supported=False,
                enabled=enabled,
                timer_active=False,
                timer_enabled_at_login=False,
                interval_minutes=interval,
                next_run_utc=None,
                last_trigger_utc=None,
                last_result=None,
                detail=(
                    "This system has no reachable systemd user service manager, so "
                    "Bandwidth Logger cannot schedule tests in the background. "
                    "Tests started with Test Now still work."
                ),
            )

        properties = self._timer_properties()
        active_state = properties.get("ActiveState", "")
        timer_active = active_state in {"active", "activating"}

        enabled_state = self._systemctl("--user", "is-enabled", TIMER_UNIT, check=False)
        timer_enabled_at_login = bool(
            enabled_state and enabled_state.stdout.strip() in {"enabled", "enabled-runtime"}
        )

        detail = ""
        if enabled and not timer_active:
            detail = self._failure_detail(properties)

        next_run = self._next_elapse(properties)
        estimated = False
        if next_run is None and timer_active:
            next_run = self._estimated_next_run(interval)
            estimated = next_run is not None

        return SchedulerStatus(
            supported=True,
            enabled=enabled,
            timer_active=timer_active,
            timer_enabled_at_login=timer_enabled_at_login,
            interval_minutes=interval,
            next_run_utc=next_run,
            next_run_estimated=estimated,
            last_trigger_utc=_usec_to_datetime(properties.get("LastTriggerUSec")),
            last_result=properties.get("Result") or None,
            detail=detail,
        )

    def _next_elapse(self, properties: dict[str, str]) -> datetime | None:
        """When the timer next fires.

        ``systemctl show`` turns out to be the wrong source. Observed on
        systemd 255 with this timer active and counting down correctly:

            NextElapseUSecMonotonic=infinity
            LastTriggerUSec=Tue 2026-08-04 09:51:17 EDT

        -- the monotonic property reads ``infinity`` for an
        ``OnUnitActiveSec`` timer whose next firing is computed from the
        service's last activation, and the timestamp properties are
        pretty-printed rather than given as raw microseconds. Meanwhile
        ``list-timers`` reported the correct next run to the second.

        So ``list-timers --json`` is asked first, since that is the code path
        systemd itself uses to answer this question. The ``show`` properties
        remain as a fallback for older versions that report raw microseconds.
        """
        from_list = self._next_elapse_from_list_timers()
        if from_list is not None:
            return from_list
        return _next_elapse_from_properties(properties)

    def _estimated_next_run(self, interval_minutes: int) -> datetime | None:
        """Work out the next run from the recorded history.

        ``OnUnitActiveSec`` fires one interval after the service last ran, and
        the application already records exactly when that was -- so the next
        run can be derived without asking systemd at all.

        This exists because systemd's own reporting of the next elapse varies
        by version in ways that proved unreliable: a timer counting down
        perfectly reported ``infinity`` through one interface and the correct
        time through another. Rather than depend on that, the answer is
        computed from data the application owns, and labelled as an estimate
        so it is never mistaken for systemd's own figure.
        """
        latest = self.database.latest_run()
        if latest is None:
            return None

        started = parse_iso(latest.started_at_utc)
        if started is None:
            return None

        expected = started + timedelta(minutes=interval_minutes)
        # A stale estimate is worse than none: if the expected time is well
        # past, the schedule was interrupted and the history is the wrong
        # basis for a prediction.
        if expected < utc_now() - timedelta(minutes=interval_minutes):
            return None
        return expected

    def _next_elapse_from_list_timers(self) -> datetime | None:
        completed = self._systemctl(
            "--user", "list-timers", TIMER_UNIT, "--all", "--json=short", check=False
        )
        if completed is None or completed.returncode != 0:
            return None

        try:
            entries = json.loads(completed.stdout or "[]")
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return None

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # systemd has used both spellings across versions.
            for key in ("next", "NextElapseUSecRealtime", "next_elapse"):
                moment = _usec_to_datetime(entry.get(key))
                if moment is not None:
                    return moment
        return None

    def _timer_properties(self) -> dict[str, str]:
        completed = self._systemctl(
            "--user",
            "show",
            TIMER_UNIT,
            "--property=ActiveState",
            "--property=LoadState",
            "--property=UnitFileState",
            "--property=Result",
            "--property=NextElapseUSecRealtime",
            "--property=NextElapseUSecMonotonic",
            "--property=LastTriggerUSec",
            check=False,
        )
        if completed is None:
            return {}
        properties: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key.strip()] = value.strip()
        return properties

    def _failure_detail(self, properties: dict[str, str]) -> str:
        """Text behind the Details button when the timer is not running."""
        lines = [
            "Automatic testing is switched on, but the background timer is not running.",
            "",
            f"Timer unit: {TIMER_UNIT}",
            f"Load state: {properties.get('LoadState', 'unknown')}",
            f"Active state: {properties.get('ActiveState', 'unknown')}",
            f"Unit file state: {properties.get('UnitFileState', 'unknown')}",
            f"Last result: {properties.get('Result', 'unknown')}",
        ]
        listing = self._systemctl(
            "--user", "status", TIMER_UNIT, "--no-pager", "--lines=20", check=False
        )
        if listing is not None and listing.stdout.strip():
            lines.extend(["", "systemctl --user status output:", "", listing.stdout.strip()])
        return "\n".join(lines)

    # -- missed runs -------------------------------------------------------

    def expected_gap(self, interval_minutes: int) -> timedelta:
        """How long a gap may be before it counts as a delayed run."""
        return timedelta(minutes=interval_minutes * 2) + MISSED_RUN_SLACK

    def record_missed_window(self, *, now: datetime | None = None) -> TestRun | None:
        """Note that automatic testing was interrupted, if it was.

        Called once when a scheduled run starts and once when the window
        opens. It writes at most one record per gap and never triggers extra
        tests, which is what keeps a suspended laptop from producing a burst
        of catch-up runs on wake.
        """
        if not self.database.get_bool(SETTING_AUTO_ENABLED):
            return None

        interval = self.database.get_int(SETTING_INTERVAL_MINUTES, 30)
        moment = now or utc_now()
        latest = self.database.latest_run()
        if latest is None:
            return None

        last_attempt = parse_iso(latest.started_at_utc)
        if last_attempt is None:
            return None

        gap = moment - last_attempt
        if gap <= self.expected_gap(interval):
            return None

        # Do not report the same gap twice.
        if (
            latest.trigger_type == TriggerType.STARTUP_RECOVERY
            and latest.status == RunStatus.SKIPPED
        ):
            return None

        run = TestRun(
            trigger_type=TriggerType.STARTUP_RECOVERY,
            started_at_utc=to_utc_iso(moment),
            started_at_local=to_local_iso(moment),
            completed_at_utc=to_utc_iso(moment),
            completed_at_local=to_local_iso(moment),
            duration_ms=0,
            status=RunStatus.SKIPPED,
            error_category=ErrorCategory.UNKNOWN,
            error_message=(
                "Automatic testing was interrupted. No test ran between "
                f"{last_attempt.astimezone():%Y-%m-%d %H:%M:%S} and "
                f"{moment.astimezone():%Y-%m-%d %H:%M:%S} "
                f"(a gap of {_describe_gap(gap)}), most likely because the computer was "
                "switched off, suspended, or logged out. Testing resumes on the normal "
                f"schedule of one test every {describe_interval(interval)}; missed tests "
                "are not replayed."
            ),
            engine_name="none",
            application_version=latest.application_version,
            created_at_utc=to_utc_iso(moment),
        )
        self.database.insert_run(run)
        log.info("Recorded interrupted scheduling window of %s", _describe_gap(gap))
        return run


def _next_elapse_from_properties(properties: dict[str, str]) -> datetime | None:
    """Fallback for systemd versions that report raw microseconds.

    systemd reports the next elapse in whichever clock the timer is defined
    against. This timer uses ``OnActiveSec``/``OnUnitActiveSec``, which are
    **monotonic**, so ``NextElapseUSecRealtime`` is 0 and the answer lives in
    ``NextElapseUSecMonotonic`` -- measured from boot, not from the epoch.
    Reading only the realtime property made a perfectly healthy timer report
    "Not scheduled".

    Both are read, so a calendar-based timer would still work if one is ever
    introduced.
    """
    realtime = _usec_to_datetime(properties.get("NextElapseUSecRealtime"))
    if realtime is not None:
        return realtime

    monotonic_usec = properties.get("NextElapseUSecMonotonic")
    if not monotonic_usec:
        return None
    try:
        target = int(monotonic_usec) / 1_000_000
    except ValueError:
        return None
    if target <= 0:
        return None

    # CLOCK_MONOTONIC is what both systemd and time.monotonic() use on Linux,
    # so the difference converts straight into wall-clock time.
    seconds_away = target - time.monotonic()
    return utc_now() + timedelta(seconds=seconds_away)


def _usec_to_datetime(value: object) -> datetime | None:
    """Convert a systemd microseconds-since-epoch value to a datetime.

    "No such time" is reported as 0, as the unsigned 64-bit maximum, or as
    the literal string ``infinity`` -- all of which mean nothing is
    scheduled. Newer systemd also pretty-prints timestamps as dates, which
    are locale-dependent and deliberately not parsed here; the JSON path
    above supplies the numeric answer instead.
    """
    if value is None or value == "":
        return None
    try:
        microseconds = int(value)
    except (TypeError, ValueError):
        return None
    if microseconds <= 0 or microseconds >= 2**64 - 1:
        return None
    try:
        return datetime.fromtimestamp(microseconds / 1_000_000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _describe_gap(gap: timedelta) -> str:
    total_minutes = int(gap.total_seconds() // 60)
    if total_minutes < 60:
        return f"{total_minutes} minutes"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"{hours} hours {minutes} minutes"
    days, hours = divmod(hours, 24)
    return f"{days} days {hours} hours"
