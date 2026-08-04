"""Application entry point for the graphical interface.

Kept deliberately thin: it opens the database, re-asserts the schedule, and
shows the window. A single instance is enforced by ``Gtk.Application`` so
launching from the application menu twice raises the existing window rather
than opening a second one against the same database.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

from . import APPLICATION_ID, APPLICATION_NAME, APPLICATION_VERSION  # noqa: E402
from .core.scheduler import Scheduler, SchedulerError  # noqa: E402
from .storage.database import Database, DatabaseError  # noqa: E402
from .system import paths  # noqa: E402
from .system.logging_setup import configure_logging, get_logger  # noqa: E402

log = get_logger("application")


class BandwidthLoggerApplication(Gtk.Application):
    """The GTK application object."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APPLICATION_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.database: Database | None = None
        self._window: Gtk.ApplicationWindow | None = None

    def do_startup(self) -> None:  # noqa: N802 - GObject naming
        Gtk.Application.do_startup(self)
        GLib.set_application_name(APPLICATION_NAME)
        Gtk.Window.set_default_icon_name("bandwidth-logger")

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def do_activate(self) -> None:  # noqa: N802 - GObject naming
        if self._window is not None:
            self._window.present()
            return

        try:
            paths.ensure_directories()
            self.database = Database().open()
        except (DatabaseError, OSError) as exc:
            log.exception("Cannot open the database")
            self._fatal(
                "Bandwidth Logger cannot start",
                "The record database could not be opened, so no results could be "
                f"read or saved.\n\n{exc}\n\nThe database is expected at:\n"
                f"{paths.database_path()}",
            )
            return

        # Re-assert the stored schedule against systemd. This repairs a unit
        # file that an upgrade replaced or that went missing, without the
        # user having to touch the switch.
        try:
            Scheduler(self.database).reapply_from_settings()
        except (SchedulerError, DatabaseError) as exc:
            log.warning("Could not re-apply the saved schedule at start-up: %s", exc)

        from .ui.main_window import MainWindow

        self._window = MainWindow(self, self.database)
        self._window.present()

    def do_shutdown(self) -> None:  # noqa: N802 - GObject naming
        # Only the window closes here. The systemd timer is intentionally
        # left running: quitting the application does not switch off
        # automatic testing.
        if self.database is not None:
            self.database.close()
        Gtk.Application.do_shutdown(self)

    def _fatal(self, heading: str, body: str) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_modal(True)
        dialog.set_message(heading)
        dialog.set_detail(body)
        dialog.set_buttons(["Close"])
        dialog.choose(None, None, lambda *_: self.quit())
        self.hold()


def main(argv: list[str] | None = None) -> int:
    """Run the graphical application."""
    arguments = list(sys.argv if argv is None else argv)

    if "--version" in arguments:
        print(f"{APPLICATION_NAME} {APPLICATION_VERSION}")
        return 0

    configure_logging(stream=True)
    log.info("Starting %s %s", APPLICATION_NAME, APPLICATION_VERSION)
    return BandwidthLoggerApplication().run(arguments)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
