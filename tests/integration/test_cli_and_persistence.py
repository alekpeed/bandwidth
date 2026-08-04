"""The headless entry point, and persistence across processes.

This is what the systemd timer actually runs, so these tests exercise the
scheduled path exactly as it happens in production: a fresh process, opening
the database on its own, writing a record, exiting.

They also cover the specification's requirement that the scheduler survives
the interface quitting -- which here means proving that quitting the
application does not stop or disable the timer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import make_run

from bandwidth_logger.core.models import RunStatus, TriggerType
from bandwidth_logger.storage.database import (
    SETTING_AUTO_ENABLED,
    SETTING_INTERVAL_MINUTES,
    Database,
)

pytestmark = pytest.mark.integration

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
FAKE_ENGINE_DIR = Path(__file__).resolve().parents[1] / "support"


def run_cli(
    *arguments: str,
    home: Path,
    extra_path: str | None = None,
    only_path: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the CLI in a separate process, as systemd would.

    ``only_path`` *replaces* PATH rather than extending it. The autouse guard
    in conftest cannot reach a child process, so a test that needs "no engine
    installed" has to make that true in the child's own environment --
    otherwise a real ``speedtest-cli`` on the build machine would be found
    and a live speed test would run.
    """
    environment = {
        **os.environ,
        "BANDWIDTH_LOGGER_HOME": str(home),
        "PYTHONPATH": str(SOURCE_ROOT),
    }
    if only_path is not None:
        environment["PATH"] = only_path
    elif extra_path:
        environment["PATH"] = f"{extra_path}:{environment['PATH']}"
    return subprocess.run(
        [sys.executable, "-m", "bandwidth_logger.cli", *arguments],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )


@pytest.fixture()
def stub_engine_on_path(tmp_path):
    """Put a ``speedtest-cli`` on PATH that prints a recorded result.

    Lets the CLI's own engine selection run for real -- including
    ``shutil.which`` and the version probe -- without any network traffic.
    """
    directory = tmp_path / "bin"
    directory.mkdir()
    script = directory / "speedtest-cli"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('speedtest-cli 2.1.3')\n"
        "    sys.exit(0)\n"
        "print(json.dumps({\n"
        "    'download': 95000000.0,\n"
        "    'upload': 19000000.0,\n"
        "    'ping': 10.5,\n"
        "    'server': {'id': '12345', 'sponsor': 'Example Telecom',\n"
        "               'name': 'Manchester', 'country': 'United Kingdom',\n"
        "               'host': 'speedtest.example.net:8080'},\n"
        "    'client': {'ip': '203.0.113.42', 'isp': 'Example Internet'},\n"
        "}))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(directory)


class TestScheduledRun:
    def test_a_scheduled_run_stores_one_record_and_exits_successfully(
        self, isolated_home, stub_engine_on_path
    ):
        result = run_cli("--scheduled", home=isolated_home, extra_path=stub_engine_on_path)

        assert result.returncode == 0, result.stderr

        database = Database().open()
        try:
            runs = database.list_runs()
            assert len(runs) == 1
            assert runs[0].trigger_type == TriggerType.SCHEDULED
            assert runs[0].status == RunStatus.SUCCESS
            assert runs[0].download_bps == 95_000_000
            assert runs[0].engine_name == "speedtest-cli"
            # A scheduled run records when it was due, as well as when it ran.
            assert runs[0].scheduled_at_utc is not None
        finally:
            database.close()

    def test_a_manual_run_is_marked_manual(self, isolated_home, stub_engine_on_path):
        result = run_cli("--now", home=isolated_home, extra_path=stub_engine_on_path)
        assert result.returncode == 0, result.stderr

        database = Database().open()
        try:
            run = database.latest_run()
            assert run.trigger_type == TriggerType.MANUAL
            assert run.scheduled_at_utc is None
        finally:
            database.close()

    def test_a_failed_test_still_exits_zero_so_the_timer_keeps_running(self, isolated_home, tmp_path):
        """No engine is on PATH, so the test fails.

        The unit must not be marked failed for that: a failing test is a
        recorded outcome, and one bad test must never stop future scheduling.

        PATH is replaced, not extended, so no engine can be discovered even
        on a machine that has one installed.
        """
        empty = tmp_path / "empty-path"
        empty.mkdir()
        result = run_cli("--scheduled", home=isolated_home, only_path=str(empty))

        assert result.returncode == 0, result.stderr

        database = Database().open()
        try:
            run = database.latest_run()
            assert run.status == RunStatus.FAILED
            assert run.error_category == "engine_missing"
        finally:
            database.close()

    def test_records_survive_between_processes(self, isolated_home, stub_engine_on_path):
        for _ in range(3):
            run_cli("--scheduled", home=isolated_home, extra_path=stub_engine_on_path)

        database = Database().open()
        try:
            assert database.count_runs() == 3
        finally:
            database.close()


class TestStatusCommand:
    def test_status_reports_the_stored_state_as_json(self, isolated_home, stub_engine_on_path):
        run_cli("--now", home=isolated_home, extra_path=stub_engine_on_path)
        result = run_cli("--status", "--json", home=isolated_home, extra_path=stub_engine_on_path)

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["record_count"] == 1
        assert payload["schema_version"] >= 1
        assert payload["engines"]["selected"] == "speedtest-cli"
        assert payload["latest_run"]["status"] == "success"

    def test_status_works_on_an_empty_database(self, isolated_home):
        result = run_cli("--status", home=isolated_home)
        assert result.returncode == 0, result.stderr
        assert "Records stored:      0" in result.stdout


class TestExportCommand:
    def test_export_writes_every_record(self, isolated_home, tmp_path, stub_engine_on_path):
        run_cli("--now", home=isolated_home, extra_path=stub_engine_on_path)
        destination = tmp_path / "export.csv"

        result = run_cli("--export", str(destination), home=isolated_home)

        assert result.returncode == 0, result.stderr
        assert "Exported 1 record" in result.stdout
        assert destination.exists()
        assert "external_ip" not in destination.read_text(encoding="utf-8").splitlines()[0]

    def test_export_row_count_matches_the_selected_records(self, isolated_home, tmp_path, stub_engine_on_path):
        for _ in range(4):
            run_cli("--scheduled", home=isolated_home, extra_path=stub_engine_on_path)

        destination = tmp_path / "export.csv"
        run_cli("--export", str(destination), home=isolated_home)

        lines = destination.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 5  # header plus four records

        database = Database().open()
        try:
            assert database.count_runs() == 4
        finally:
            database.close()

    def test_the_external_ip_can_be_included_on_request(self, isolated_home, tmp_path, stub_engine_on_path):
        run_cli("--now", home=isolated_home, extra_path=stub_engine_on_path)
        destination = tmp_path / "export.csv"

        run_cli("--export", str(destination), "--include-external-ip", home=isolated_home)

        text = destination.read_text(encoding="utf-8")
        assert "external_ip" in text.splitlines()[0]
        assert "203.0.113.42" in text


class TestSchedulerSurvivesTheInterface:
    def test_quitting_the_application_does_not_disable_the_timer(self):
        """The application's shutdown closes the database and nothing else.

        If shutdown ever started stopping the timer, scheduling would
        silently die whenever the user quit -- exactly what the
        specification forbids.

        Read from source rather than imported, so the check still runs on a
        machine without GTK installed.
        """
        source = (SOURCE_ROOT / "bandwidth_logger" / "application.py").read_text(encoding="utf-8")
        body = source.split("def do_shutdown")[1].split("\ndef ")[0]

        for forbidden in ("stop(", "disable", "remove_units", "apply("):
            assert forbidden not in body, f"shutdown must not {forbidden} the scheduler"
        assert "database.close()" in body

    def test_the_stored_schedule_is_still_enabled_after_the_process_exits(
        self, isolated_home, stub_engine_on_path
    ):
        database = Database().open()
        database.set_bool(SETTING_AUTO_ENABLED, True)
        database.set_int(SETTING_INTERVAL_MINUTES, 30)
        database.close()

        # A completely separate process, as the timer would be.
        run_cli("--scheduled", home=isolated_home, extra_path=stub_engine_on_path)

        reopened = Database().open()
        try:
            assert reopened.get_bool(SETTING_AUTO_ENABLED) is True
            assert reopened.get_int(SETTING_INTERVAL_MINUTES) == 30
        finally:
            reopened.close()


class TestDatabasePersistence:
    def test_settings_and_records_survive_reopening(self, isolated_home):
        database = Database().open()
        database.set_int(SETTING_INTERVAL_MINUTES, 240)
        for index in range(5):
            database.insert_run(make_run(isp_name=f"Provider {index}"))
        database.close()

        reopened = Database().open()
        try:
            assert reopened.count_runs() == 5
            assert reopened.get_int(SETTING_INTERVAL_MINUTES) == 240
        finally:
            reopened.close()

    def test_deleting_a_range_leaves_the_rest_untouched(self, database):
        from datetime import timedelta

        from bandwidth_logger.core.models import to_utc_iso, utc_now

        now = utc_now()
        for days in range(6):
            database.insert_run(make_run(moment=now - timedelta(days=days)))

        cutoff = to_utc_iso(now - timedelta(days=2, hours=12))
        removed = database.delete_runs(start_utc=cutoff)

        assert removed == 3
        assert database.count_runs() == 3

    def test_the_table_never_omits_failed_attempts(self, database):
        database.insert_run(make_run(status=RunStatus.SUCCESS))
        database.insert_run(make_run(status=RunStatus.FAILED))
        database.insert_run(make_run(status=RunStatus.TIMEOUT))
        database.insert_run(make_run(status=RunStatus.SKIPPED))

        assert len(database.list_runs()) == 4

    def test_ordering_can_be_reversed(self, database):
        from datetime import timedelta

        from bandwidth_logger.core.models import utc_now

        now = utc_now()
        for hours in range(4):
            database.insert_run(make_run(moment=now - timedelta(hours=hours), isp_name=f"h{hours}"))

        newest = database.list_runs(newest_first=True)
        oldest = database.list_runs(newest_first=False)

        assert [run.isp_name for run in newest] == ["h0", "h1", "h2", "h3"]
        assert [run.isp_name for run in oldest] == ["h3", "h2", "h1", "h0"]
