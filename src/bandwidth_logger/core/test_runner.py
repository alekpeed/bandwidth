"""Execution of one speed test, from trigger to stored record.

This module is the only place a test is ever run. The window and the
scheduler both call :meth:`TestRunner.run_test`, so a manual test and a
scheduled test are the same code path with a different ``trigger_type`` --
there is no second implementation that could drift.

Two guarantees drive the design:

*Every attempt is recorded.* Success, failure, timeout, cancellation, a
missing engine, an overlap -- each one produces a row. The only thing that
can prevent a row is the database being unwritable, and that is logged and
raised rather than swallowed.

*Only one test runs at a time.* The window and the scheduler are separate
processes, so an in-process lock would not be enough; the lock is an advisory
lock on a file, which the kernel releases even if a process is killed.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from .. import APPLICATION_VERSION
from ..engines import SpeedTestEngine
from ..engines.base import EngineError, classify_error_text
from ..engines.registry import AUTOMATIC, select_engine
from ..storage.database import (
    SETTING_ENGINE_NAME,
    SETTING_SERVER_ID,
    SETTING_TIMEOUT_SECONDS,
    Database,
    DatabaseError,
)
from ..system import paths
from ..system.logging_setup import get_logger
from ..system.network_info import describe_connection
from .models import (
    ErrorCategory,
    RunStatus,
    TestRun,
    TriggerType,
    to_local_iso,
    to_utc_iso,
    utc_now,
)

log = get_logger("test_runner")

DEFAULT_TIMEOUT_SECONDS = 180
#: Grace period between asking the engine to stop and forcing it to.
TERMINATE_GRACE_SECONDS = 5.0
LOCK_FILENAME = "test-runner.lock"


class TestAlreadyRunning(RuntimeError):
    """Another test holds the lock."""


@dataclass(slots=True)
class RunOutcome:
    """What a run attempt produced.

    ``run`` is always present. ``stored`` is False only when the database
    itself could not be written, in which case ``save_error`` explains why.
    """

    run: TestRun
    stored: bool = True
    save_error: str | None = None


def lock_path() -> str:
    return str(paths.state_dir() / LOCK_FILENAME)


@contextmanager
def _exclusive_lock() -> Iterator[bool]:
    """Try to take the cross-process run lock without waiting.

    Yields True when the lock was acquired. The advisory lock is held on an
    open file descriptor, so it is released by the kernel if this process
    dies -- a crashed test cannot wedge the scheduler permanently.
    """
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    handle = open(lock_path(), "a+")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - descriptor already gone
                pass
        handle.close()


def is_test_running() -> bool:
    """True when some process currently holds the run lock."""
    with _exclusive_lock() as acquired:
        return not acquired


class TestRunner:
    """Runs speed tests and records the result of every attempt."""

    #: Not a pytest test class, despite the name.
    __test__ = False

    def __init__(
        self,
        database: Database,
        *,
        engine: SpeedTestEngine | None = None,
        timeout_seconds: int | None = None,
        application_version: str = APPLICATION_VERSION,
    ) -> None:
        self.database = database
        self._forced_engine = engine
        self._forced_timeout = timeout_seconds
        self.application_version = application_version
        self._process: subprocess.Popen[str] | None = None
        self._cancelled = threading.Event()
        self._process_lock = threading.Lock()

    # -- configuration -----------------------------------------------------

    def resolve_engine(self) -> SpeedTestEngine | None:
        if self._forced_engine is not None:
            return self._forced_engine
        preference = self.database.get_setting(SETTING_ENGINE_NAME) or AUTOMATIC
        engine = select_engine(preference)
        if engine is not None:
            # A pinned server keeps a history comparable over time. Without
            # one the engine re-chooses by lowest latency on every run, and a
            # change in the recorded speed can mean nothing more than that a
            # different server answered.
            engine.server_id = (self.database.get_setting(SETTING_SERVER_ID) or "").strip() or None
        return engine

    def resolve_timeout(self) -> int:
        """Engine timeout in seconds.

        Deliberately an internal setting rather than a prominent control:
        clamped to a sane band so a stray value cannot hang the scheduler or
        cut off a slow but working connection.
        """
        if self._forced_timeout is not None:
            return int(self._forced_timeout)
        configured = self.database.get_int(SETTING_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS)
        return max(30, min(configured, 3600))

    # -- cancellation ------------------------------------------------------

    def cancel(self) -> bool:
        """Ask a running test to stop. Returns True if one was running."""
        with self._process_lock:
            process = self._process
            if process is None or process.poll() is not None:
                return False
            self._cancelled.set()
            self._stop_process(process)
            return True

    @property
    def is_running(self) -> bool:
        with self._process_lock:
            return self._process is not None and self._process.poll() is None

    # -- the run -----------------------------------------------------------

    def run_test(
        self,
        trigger_type: str = TriggerType.MANUAL,
        *,
        scheduled_at_utc: str | None = None,
        on_state: Callable[[str], None] | None = None,
    ) -> RunOutcome:
        """Attempt one speed test and store exactly one record.

        ``on_state`` receives short progress labels for the status area.
        """
        notify = on_state or (lambda _state: None)
        self._cancelled.clear()

        with _exclusive_lock() as acquired:
            if not acquired:
                # Overlap policy: skip and say so. A skipped attempt is
                # recorded rather than dropped, and no backlog can build up.
                log.info("Skipping %s test: another test is already running", trigger_type)
                return self._store(
                    self._skipped_run(trigger_type, scheduled_at_utc),
                    notify=notify,
                )

            notify("Testing")
            run = self._execute(trigger_type, scheduled_at_utc, notify)
            return self._store(run, notify=notify)

    # -- internals ---------------------------------------------------------

    def _execute(
        self,
        trigger_type: str,
        scheduled_at_utc: str | None,
        notify: Callable[[str], None],
    ) -> TestRun:
        started_at = utc_now()
        monotonic_start = time.monotonic()
        engine = self.resolve_engine()
        engine_name = engine.name if engine is not None else "none"

        run = TestRun(
            trigger_type=trigger_type,
            scheduled_at_utc=scheduled_at_utc,
            started_at_utc=to_utc_iso(started_at),
            started_at_local=to_local_iso(started_at),
            status=RunStatus.FAILED,
            engine_name=engine_name,
            application_version=self.application_version,
            created_at_utc=to_utc_iso(started_at),
        )

        interface, link_type = describe_connection()
        run.interface_name = interface
        run.connection_type = link_type

        if engine is None:
            self._finish(run, started_at, monotonic_start)
            run.status = RunStatus.FAILED
            run.error_category = ErrorCategory.ENGINE_MISSING
            run.error_message = (
                "No speed-test engine is installed. Install the Ookla Speedtest CLI "
                "or speedtest-cli, then run the test again."
            )
            return run

        try:
            command = engine.build_command()
        except EngineError as exc:
            self._finish(run, started_at, monotonic_start)
            run.status = RunStatus.FAILED
            run.error_category = exc.category
            run.error_message = exc.message
            return run

        timeout = self.resolve_timeout()
        log.info("Running %s test with %s (timeout %ds)", trigger_type, engine.name, timeout)

        try:
            exit_code, stdout, stderr, timed_out = self._spawn(command, timeout)
        except OSError as exc:
            self._finish(run, started_at, monotonic_start)
            run.status = RunStatus.FAILED
            run.error_category = (
                ErrorCategory.ENGINE_MISSING
                if exc.errno == errno.ENOENT
                else ErrorCategory.PROCESS_ERROR
            )
            run.error_message = f"Could not start the engine: {exc}"
            return run

        self._finish(run, started_at, monotonic_start)
        run.engine_version = engine.version()

        if self._cancelled.is_set():
            run.status = RunStatus.CANCELLED
            run.error_category = ErrorCategory.CANCELLED
            run.error_message = "The test was cancelled before it finished."
            return run

        if timed_out:
            run.status = RunStatus.TIMEOUT
            run.error_category = ErrorCategory.TIMEOUT
            run.error_message = (
                f"The engine did not finish within {timeout} seconds and was stopped."
                + (f"\n\nEngine output:\n{stderr.strip()}" if stderr.strip() else "")
            )
            return run

        if exit_code != 0:
            category, message = engine.classify_failure(exit_code, stdout, stderr)
            run.status = RunStatus.FAILED
            run.error_category = category
            run.error_message = f"The engine exited with status {exit_code}.\n\n{message}"
            run.raw_result_json = _raw_snapshot(stdout, stderr, exit_code)
            return run

        notify("Saving result")
        try:
            measurement = engine.parse_output(stdout)
        except EngineError as exc:
            run.status = RunStatus.FAILED
            run.error_category = exc.category
            run.error_message = exc.message
            run.raw_result_json = _raw_snapshot(stdout, stderr, exit_code)
            return run

        run.status = RunStatus.SUCCESS
        run.engine_name = measurement.engine_name
        run.engine_version = measurement.engine_version or run.engine_version
        run.download_bps = measurement.download_bps
        run.upload_bps = measurement.upload_bps
        run.idle_latency_ms = measurement.idle_latency_ms
        run.download_latency_ms = measurement.download_latency_ms
        run.upload_latency_ms = measurement.upload_latency_ms
        run.jitter_ms = measurement.jitter_ms
        run.packet_loss_percent = measurement.packet_loss_percent
        run.server_id = measurement.server_id
        run.server_name = measurement.server_name
        run.server_host = measurement.server_host
        run.server_city = measurement.server_city
        run.server_region = measurement.server_region
        run.server_country = measurement.server_country
        run.isp_name = measurement.isp_name
        run.external_ip = measurement.external_ip
        run.raw_result_json = measurement.raw_result_json
        # The engine's own view of the interface wins when it reports one.
        if measurement.interface_name:
            run.interface_name = measurement.interface_name
            run.connection_type = (
                link_type
                if measurement.interface_name == interface
                else _link_type_for(measurement.interface_name)
            )
        return run

    def _spawn(self, command: list[str], timeout: int) -> tuple[int, str, str, bool]:
        """Run the engine without a shell, enforcing the timeout.

        The engine gets its own session so that stopping it also stops any
        helper process it started, rather than leaving orphans holding the
        connection.
        """
        process = subprocess.Popen(  # noqa: S603 - fixed argv, never shell-interpolated
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with self._process_lock:
            self._process = process

        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            log.warning("Engine exceeded %ds; stopping it", timeout)
            self._stop_process(process)
            try:
                stdout, stderr = process.communicate(timeout=TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - kill already sent
                stdout, stderr = "", ""
        finally:
            with self._process_lock:
                self._process = None

        return process.returncode if process.returncode is not None else -1, stdout, stderr, timed_out

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        """Ask the engine's process group to stop, then insist."""
        try:
            group = os.getpgid(process.pid)
        except OSError:
            group = None

        def signal_group(sig: int) -> None:
            if group is not None:
                os.killpg(group, sig)
            else:  # pragma: no cover - process already reaped
                process.send_signal(sig)

        try:
            signal_group(signal.SIGTERM)
        except OSError:
            return

        deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.1)

        try:
            signal_group(signal.SIGKILL)
        except OSError:  # pragma: no cover - exited between the two signals
            pass

    def _skipped_run(self, trigger_type: str, scheduled_at_utc: str | None) -> TestRun:
        moment = utc_now()
        return TestRun(
            trigger_type=trigger_type,
            scheduled_at_utc=scheduled_at_utc,
            started_at_utc=to_utc_iso(moment),
            started_at_local=to_local_iso(moment),
            completed_at_utc=to_utc_iso(moment),
            completed_at_local=to_local_iso(moment),
            duration_ms=0,
            status=RunStatus.SKIPPED,
            error_category=ErrorCategory.SKIPPED_OVERLAP,
            error_message="Skipped because the previous test was still running.",
            engine_name="none",
            application_version=self.application_version,
            created_at_utc=to_utc_iso(moment),
        )

    @staticmethod
    def _finish(run: TestRun, started_at: datetime, monotonic_start: float) -> None:
        """Stamp completion time and duration.

        Duration comes from the monotonic clock so a clock adjustment during
        the test cannot produce a negative or wildly wrong figure, while the
        stored timestamps stay wall-clock.
        """
        completed_at = utc_now()
        run.completed_at_utc = to_utc_iso(completed_at)
        run.completed_at_local = to_local_iso(completed_at)
        run.duration_ms = int(round((time.monotonic() - monotonic_start) * 1000))

    def _store(self, run: TestRun, *, notify: Callable[[str], None]) -> RunOutcome:
        try:
            self.database.insert_run(run)
        except DatabaseError as exc:
            # The database is the record of truth; if it cannot be written the
            # user has to be told, and the diagnostic log keeps the evidence.
            log.error("Could not store %s run: %s", run.trigger_type, exc)
            log.error("Unsaved record: %s", json.dumps(run.to_row(), default=str))
            notify("Error")
            return RunOutcome(run=run, stored=False, save_error=str(exc))
        notify("Idle")
        return RunOutcome(run=run, stored=True)


def _link_type_for(interface: str) -> str | None:
    from ..system.network_info import connection_type

    return connection_type(interface)


def _raw_snapshot(stdout: str, stderr: str, exit_code: int) -> str:
    """Keep the engine's unparsed output on failed rows for diagnosis."""
    return json.dumps(
        {
            "exit_code": exit_code,
            "stdout": stdout[:20000],
            "stderr": stderr[:20000],
        },
        sort_keys=True,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "RunOutcome",
    "TestAlreadyRunning",
    "TestRunner",
    "classify_error_text",
    "is_test_running",
    "lock_path",
]
