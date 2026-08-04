"""Building the interface for real.

Not a replacement for using the application: it drives no clicks and takes no
screenshots. What it does catch is the class of mistake that unit tests
cannot -- a widget method that does not exist, a signal with the wrong
signature, a property GTK rejects -- any of which would crash the window on
launch while every other test still passed.

Skipped when GTK or a display is unavailable, so the suite stays runnable on
a headless machine with no GTK installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"

# Constructed in a subprocess: GTK cannot be initialised twice in one process,
# and a crash in GTK takes the whole process down with it.
BUILD_SCRIPT = """
import sys
sys.path.insert(0, %(source_root)r)

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk

from bandwidth_logger.storage.database import Database
from bandwidth_logger.core.models import RunStatus, TriggerType, utc_now, to_utc_iso, to_local_iso
from bandwidth_logger.core.models import TestRun

assert Gtk.init_check(), "GTK could not open a display"

database = Database().open()

# One successful record and one failure, so both rendering paths are built.
moment = utc_now()
for status, error in ((RunStatus.SUCCESS, None), (RunStatus.FAILED, "no_network")):
    database.insert_run(TestRun(
        trigger_type=TriggerType.MANUAL,
        started_at_utc=to_utc_iso(moment),
        started_at_local=to_local_iso(moment),
        completed_at_utc=to_utc_iso(moment),
        completed_at_local=to_local_iso(moment),
        duration_ms=9000,
        status=status,
        error_category=error,
        error_message="Engine said: no route to host" if error else None,
        download_bps=95_000_000 if not error else None,
        upload_bps=19_000_000 if not error else None,
        idle_latency_ms=10.5 if not error else None,
        isp_name="Example Internet",
        server_name="Example Telecom",
        server_city="Manchester",
        interface_name="enp3s0",
        connection_type="Ethernet",
        engine_name="fake",
        engine_version="9.9.9",
        application_version="1.0.0",
        created_at_utc=to_utc_iso(moment),
        raw_result_json='{"ok": true}',
    ))

from bandwidth_logger.ui.main_window import (
    DeleteDialog, ExportDialog, MainWindow,
)
from bandwidth_logger.ui.details_dialog import DetailsDialog
from bandwidth_logger.ui.settings_dialog import EngineSetupDialog, SettingsDialog

application = Gtk.Application(application_id="org.bandwidthlogger.SmokeTest")

built = []

def on_activate(app):
    window = MainWindow(app, database)
    built.append("main window")

    # The table must have populated from the database.
    assert window.store.get_n_items() == 2, window.store.get_n_items()
    assert window.column_view.get_columns().get_n_items() == 9

    # Every dialog the user can reach.
    DetailsDialog(window, database.latest_run())
    built.append("details dialog")
    SettingsDialog(window, database)
    built.append("settings dialog")
    EngineSetupDialog(window, database)
    built.append("engine setup dialog")
    ExportDialog(window, database)
    built.append("export dialog")
    DeleteDialog(window, database, on_deleted=lambda: None)
    built.append("delete dialog")

    # Exercise the refresh path, which runs on a timer in normal use.
    window.refresh_all()
    built.append("refresh")

    # Switching order re-reads and re-renders the table.
    window.order_dropdown.set_selected(1)
    assert window.store.get_n_items() == 2
    built.append("reorder")

    app.quit()

application.connect("activate", on_activate)
code = application.run([])

print("BUILT:" + ",".join(built))
sys.exit(code)
""" % {"source_root": str(SOURCE_ROOT)}


def _gtk_available() -> bool:
    probe = subprocess.run(
        [sys.executable, "-c", "import gi; gi.require_version('Gtk','4.0'); from gi.repository import Gtk"],
        capture_output=True,
    )
    return probe.returncode == 0


@pytest.mark.skipif(not _gtk_available(), reason="GTK 4 / PyGObject is not installed")
@pytest.mark.skipif(
    not os.environ.get("DISPLAY") and not shutil.which("xvfb-run"),
    reason="no display and no xvfb-run to provide one",
)
def test_the_whole_interface_builds_without_error(tmp_path, isolated_home):
    """Build the window and every dialog against a real GTK."""
    script = tmp_path / "build_ui.py"
    script.write_text(BUILD_SCRIPT, encoding="utf-8")

    command = [sys.executable, str(script)]
    if not os.environ.get("DISPLAY"):
        command = ["xvfb-run", "-a", *command]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, "BANDWIDTH_LOGGER_HOME": str(isolated_home), "GSETTINGS_BACKEND": "memory"},
        timeout=180,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    built = [line for line in result.stdout.splitlines() if line.startswith("BUILT:")]
    assert built, result.stdout
    for part in (
        "main window",
        "details dialog",
        "settings dialog",
        "engine setup dialog",
        "export dialog",
        "delete dialog",
        "refresh",
        "reorder",
    ):
        assert part in built[0], f"{part} was not reached: {built[0]}"

    # GTK warns loudly about misused widgets; none of that is acceptable.
    for line in result.stderr.splitlines():
        assert "CRITICAL" not in line, line
        assert "Gtk-WARNING" not in line, line
