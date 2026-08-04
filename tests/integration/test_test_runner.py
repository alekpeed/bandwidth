"""End-to-end runs against the fake engine.

These spawn real subprocesses. Nothing here touches the internet: the fake
engine prints recorded output and exits.
"""

from __future__ import annotations

import os
import threading
import time

import pytest
from support import (
    BEHAVIOUR_DNS_FAILURE,
    BEHAVIOUR_FAILURE,
    BEHAVIOUR_HANG,
    BEHAVIOUR_MALFORMED,
    BEHAVIOUR_SPARSE,
    BEHAVIOUR_SUCCESS,
    BEHAVIOUR_ZEROS,
    FakeEngine,
)

from bandwidth_logger.core.models import ErrorCategory, RunStatus, TriggerType
from bandwidth_logger.core.test_runner import TestRunner, is_test_running
from bandwidth_logger.storage.database import DatabaseError

pytestmark = pytest.mark.integration


class TestSuccessfulRun:
    def test_a_successful_result_is_stored_exactly_once(self, runner, database):
        outcome = runner.run_test(TriggerType.MANUAL)

        assert outcome.stored is True
        assert outcome.run.status == RunStatus.SUCCESS
        assert database.count_runs() == 1

        stored = database.get_run(outcome.run.id)
        assert stored.download_bps == 95_000_000
        assert stored.upload_bps == 19_000_000
        assert stored.idle_latency_ms == 10.5
        assert stored.isp_name == "Example Internet"
        assert stored.trigger_type == TriggerType.MANUAL

    def test_timings_and_engine_details_are_recorded(self, runner, database):
        run = runner.run_test(TriggerType.MANUAL).run

        assert run.started_at_utc.endswith("+00:00")
        assert run.completed_at_utc is not None
        assert run.started_at_local
        assert run.completed_at_local
        assert run.duration_ms is not None and run.duration_ms >= 0
        assert run.engine_name == "fake"
        assert run.engine_version == "9.9.9"
        assert run.application_version

    def test_the_raw_engine_result_is_kept(self, runner, database):
        run = runner.run_test(TriggerType.MANUAL).run
        assert run.raw_result_json
        assert "Example Telecom" in run.raw_result_json

    def test_a_sparse_result_stores_nulls_not_zeros(self, database):
        runner = TestRunner(database, engine=FakeEngine(BEHAVIOUR_SPARSE), timeout_seconds=30)
        run = runner.run_test(TriggerType.MANUAL).run

        assert run.status == RunStatus.SUCCESS
        assert run.download_bps == 10_000_000
        assert run.jitter_ms is None
        assert run.packet_loss_percent is None
        assert run.server_name is None

    def test_measured_zeros_survive_the_round_trip(self, database):
        runner = TestRunner(database, engine=FakeEngine(BEHAVIOUR_ZEROS), timeout_seconds=30)
        run_id = runner.run_test(TriggerType.MANUAL).run.id

        stored = database.get_run(run_id)
        assert stored.status == RunStatus.SUCCESS
        assert stored.download_bps == 0
        assert stored.upload_bps == 0
        assert stored.packet_loss_percent == 0.0
        # And they are still distinguishable from missing data.
        assert stored.download_bps is not None


class TestFailures:
    def test_a_nonzero_exit_is_stored_as_a_failed_attempt(self, database):
        runner = TestRunner(database, engine=FakeEngine(BEHAVIOUR_FAILURE), timeout_seconds=30)
        outcome = runner.run_test(TriggerType.SCHEDULED)

        assert outcome.stored is True
        assert outcome.run.status == RunStatus.FAILED
        assert outcome.run.error_message
        assert database.count_runs() == 1

        stored = database.get_run(outcome.run.id)
        # A failed attempt still carries full timing, so a gap in the history
        # always has a dated, explained record.
        assert stored.started_at_utc
        assert stored.completed_at_utc
        assert stored.duration_ms is not None
        assert stored.download_bps is None

    def test_engine_wording_is_normalised_into_a_category(self, database):
        runner = TestRunner(database, engine=FakeEngine(BEHAVIOUR_DNS_FAILURE), timeout_seconds=30)
        run = runner.run_test(TriggerType.MANUAL).run

        assert run.status == RunStatus.FAILED
        assert run.error_category == ErrorCategory.DNS_FAILURE
        # The complete diagnostic text is kept, not just the category.
        assert "name resolution" in run.error_message

    def test_unusable_output_from_a_successful_exit_is_a_failure(self, database):
        runner = TestRunner(database, engine=FakeEngine(BEHAVIOUR_MALFORMED), timeout_seconds=30)
        run = runner.run_test(TriggerType.MANUAL).run

        assert run.status == RunStatus.FAILED
        assert run.error_category == ErrorCategory.MALFORMED_OUTPUT
        assert database.count_runs() == 1

    def test_a_missing_engine_is_recorded_rather_than_ignored(self, database):
        runner = TestRunner(database, engine=None, timeout_seconds=30)
        outcome = runner.run_test(TriggerType.SCHEDULED)

        assert outcome.stored is True
        assert outcome.run.status == RunStatus.FAILED
        assert outcome.run.error_category == ErrorCategory.ENGINE_MISSING
        assert database.count_runs() == 1

    def test_one_failure_does_not_prevent_the_next_test(self, database):
        failing = TestRunner(database, engine=FakeEngine(BEHAVIOUR_FAILURE), timeout_seconds=30)
        failing.run_test(TriggerType.SCHEDULED)

        working = TestRunner(database, engine=FakeEngine(BEHAVIOUR_SUCCESS), timeout_seconds=30)
        outcome = working.run_test(TriggerType.SCHEDULED)

        assert outcome.run.status == RunStatus.SUCCESS
        assert database.count_runs() == 2

    def test_an_unwritable_database_is_surfaced_not_swallowed(self, database, monkeypatch):
        def explode(_run):
            raise DatabaseError("disk went away")

        monkeypatch.setattr(database, "insert_run", explode)
        runner = TestRunner(database, engine=FakeEngine(), timeout_seconds=30)
        outcome = runner.run_test(TriggerType.MANUAL)

        assert outcome.stored is False
        assert "disk went away" in outcome.save_error


class TestTimeout:
    def test_a_timeout_is_recorded_and_the_process_is_stopped(self, database, tmp_path):
        marker = tmp_path / "child.pid"
        os.environ["FAKE_ENGINE_CHILD_PID_FILE"] = str(marker)
        try:
            runner = TestRunner(
                database, engine=FakeEngine(BEHAVIOUR_HANG), timeout_seconds=30
            )
            # Override the resolved timeout to keep the test quick.
            runner._forced_timeout = 2
            started = time.monotonic()
            outcome = runner.run_test(TriggerType.SCHEDULED)
            elapsed = time.monotonic() - started
        finally:
            os.environ.pop("FAKE_ENGINE_CHILD_PID_FILE", None)

        assert outcome.run.status == RunStatus.TIMEOUT
        assert outcome.run.error_category == ErrorCategory.TIMEOUT
        assert "2 seconds" in outcome.run.error_message
        assert database.count_runs() == 1
        # It really stopped rather than running to completion.
        assert elapsed < 30

        # The engine's own child was killed too, not orphaned.
        if marker.exists():
            child_pid = int(marker.read_text())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not _process_alive(child_pid):
                    break
                time.sleep(0.1)
            assert not _process_alive(child_pid), "the engine's child process survived"

    def test_the_run_lock_is_released_after_a_timeout(self, database):
        runner = TestRunner(database, engine=FakeEngine(BEHAVIOUR_HANG), timeout_seconds=30)
        runner._forced_timeout = 2
        runner.run_test(TriggerType.SCHEDULED)

        assert is_test_running() is False


class TestOverlapPrevention:
    def test_two_simultaneous_requests_do_not_run_concurrently(self, database):
        """Overlap policy: the second attempt is skipped and recorded.

        Both threads ask to run at once. Exactly one executes the engine; the
        other must produce a ``skipped`` record with ``skipped_overlap`` --
        not a silent no-op, and not a second concurrent engine process.
        """
        engine = FakeEngine(BEHAVIOUR_SUCCESS, delay_seconds=1.5)
        runner_one = TestRunner(database, engine=engine, timeout_seconds=30)
        runner_two = TestRunner(database, engine=engine, timeout_seconds=30)

        outcomes = {}
        barrier = threading.Barrier(2)

        def attempt(name, runner):
            barrier.wait()
            outcomes[name] = runner.run_test(TriggerType.MANUAL)

        threads = [
            threading.Thread(target=attempt, args=("first", runner_one)),
            threading.Thread(target=attempt, args=("second", runner_two)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        statuses = sorted(outcome.run.status for outcome in outcomes.values())
        assert statuses == [RunStatus.SKIPPED, RunStatus.SUCCESS]

        # Both attempts are in the history; neither disappeared.
        assert database.count_runs() == 2
        skipped = [r for r in database.list_runs() if r.status == RunStatus.SKIPPED]
        assert len(skipped) == 1
        assert skipped[0].error_category == ErrorCategory.SKIPPED_OVERLAP
        assert "still running" in skipped[0].error_message

        # Only one engine process was ever started.
        assert engine.invocations == 1

    def test_a_scheduled_test_is_skipped_while_a_manual_one_runs(self, database):
        slow = FakeEngine(BEHAVIOUR_SUCCESS, delay_seconds=1.5)
        manual_runner = TestRunner(database, engine=slow, timeout_seconds=30)
        scheduled_runner = TestRunner(database, engine=FakeEngine(), timeout_seconds=30)

        thread = threading.Thread(target=lambda: manual_runner.run_test(TriggerType.MANUAL))
        thread.start()
        try:
            _wait_until(lambda: is_test_running(), timeout=5)
            outcome = scheduled_runner.run_test(TriggerType.SCHEDULED)
        finally:
            thread.join(timeout=30)

        assert outcome.run.status == RunStatus.SKIPPED
        assert outcome.run.trigger_type == TriggerType.SCHEDULED
        assert outcome.run.error_category == ErrorCategory.SKIPPED_OVERLAP

    def test_the_lock_is_free_before_and_after_a_run(self, runner):
        assert is_test_running() is False
        runner.run_test(TriggerType.MANUAL)
        assert is_test_running() is False

    def test_the_lock_is_held_across_processes(self, database, isolated_home):
        """The lock has to work between the window and the timer, which are
        separate processes, so it is checked from a second process.
        """
        import subprocess
        import sys

        slow = FakeEngine(BEHAVIOUR_SUCCESS, delay_seconds=2.0)
        runner = TestRunner(database, engine=slow, timeout_seconds=30)
        thread = threading.Thread(target=lambda: runner.run_test(TriggerType.MANUAL))
        thread.start()
        try:
            _wait_until(lambda: is_test_running(), timeout=5)
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.path.insert(0, %r);"
                    "from bandwidth_logger.core.test_runner import is_test_running;"
                    "print(is_test_running())" % str(_source_root()),
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "BANDWIDTH_LOGGER_HOME": str(isolated_home)},
                timeout=30,
            )
        finally:
            thread.join(timeout=30)

        assert probe.stdout.strip() == "True", probe.stderr


class TestSharedCodePath:
    def test_scheduled_and_manual_tests_use_the_same_runner(self, database):
        engine = FakeEngine()
        runner = TestRunner(database, engine=engine, timeout_seconds=30)

        manual = runner.run_test(TriggerType.MANUAL).run
        scheduled = runner.run_test(TriggerType.SCHEDULED, scheduled_at_utc="2026-08-04T09:00:00+00:00").run

        # Identical measurements and identical storage; only the trigger and
        # the scheduled time differ.
        assert manual.download_bps == scheduled.download_bps
        assert manual.engine_name == scheduled.engine_name
        assert manual.trigger_type == TriggerType.MANUAL
        assert scheduled.trigger_type == TriggerType.SCHEDULED
        assert manual.scheduled_at_utc is None
        assert scheduled.scheduled_at_utc == "2026-08-04T09:00:00+00:00"
        assert engine.invocations == 2

    def test_progress_states_are_reported(self, runner):
        seen = []
        runner.run_test(TriggerType.MANUAL, on_state=seen.append)
        assert "Testing" in seen
        assert "Saving result" in seen


class TestCancellation:
    def test_cancelling_a_running_test_records_it_as_cancelled(self, database):
        runner = TestRunner(database, engine=FakeEngine(BEHAVIOUR_HANG), timeout_seconds=60)
        result = {}

        thread = threading.Thread(
            target=lambda: result.update(outcome=runner.run_test(TriggerType.MANUAL))
        )
        thread.start()
        try:
            _wait_until(lambda: runner.is_running, timeout=10)
            assert runner.cancel() is True
        finally:
            thread.join(timeout=30)

        outcome = result["outcome"]
        assert outcome.run.status == RunStatus.CANCELLED
        assert outcome.run.error_category == ErrorCategory.CANCELLED
        assert database.count_runs() == 1

    def test_cancelling_when_nothing_runs_reports_so(self, runner):
        assert runner.cancel() is False


class TestConfiguration:
    def test_the_timeout_setting_is_clamped_to_a_sane_band(self, database):
        from bandwidth_logger.storage.database import SETTING_TIMEOUT_SECONDS

        runner = TestRunner(database)

        database.set_int(SETTING_TIMEOUT_SECONDS, 1)
        assert runner.resolve_timeout() == 30

        database.set_int(SETTING_TIMEOUT_SECONDS, 999_999)
        assert runner.resolve_timeout() == 3600

        database.set_int(SETTING_TIMEOUT_SECONDS, 180)
        assert runner.resolve_timeout() == 180

    def test_the_engine_command_is_never_shell_interpreted(self, database):
        engine = FakeEngine()
        command = engine.build_command()
        assert isinstance(command, list)
        assert all(isinstance(part, str) for part in command)


# ----------------------------------------------------------------------


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition was not met within the timeout")


def _source_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2] / "src"


class TestServerPinning:
    """The stored pin reaches the engine."""

    def test_the_saved_server_is_applied_to_the_resolved_engine(self, database, monkeypatch):
        from bandwidth_logger.engines.ookla import OoklaEngine
        from bandwidth_logger.storage.database import SETTING_SERVER_ID

        monkeypatch.setattr(OoklaEngine, "is_available", lambda self: True)
        database.set_setting(SETTING_SERVER_ID, "16976")

        engine = TestRunner(database).resolve_engine()

        assert engine is not None
        assert engine.server_id == "16976"

    def test_an_empty_setting_leaves_the_engine_free_to_choose(self, database, monkeypatch):
        from bandwidth_logger.engines.ookla import OoklaEngine
        from bandwidth_logger.storage.database import SETTING_SERVER_ID

        monkeypatch.setattr(OoklaEngine, "is_available", lambda self: True)
        database.set_setting(SETTING_SERVER_ID, "   ")

        assert TestRunner(database).resolve_engine().server_id is None

    def test_the_server_that_answered_is_recorded_either_way(self, runner, database):
        """Pinned or not, the row names the server, so a history can always
        be checked for a server change after the fact.
        """
        run = runner.run_test(TriggerType.MANUAL).run

        assert run.server_id == "12345"
        assert run.server_name == "Example Telecom"
