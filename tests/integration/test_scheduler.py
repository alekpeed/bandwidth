"""Scheduler behaviour.

``systemctl`` is replaced with a recording stub so these tests assert the
exact commands the scheduler issues, without needing a systemd user instance
(there is none in a container, and a real one would leave units behind).

What is being checked is the specification's scheduling rules: enabling
schedules from now, changing the interval reschedules from now, disabling
cancels future runs without interrupting a running test, and a machine that
was off never produces a burst of catch-up tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_run

from bandwidth_logger.core.models import RunStatus, TriggerType
from bandwidth_logger.core.scheduler import (
    SERVICE_UNIT,
    TIMER_UNIT,
    Scheduler,
    SchedulerError,
)
from bandwidth_logger.storage.database import (
    SETTING_AUTO_ENABLED,
    SETTING_INTERVAL_MINUTES,
    SETTING_RUN_AFTER_LOGIN,
)

pytestmark = pytest.mark.integration


class FakeSystemctl:
    """Records ``systemctl`` invocations and answers ``show`` queries."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.active = False
        self.unit_file_enabled = False
        self.next_elapse_usec: int | None = None
        self.next_elapse_monotonic_usec: str | int | None = None
        #: What ``list-timers --json=short`` returns. None means the command
        #: is unsupported, mirroring an older systemd.
        self.list_timers_json: str | None = None
        #: Plain ``list-timers --timestamp=unix`` output. None means the
        #: option is unsupported.
        self.list_timers_unix: str | None = None
        self.available = True

    def install(self, monkeypatch) -> None:
        stub = self

        def _systemctl(_scheduler_self, *arguments, check=True):  # noqa: ANN001
            return stub._dispatch(*arguments, check=check)

        monkeypatch.setattr(Scheduler, "systemctl_path", staticmethod(lambda: "/bin/systemctl"))
        monkeypatch.setattr(Scheduler, "_systemctl", _systemctl)

    def _dispatch(self, *arguments, check=True):  # noqa: ANN001
        import subprocess

        self.calls.append(tuple(arguments))
        stdout = ""
        returncode = 0

        if not self.available:
            if check:
                raise SchedulerError("systemctl is not available on this system.")
            return None

        if "list-timers" in arguments and "--timestamp=unix" in arguments:
            if self.list_timers_unix is None:
                return subprocess.CompletedProcess(
                    args=list(arguments), returncode=1, stdout="",
                    stderr="systemctl: unrecognized option '--timestamp=unix'",
                )
            return subprocess.CompletedProcess(
                args=list(arguments), returncode=0, stdout=self.list_timers_unix, stderr=""
            )

        if "list-timers" in arguments:
            if self.list_timers_json is None:
                return subprocess.CompletedProcess(
                    args=list(arguments), returncode=1, stdout="", stderr="unknown option"
                )
            return subprocess.CompletedProcess(
                args=list(arguments), returncode=0, stdout=self.list_timers_json, stderr=""
            )

        if "is-system-running" in arguments:
            stdout = "running\n"
        elif "show" in arguments:
            stdout = (
                f"ActiveState={'active' if self.active else 'inactive'}\n"
                "LoadState=loaded\n"
                f"UnitFileState={'enabled' if self.unit_file_enabled else 'disabled'}\n"
                "Result=success\n"
                f"NextElapseUSecRealtime={self.next_elapse_usec or 0}\n"
                f"NextElapseUSecMonotonic={self.next_elapse_monotonic_usec or 0}\n"
                "LastTriggerUSec=0\n"
            )
        elif "is-enabled" in arguments:
            stdout = "enabled\n" if self.unit_file_enabled else "disabled\n"
            returncode = 0 if self.unit_file_enabled else 1
        elif "restart" in arguments or "start" in arguments:
            self.active = True
        elif "stop" in arguments:
            self.active = False
        elif "enable" in arguments:
            self.unit_file_enabled = True
        elif "disable" in arguments:
            self.unit_file_enabled = False

        return subprocess.CompletedProcess(args=list(arguments), returncode=returncode, stdout=stdout, stderr="")

    def commands(self) -> list[str]:
        """Flattened call list, for readable assertions."""
        return [" ".join(call) for call in self.calls]


@pytest.fixture()
def systemctl(monkeypatch, database):
    stub = FakeSystemctl()
    stub.install(monkeypatch)
    return stub


@pytest.fixture()
def scheduler(database, systemctl):
    return Scheduler(database)


class TestEnabling:
    def test_enabling_writes_the_units_and_starts_the_timer(self, scheduler, systemctl, isolated_home):
        status = scheduler.apply(enabled=True, interval_minutes=30, run_after_login=True)

        unit_dir = isolated_home / ".config/systemd/user"
        assert (unit_dir / SERVICE_UNIT).exists()
        assert (unit_dir / TIMER_UNIT).exists()

        commands = systemctl.commands()
        assert "--user daemon-reload" in commands
        assert f"--user enable {TIMER_UNIT}" in commands
        assert f"--user restart {TIMER_UNIT}" in commands
        assert status.enabled is True
        assert status.timer_active is True

    def test_the_timer_schedules_the_first_run_one_interval_from_now(self, scheduler, isolated_home):
        scheduler.apply(enabled=True, interval_minutes=30, run_after_login=True)

        timer = (isolated_home / ".config/systemd/user" / TIMER_UNIT).read_text()
        # OnActiveSec is the countdown from the moment the timer starts.
        assert "OnActiveSec=30min" in timer
        assert "OnUnitActiveSec=30min" in timer

    def test_the_timer_never_replays_missed_runs(self, scheduler, isolated_home):
        scheduler.apply(enabled=True, interval_minutes=30, run_after_login=True)

        timer = (isolated_home / ".config/systemd/user" / TIMER_UNIT).read_text()
        assert "Persistent=false" in timer

    def test_the_units_are_user_units_in_the_home_directory(self, scheduler, systemctl, isolated_home):
        scheduler.apply(enabled=True, interval_minutes=30, run_after_login=True)

        unit_dir = isolated_home / ".config/systemd/user"
        assert (unit_dir / TIMER_UNIT).exists()

        # Nothing is ever done system-wide: every call is --user scoped, and
        # no unit is written outside the user's home directory.
        assert systemctl.calls, "the scheduler issued no systemctl commands"
        for call in systemctl.calls:
            assert call[0] == "--user", f"{call} is not scoped to the user instance"
        assert str(unit_dir).startswith(str(isolated_home))

    def test_the_service_runs_the_headless_entry_point(self, scheduler, isolated_home):
        scheduler.apply(enabled=True, interval_minutes=15, run_after_login=True)

        service = (isolated_home / ".config/systemd/user" / SERVICE_UNIT).read_text()
        assert "--scheduled" in service
        assert "Type=oneshot" in service

    def test_settings_are_persisted(self, scheduler, database):
        scheduler.apply(enabled=True, interval_minutes=120, run_after_login=False)

        assert database.get_bool(SETTING_AUTO_ENABLED) is True
        assert database.get_int(SETTING_INTERVAL_MINUTES) == 120
        assert database.get_bool(SETTING_RUN_AFTER_LOGIN) is False

    def test_an_invalid_interval_is_refused_before_anything_changes(self, scheduler, database):
        with pytest.raises(ValueError):
            scheduler.apply(enabled=True, interval_minutes=2, run_after_login=True)

        assert database.get_bool(SETTING_AUTO_ENABLED) is False


class TestIntervalChanges:
    def test_changing_the_interval_rewrites_the_unit_and_restarts_the_timer(
        self, scheduler, systemctl, isolated_home
    ):
        scheduler.apply(enabled=True, interval_minutes=30, run_after_login=True)
        systemctl.calls.clear()

        scheduler.apply(enabled=True, interval_minutes=120, run_after_login=True)

        timer = (isolated_home / ".config/systemd/user" / TIMER_UNIT).read_text()
        assert "OnUnitActiveSec=120min" in timer
        assert "OnActiveSec=120min" in timer
        assert "30min" not in timer

        commands = systemctl.commands()
        # daemon-reload picks up the new text; restart makes it take effect now.
        assert "--user daemon-reload" in commands
        assert f"--user restart {TIMER_UNIT}" in commands

    def test_the_new_interval_is_reflected_in_the_status(self, scheduler):
        scheduler.apply(enabled=True, interval_minutes=30, run_after_login=True)
        status = scheduler.apply(enabled=True, interval_minutes=240, run_after_login=True)

        assert status.interval_minutes == 240


class TestDisabling:
    def test_disabling_stops_and_disables_the_timer(self, scheduler, systemctl):
        scheduler.apply(enabled=True, interval_minutes=30, run_after_login=True)
        systemctl.calls.clear()

        status = scheduler.apply(enabled=False, interval_minutes=30, run_after_login=True)

        commands = systemctl.commands()
        assert f"--user stop {TIMER_UNIT}" in commands
        assert f"--user disable {TIMER_UNIT}" in commands
        assert status.enabled is False
        assert status.timer_active is False

    def test_disabling_does_not_touch_a_running_test(self, scheduler, systemctl):
        """Only the timer is stopped, never the service.

        Stopping bandwidth-logger-test.service would kill a test that is
        already in flight; the specification requires it to finish.
        """
        scheduler.apply(enabled=True, interval_minutes=30, run_after_login=True)
        systemctl.calls.clear()

        scheduler.apply(enabled=False, interval_minutes=30, run_after_login=True)

        for command in systemctl.commands():
            assert SERVICE_UNIT not in command

    def test_a_disabled_scheduler_reports_no_next_run(self, scheduler):
        scheduler.apply(enabled=False, interval_minutes=30, run_after_login=True)
        status = scheduler.status()

        assert status.next_run_display() == "Not scheduled"
        assert status.healthy is True


class TestStartAtLogin:
    def test_enabling_start_at_login_enables_the_unit_file(self, scheduler, systemctl):
        scheduler.apply(enabled=True, interval_minutes=30, run_after_login=True)
        assert f"--user enable {TIMER_UNIT}" in systemctl.commands()
        assert systemctl.unit_file_enabled is True

    def test_declining_start_at_login_leaves_the_unit_file_disabled(self, scheduler, systemctl):
        scheduler.apply(enabled=True, interval_minutes=30, run_after_login=False)
        assert f"--user disable {TIMER_UNIT}" in systemctl.commands()
        assert systemctl.unit_file_enabled is False


class TestStatusReporting:
    def test_status_reflects_the_real_timer_not_the_saved_switch(self, scheduler, systemctl, database):
        scheduler.apply(enabled=True, interval_minutes=30, run_after_login=True)

        # The timer dies behind the application's back.
        systemctl.active = False
        status = scheduler.status()

        assert database.get_bool(SETTING_AUTO_ENABLED) is True
        assert status.enabled is True
        assert status.timer_active is False
        assert status.healthy is False
        assert "Scheduler error" in status.next_run_display() or status.next_run_display().startswith("Not scheduled")
        assert status.detail, "an unhealthy scheduler must offer a detail message"

    def test_the_next_run_time_comes_from_systemd(self, scheduler, systemctl):
        moment = datetime.now(timezone.utc) + timedelta(minutes=30)
        systemctl.next_elapse_usec = int(moment.timestamp() * 1_000_000)

        scheduler.apply(enabled=True, interval_minutes=30, run_after_login=True)
        status = scheduler.status()

        assert status.next_run_utc is not None
        assert abs((status.next_run_utc - moment).total_seconds()) < 2
        # Shown as an exact local date and time.
        assert status.next_run_display() == moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    def test_systemd_absence_is_reported_plainly(self, scheduler, systemctl, database):
        database.set_bool(SETTING_AUTO_ENABLED, True)
        systemctl.available = False

        status = scheduler.status()

        assert status.supported is False
        assert status.healthy is False
        assert "cannot schedule" in status.detail
        assert "Test Now" in status.detail

    def test_reapply_from_settings_restores_the_stored_schedule(self, scheduler, database, isolated_home):
        database.set_bool(SETTING_AUTO_ENABLED, True)
        database.set_int(SETTING_INTERVAL_MINUTES, 45)
        database.set_bool(SETTING_RUN_AFTER_LOGIN, True)

        # A unit file lost to an upgrade or a cleaned home directory.
        status = scheduler.reapply_from_settings()

        assert (isolated_home / ".config/systemd/user" / TIMER_UNIT).exists()
        assert status.interval_minutes == 45
        assert status.timer_active is True


class TestMissedRuns:
    def test_a_long_gap_is_recorded_once_as_an_interruption(self, scheduler, database):
        database.set_bool(SETTING_AUTO_ENABLED, True)
        database.set_int(SETTING_INTERVAL_MINUTES, 30)

        old = datetime.now(timezone.utc) - timedelta(hours=8)
        database.insert_run(make_run(moment=old))

        recorded = scheduler.record_missed_window()

        assert recorded is not None
        assert recorded.trigger_type == TriggerType.STARTUP_RECOVERY
        assert recorded.status == RunStatus.SKIPPED
        assert "not replayed" in recorded.error_message
        assert database.count_runs() == 2

    def test_the_same_gap_is_not_reported_twice(self, scheduler, database):
        database.set_bool(SETTING_AUTO_ENABLED, True)
        database.set_int(SETTING_INTERVAL_MINUTES, 30)
        database.insert_run(make_run(moment=datetime.now(timezone.utc) - timedelta(hours=8)))

        assert scheduler.record_missed_window() is not None
        assert scheduler.record_missed_window() is None
        assert database.count_runs() == 2

    def test_no_burst_of_catch_up_tests_is_created(self, scheduler, database):
        """A three-day gap at a 30-minute interval would be 144 missed runs.

        Exactly one note is written and no test is triggered by it.
        """
        database.set_bool(SETTING_AUTO_ENABLED, True)
        database.set_int(SETTING_INTERVAL_MINUTES, 30)
        database.insert_run(make_run(moment=datetime.now(timezone.utc) - timedelta(days=3)))

        scheduler.record_missed_window()

        assert database.count_runs() == 2

    def test_an_ordinary_gap_is_not_reported(self, scheduler, database):
        database.set_bool(SETTING_AUTO_ENABLED, True)
        database.set_int(SETTING_INTERVAL_MINUTES, 30)
        database.insert_run(make_run(moment=datetime.now(timezone.utc) - timedelta(minutes=31)))

        assert scheduler.record_missed_window() is None
        assert database.count_runs() == 1

    def test_nothing_is_recorded_when_automatic_testing_is_off(self, scheduler, database):
        database.set_bool(SETTING_AUTO_ENABLED, False)
        database.insert_run(make_run(moment=datetime.now(timezone.utc) - timedelta(days=2)))

        assert scheduler.record_missed_window() is None
        assert database.count_runs() == 1

    def test_nothing_is_recorded_on_a_brand_new_database(self, scheduler, database):
        database.set_bool(SETTING_AUTO_ENABLED, True)
        assert scheduler.record_missed_window() is None
        assert database.count_runs() == 0


class TestNextRunFromAMonotonicTimer:
    """Reading when the timer next fires.

    The timer is defined with OnActiveSec/OnUnitActiveSec, which are
    *monotonic*. systemd reports those in NextElapseUSecMonotonic and leaves
    NextElapseUSecRealtime at 0. Reading only the realtime property made a
    perfectly healthy, actively counting timer report "Not scheduled" -- the
    schedule worked, the window said it did not.
    """

    def test_a_monotonic_next_elapse_is_converted_to_wall_clock(
        self, scheduler, systemctl, database
    ):
        import time as time_module

        from bandwidth_logger.storage.database import SETTING_AUTO_ENABLED

        database.set_bool(SETTING_AUTO_ENABLED, True)
        systemctl.active = True
        # Five minutes from now, expressed the way systemd would.
        systemctl.next_elapse_monotonic_usec = int((time_module.monotonic() + 300) * 1_000_000)
        systemctl.next_elapse_usec = 0

        status = scheduler.status()

        assert status.next_run_utc is not None, "a monotonic timer must still report a next run"
        seconds_away = (status.next_run_utc - datetime.now(timezone.utc)).total_seconds()
        assert 290 < seconds_away < 310
        assert status.next_run_display() != "Not scheduled"

    def test_a_realtime_next_elapse_still_wins_when_present(self, scheduler, systemctl, database):
        from bandwidth_logger.storage.database import SETTING_AUTO_ENABLED

        database.set_bool(SETTING_AUTO_ENABLED, True)
        systemctl.active = True
        moment = datetime.now(timezone.utc) + timedelta(minutes=30)
        systemctl.next_elapse_usec = int(moment.timestamp() * 1_000_000)
        systemctl.next_elapse_monotonic_usec = 0

        status = scheduler.status()

        assert abs((status.next_run_utc - moment).total_seconds()) < 2

    def test_neither_property_set_means_nothing_scheduled(self, scheduler, systemctl, database):
        from bandwidth_logger.storage.database import SETTING_AUTO_ENABLED

        database.set_bool(SETTING_AUTO_ENABLED, True)
        systemctl.active = True
        systemctl.next_elapse_usec = 0
        systemctl.next_elapse_monotonic_usec = 0

        assert scheduler.status().next_run_utc is None


class TestNextRunFromListTimers:
    """Where the next run time actually comes from.

    Observed on systemd 255, with the timer active and counting down
    correctly::

        ActiveState=active
        NextElapseUSecMonotonic=infinity
        LastTriggerUSec=Tue 2026-08-04 09:51:17 EDT

    The monotonic property reads ``infinity`` for an OnUnitActiveSec timer,
    and the timestamps are pretty-printed rather than raw microseconds --
    while ``list-timers`` reported the next run correctly to the second. So
    list-timers is asked first, and the show properties are only a fallback.
    """

    def _enable(self, database, systemctl):
        from bandwidth_logger.storage.database import SETTING_AUTO_ENABLED

        database.set_bool(SETTING_AUTO_ENABLED, True)
        systemctl.active = True

    def test_the_next_run_comes_from_list_timers(self, scheduler, systemctl, database):
        import json

        self._enable(database, systemctl)
        moment = datetime.now(timezone.utc) + timedelta(minutes=5)
        systemctl.list_timers_json = json.dumps(
            [
                {
                    "unit": TIMER_UNIT,
                    "next": int(moment.timestamp() * 1_000_000),
                    "activates": SERVICE_UNIT,
                }
            ]
        )
        # The properties are useless, exactly as systemd 255 reports them.
        systemctl.next_elapse_usec = 0
        systemctl.next_elapse_monotonic_usec = "infinity"

        status = scheduler.status()

        assert status.next_run_utc is not None
        assert abs((status.next_run_utc - moment).total_seconds()) < 2

    def test_infinity_in_the_properties_does_not_crash_or_mislead(
        self, scheduler, systemctl, database
    ):
        """``int("infinity")`` raises; it must be read as 'no time', not blow up."""
        self._enable(database, systemctl)
        systemctl.list_timers_json = "[]"
        systemctl.next_elapse_monotonic_usec = "infinity"
        systemctl.next_elapse_usec = 0

        status = scheduler.status()

        assert status.next_run_utc is None

    def test_an_active_timer_with_no_readable_time_is_not_called_unscheduled(
        self, scheduler, systemctl, database
    ):
        """The timer is running. Saying "Not scheduled" would be a falsehood."""
        self._enable(database, systemctl)
        systemctl.list_timers_json = "[]"
        systemctl.next_elapse_monotonic_usec = "infinity"
        systemctl.next_elapse_usec = 0

        display = scheduler.status().next_run_display()

        assert display == "Scheduled (next run time unavailable)"
        assert "Not scheduled" not in display

    def test_malformed_json_falls_back_rather_than_failing(self, scheduler, systemctl, database):
        self._enable(database, systemctl)
        systemctl.list_timers_json = "this is not json"
        moment = datetime.now(timezone.utc) + timedelta(minutes=12)
        systemctl.next_elapse_usec = int(moment.timestamp() * 1_000_000)

        status = scheduler.status()

        assert abs((status.next_run_utc - moment).total_seconds()) < 2

    def test_an_older_systemd_without_json_still_works(self, scheduler, systemctl, database):
        """list-timers --json fails; the show properties answer instead."""
        self._enable(database, systemctl)
        systemctl.list_timers_json = None  # command unsupported
        moment = datetime.now(timezone.utc) + timedelta(minutes=8)
        systemctl.next_elapse_usec = int(moment.timestamp() * 1_000_000)

        status = scheduler.status()

        assert abs((status.next_run_utc - moment).total_seconds()) < 2


class TestEstimatedNextRun:
    """Working the next run out from the recorded history.

    systemd's reporting of the next elapse proved unreliable across versions:
    a timer counting down perfectly reported "infinity" through `show`, and
    `list-timers --json` is not available everywhere either. Since
    OnUnitActiveSec fires one interval after the service last ran, and the
    application already records exactly when that was, the answer can be
    derived from data it owns -- and is labelled as an estimate.
    """

    def _enable(self, database, systemctl, minutes=5):
        from bandwidth_logger.storage.database import (
            SETTING_AUTO_ENABLED,
            SETTING_INTERVAL_MINUTES,
        )

        database.set_bool(SETTING_AUTO_ENABLED, True)
        database.set_int(SETTING_INTERVAL_MINUTES, minutes)
        systemctl.active = True
        systemctl.list_timers_json = "[]"
        systemctl.next_elapse_usec = 0
        systemctl.next_elapse_monotonic_usec = "infinity"

    def test_the_next_run_is_derived_from_the_last_one(self, scheduler, systemctl, database):
        self._enable(database, systemctl, minutes=5)
        last = datetime.now(timezone.utc) - timedelta(minutes=2)
        database.insert_run(make_run(moment=last))

        status = scheduler.status()

        assert status.next_run_utc is not None
        assert status.next_run_estimated is True
        assert abs((status.next_run_utc - (last + timedelta(minutes=5))).total_seconds()) < 2

    def test_an_estimate_is_labelled_as_one(self, scheduler, systemctl, database):
        """Presenting a calculation as systemd's own figure would repeat the
        overconfidence that produced "Not scheduled" for a running timer.
        """
        self._enable(database, systemctl, minutes=5)
        database.insert_run(make_run(moment=datetime.now(timezone.utc) - timedelta(minutes=1)))

        assert scheduler.status().next_run_display().startswith("about ")

    def test_systemd_is_preferred_over_the_estimate(self, scheduler, systemctl, database):
        import json

        self._enable(database, systemctl, minutes=5)
        database.insert_run(make_run(moment=datetime.now(timezone.utc) - timedelta(minutes=2)))
        reported = datetime.now(timezone.utc) + timedelta(minutes=4)
        systemctl.list_timers_json = json.dumps(
            [{"unit": TIMER_UNIT, "next": int(reported.timestamp() * 1_000_000)}]
        )

        status = scheduler.status()

        assert status.next_run_estimated is False
        assert not status.next_run_display().startswith("about ")
        assert abs((status.next_run_utc - reported).total_seconds()) < 2

    def test_a_stale_history_produces_no_estimate(self, scheduler, systemctl, database):
        """If the expected time is long past, the schedule was interrupted and
        the history is the wrong basis for a prediction.
        """
        self._enable(database, systemctl, minutes=5)
        database.insert_run(make_run(moment=datetime.now(timezone.utc) - timedelta(hours=3)))

        status = scheduler.status()

        assert status.next_run_utc is None
        assert status.next_run_display() == "Scheduled (next run time unavailable)"

    def test_an_empty_history_produces_no_estimate(self, scheduler, systemctl, database):
        self._enable(database, systemctl, minutes=5)

        assert scheduler.status().next_run_utc is None

    def test_nothing_is_estimated_while_the_timer_is_inactive(self, scheduler, systemctl, database):
        self._enable(database, systemctl, minutes=5)
        systemctl.active = False
        database.insert_run(make_run(moment=datetime.now(timezone.utc) - timedelta(minutes=1)))

        status = scheduler.status()

        assert status.next_run_utc is None
        assert status.next_run_display() == "Not scheduled (scheduler error)"


class TestNextRunFromUnixTimestamps:
    """``list-timers --timestamp=unix``.

    systemd 255 rejects ``--json=short`` for list-timers outright
    ("unrecognized option"), so the plain listing is asked for with unix
    timestamps -- immune to locale, and to the pretty-printing that made the
    show properties unusable.
    """

    def _enable(self, database, systemctl):
        from bandwidth_logger.storage.database import SETTING_AUTO_ENABLED

        database.set_bool(SETTING_AUTO_ENABLED, True)
        systemctl.active = True
        systemctl.next_elapse_usec = 0
        systemctl.next_elapse_monotonic_usec = "infinity"

    def test_the_next_column_is_read_as_the_next_run(self, scheduler, systemctl, database):
        self._enable(database, systemctl)
        moment = datetime.now(timezone.utc) + timedelta(minutes=5)
        last = datetime.now(timezone.utc) - timedelta(seconds=30)
        systemctl.list_timers_unix = (
            f"@{int(moment.timestamp())} 4min 58s @{int(last.timestamp())} 30s ago "
            f"{TIMER_UNIT} {SERVICE_UNIT}\n"
        )

        status = scheduler.status()

        assert status.next_run_estimated is False
        assert abs((status.next_run_utc - moment).total_seconds()) < 2

    def test_an_unsupported_option_falls_through_quietly(self, scheduler, systemctl, database):
        """Exactly what systemd 255 does. It must not become an error."""
        self._enable(database, systemctl)
        systemctl.list_timers_unix = None
        systemctl.list_timers_json = None
        database.insert_run(make_run(moment=datetime.now(timezone.utc) - timedelta(minutes=1)))

        status = scheduler.status()

        # Falls all the way through to the computed estimate.
        assert status.next_run_utc is not None
        assert status.next_run_estimated is True

    def test_a_row_with_no_next_time_yields_nothing(self, scheduler, systemctl, database):
        self._enable(database, systemctl)
        systemctl.list_timers_unix = f"- - - - {TIMER_UNIT} {SERVICE_UNIT}\n"
        systemctl.list_timers_json = "[]"

        assert scheduler.status().next_run_utc is None

    def test_systemd_is_preferred_over_the_estimate(self, scheduler, systemctl, database):
        self._enable(database, systemctl)
        database.insert_run(make_run(moment=datetime.now(timezone.utc) - timedelta(minutes=2)))
        moment = datetime.now(timezone.utc) + timedelta(minutes=3)
        systemctl.list_timers_unix = f"@{int(moment.timestamp())} 3min {TIMER_UNIT}\n"

        status = scheduler.status()

        assert status.next_run_estimated is False
        assert not status.next_run_display().startswith("about ")
