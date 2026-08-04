"""The main window.

Layout follows the specification: a status area across the top, the complete
log table in the middle, and the four controls along the bottom.

Two things shape the code more than anything else. First, a test runs on a
worker thread and every update is pushed back to the main loop with
``GLib.idle_add``, because a GTK widget may only be touched from the thread
that owns the main loop. Second, scheduled tests are performed by a separate
process -- the systemd timer -- so this window cannot rely on having seen
them happen; it re-reads the database on a timer and shows what is actually
there.
"""

from __future__ import annotations

import threading
from datetime import date, datetime

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, GObject, Gtk  # noqa: E402

from .. import APPLICATION_NAME, APPLICATION_VERSION  # noqa: E402
from ..core.models import (  # noqa: E402
    RunStatus,
    TestRun,
    TriggerType,
    format_duration,
    format_for_display,
    format_mbps,
    format_ms,
    parse_iso,
    summarise_error,
)
from ..core.scheduler import (  # noqa: E402
    INTERVAL_PRESETS,
    Scheduler,
    SchedulerError,
    SchedulerStatus,
    describe_interval,
    validate_interval,
)
from ..core.test_runner import TestRunner, is_test_running  # noqa: E402
from ..engines.registry import available_engines  # noqa: E402
from ..storage.database import (  # noqa: E402
    SETTING_AUTO_ENABLED,
    SETTING_CLOSE_NOTICE_SHOWN,
    SETTING_EXPORT_INCLUDE_IP,
    SETTING_INTERVAL_MINUTES,
    SETTING_RUN_AFTER_LOGIN,
    SETTING_SORT_NEWEST_FIRST,
    Database,
    DatabaseError,
)
from ..storage.export_csv import (  # noqa: E402
    default_filename,
    export_database,
    local_day_end_utc,
    local_day_start_utc,
)
from ..system import paths  # noqa: E402
from ..system.logging_setup import get_logger  # noqa: E402
from .details_dialog import DetailsDialog  # noqa: E402
from .settings_dialog import EngineSetupDialog, SettingsDialog  # noqa: E402

log = get_logger("main_window")

#: How often the window re-reads the database and the timer state. Scheduled
#: tests happen in another process, so this is how their results appear.
REFRESH_SECONDS = 10

CUSTOM_INTERVAL_LABEL = "Custom interval..."


class RunObject(GObject.Object):
    """Wraps a :class:`TestRun` so it can live in a ``Gio.ListStore``."""

    __gtype_name__ = "BandwidthLoggerRunObject"

    def __init__(self, run: TestRun) -> None:
        super().__init__()
        self.run = run


class MainWindow(Gtk.ApplicationWindow):
    """Bandwidth Logger's only window."""

    def __init__(self, application: Gtk.Application, database: Database) -> None:
        super().__init__(
            application=application,
            title=APPLICATION_NAME,
            default_width=1180,
            default_height=760,
        )
        self.database = database
        self.scheduler = Scheduler(database)
        self.runner = TestRunner(database)
        self._test_thread: threading.Thread | None = None
        self._state_text = "Idle"
        self._last_status: SchedulerStatus | None = None
        self._suppress_switch = False

        self._build_header()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        self.banner = self._build_banner()
        root.append(self.banner)

        root.append(self._build_status_area())
        root.append(self._build_table())
        root.append(self._build_bottom_controls())

        self._load_settings_into_controls()
        self.refresh_all()

        self._refresh_source = GLib.timeout_add_seconds(REFRESH_SECONDS, self._on_periodic_refresh)
        self.connect("close-request", self._on_close_request)

        GLib.idle_add(self._first_run_checks)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_header(self) -> None:
        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        menu = Gio.Menu()
        menu.append("Settings", "win.settings")
        menu.append("Set up speed-test engine", "win.engine-setup")
        menu.append("About Bandwidth Logger", "win.about")

        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_tooltip_text("Main menu")
        menu_button.set_menu_model(menu)
        menu_button.update_property([Gtk.AccessibleProperty.LABEL], ["Main menu"])
        header.pack_end(menu_button)

        for name, handler in (
            ("settings", lambda *_: self._open_settings()),
            ("engine-setup", lambda *_: EngineSetupDialog(self, self.database).present()),
            ("about", lambda *_: self._show_about()),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

    def _build_banner(self) -> Gtk.Box:
        """Warning strip shown above the status area when something is wrong.

        Carries an explicit word ("Scheduler error", "No engine"), never a
        colour on its own.
        """
        banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        banner.set_margin_top(10)
        banner.set_margin_start(14)
        banner.set_margin_end(14)
        banner.set_visible(False)

        self.banner_label = Gtk.Label(xalign=0.0, wrap=True, hexpand=True)
        banner.append(self.banner_label)

        self.banner_button = Gtk.Button(label="Details")
        self.banner_button.connect("clicked", self._on_banner_action)
        banner.append(self.banner_button)
        self._banner_action = "details"
        return banner

    def _build_status_area(self) -> Gtk.Widget:
        frame = Gtk.Frame()
        frame.set_margin_top(12)
        frame.set_margin_start(14)
        frame.set_margin_end(14)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(14)
        outer.set_margin_bottom(14)
        outer.set_margin_start(14)
        outer.set_margin_end(14)
        frame.set_child(outer)

        # -- automatic testing and interval --------------------------------
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        outer.append(top)

        auto_label = Gtk.Label(label="_Automatic testing", use_underline=True)
        self.auto_switch = Gtk.Switch()
        self.auto_switch.set_valign(Gtk.Align.CENTER)
        self.auto_switch.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Automatic testing on or off"]
        )
        auto_label.set_mnemonic_widget(self.auto_switch)
        self.auto_switch.connect("state-set", self._on_auto_switch)
        top.append(auto_label)
        top.append(self.auto_switch)

        self.auto_state_label = Gtk.Label(label="Off")
        self.auto_state_label.set_width_chars(4)
        top.append(self.auto_state_label)

        top.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        interval_label = Gtk.Label(label="Test _interval", use_underline=True)
        top.append(interval_label)

        self._interval_labels = [label for _minutes, label in INTERVAL_PRESETS]
        self._interval_labels.append(CUSTOM_INTERVAL_LABEL)
        self.interval_dropdown = Gtk.DropDown.new_from_strings(self._interval_labels)
        self.interval_dropdown.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Test interval"]
        )
        self.interval_dropdown.connect("notify::selected", self._on_interval_choice)
        interval_label.set_mnemonic_widget(self.interval_dropdown)
        top.append(self.interval_dropdown)

        self.custom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.custom_spin = Gtk.SpinButton.new_with_range(1, 10080, 1)
        self.custom_spin.set_value(30)
        self.custom_spin.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Custom interval amount"]
        )
        self.custom_units = Gtk.DropDown.new_from_strings(["Minutes", "Hours"])
        self.custom_units.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Custom interval unit"]
        )
        self.custom_box.append(self.custom_spin)
        self.custom_box.append(self.custom_units)
        self.custom_box.set_visible(False)
        top.append(self.custom_box)

        self.apply_button = Gtk.Button(label="_Apply", use_underline=True)
        self.apply_button.set_tooltip_text("Apply the selected interval from now")
        self.apply_button.connect("clicked", self._on_apply_interval)
        top.append(self.apply_button)

        spacer = Gtk.Box(hexpand=True)
        top.append(spacer)

        self.test_now_button = Gtk.Button(label="_Test Now", use_underline=True)
        self.test_now_button.add_css_class("suggested-action")
        self.test_now_button.connect("clicked", self._on_test_now)
        top.append(self.test_now_button)

        # -- next test and current state -----------------------------------
        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        outer.append(bottom)

        self.next_test_label = Gtk.Label(label="Next test: Not scheduled", xalign=0.0)
        bottom.append(self.next_test_label)

        self.state_label = Gtk.Label(label="Current state: Idle", xalign=0.0)
        bottom.append(self.state_label)

        self.progress = Gtk.ProgressBar()
        self.progress.set_valign(Gtk.Align.CENTER)
        self.progress.set_hexpand(True)
        self.progress.set_visible(False)
        self.progress.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Speed test in progress"]
        )
        bottom.append(self.progress)

        return frame

    def _build_table(self) -> Gtk.Widget:
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        container.set_margin_top(12)
        container.set_margin_start(14)
        container.set_margin_end(14)
        container.set_vexpand(True)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        heading = Gtk.Label(label="Test history", xalign=0.0)
        heading.add_css_class("heading")
        controls.append(heading)

        self.record_count_label = Gtk.Label(label="", xalign=0.0)
        self.record_count_label.add_css_class("dim-label")
        controls.append(self.record_count_label)

        controls.append(Gtk.Box(hexpand=True))

        order_label = Gtk.Label(label="_Order", use_underline=True)
        controls.append(order_label)
        self.order_dropdown = Gtk.DropDown.new_from_strings(["Newest first", "Oldest first"])
        self.order_dropdown.update_property([Gtk.AccessibleProperty.LABEL], ["Table order"])
        self.order_dropdown.connect("notify::selected", self._on_order_changed)
        order_label.set_mnemonic_widget(self.order_dropdown)
        controls.append(self.order_dropdown)

        refresh = Gtk.Button(label="_Refresh", use_underline=True)
        refresh.connect("clicked", lambda _button: self.refresh_all())
        controls.append(refresh)

        container.append(controls)

        self.store = Gio.ListStore.new(RunObject)
        # Multiple selection so several rows can be deleted at once. Opening
        # the details of one row still works: activating a row (double-click
        # or Enter) is a separate gesture from selecting it.
        self.selection = Gtk.MultiSelection.new(self.store)

        self.column_view = Gtk.ColumnView.new(self.selection)
        self.column_view.set_show_row_separators(True)
        self.column_view.set_show_column_separators(True)
        self.column_view.set_vexpand(True)
        self.column_view.connect("activate", self._on_row_activated)
        self.selection.connect(
            "selection-changed", lambda *_: self._update_delete_selected_state()
        )

        for title, accessor, width, numeric in _TABLE_COLUMNS:
            self.column_view.append_column(_build_column(title, accessor, width, numeric))

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self.column_view)

        frame = Gtk.Frame()
        frame.set_child(scroller)
        container.append(frame)

        hint = Gtk.Label(
            label=(
                "Every attempted test is listed, including failed, timed-out and "
                "skipped attempts. Select a row and press Enter, or double-click it, "
                "to see the complete record."
            ),
            xalign=0.0,
            wrap=True,
        )
        hint.add_css_class("dim-label")
        container.append(hint)

        return container

    def _build_bottom_controls(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)

        export = Gtk.Button(label="_Export CSV", use_underline=True)
        export.connect("clicked", self._on_export)
        box.append(export)

        open_folder = Gtk.Button(label="Open _data folder", use_underline=True)
        open_folder.connect("clicked", self._on_open_data_folder)
        box.append(open_folder)

        self.delete_selected_button = Gtk.Button(
            label="Delete se_lected", use_underline=True
        )
        self.delete_selected_button.add_css_class("destructive-action")
        self.delete_selected_button.set_tooltip_text(
            "Delete only the rows selected in the table"
        )
        self.delete_selected_button.set_sensitive(False)
        self.delete_selected_button.connect("clicked", self._on_delete_selected)
        box.append(self.delete_selected_button)

        delete = Gtk.Button(label="_Delete records", use_underline=True)
        delete.add_css_class("destructive-action")
        delete.set_tooltip_text("Delete all records, or a date range")
        delete.connect("clicked", self._on_delete_records)
        box.append(delete)

        box.append(Gtk.Box(hexpand=True))

        settings = Gtk.Button(label="_Settings", use_underline=True)
        settings.connect("clicked", lambda _button: self._open_settings())
        box.append(settings)

        return box

    # ------------------------------------------------------------------
    # Settings <-> controls
    # ------------------------------------------------------------------

    def _load_settings_into_controls(self) -> None:
        self._suppress_switch = True
        self.auto_switch.set_active(self.database.get_bool(SETTING_AUTO_ENABLED))
        self._suppress_switch = False

        minutes = self.database.get_int(SETTING_INTERVAL_MINUTES, 30)
        self._select_interval(minutes)

        self.order_dropdown.set_selected(
            0 if self.database.get_bool(SETTING_SORT_NEWEST_FIRST) else 1
        )

    def _select_interval(self, minutes: int) -> None:
        for index, (preset_minutes, _label) in enumerate(INTERVAL_PRESETS):
            if preset_minutes == minutes:
                self.interval_dropdown.set_selected(index)
                self.custom_box.set_visible(False)
                return

        self.interval_dropdown.set_selected(len(INTERVAL_PRESETS))
        self.custom_box.set_visible(True)
        if minutes % 60 == 0:
            self.custom_units.set_selected(1)
            self.custom_spin.set_value(minutes // 60)
        else:
            self.custom_units.set_selected(0)
            self.custom_spin.set_value(minutes)

    def _selected_interval_minutes(self) -> int:
        index = self.interval_dropdown.get_selected()
        if index < len(INTERVAL_PRESETS):
            return INTERVAL_PRESETS[index][0]
        amount = int(self.custom_spin.get_value())
        return amount * 60 if self.custom_units.get_selected() == 1 else amount

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _on_periodic_refresh(self) -> bool:
        self.refresh_all()
        return GLib.SOURCE_CONTINUE

    def refresh_all(self) -> None:
        self.refresh_table()
        self.refresh_status()

    def refresh_table(self) -> None:
        newest_first = self.order_dropdown.get_selected() == 0
        try:
            runs = self.database.list_runs(newest_first=newest_first)
        except DatabaseError as exc:
            self._show_banner(f"Cannot read the record database: {exc}", "details")
            return

        selected_ids = self._selected_run_ids()

        self.store.remove_all()
        for run in runs:
            self.store.append(RunObject(run))

        total = len(runs)
        self.record_count_label.set_text(
            "No tests recorded yet" if total == 0 else f"{total:,} record{'s' if total != 1 else ''}"
        )

        self._restore_selection(selected_ids)
        self._update_delete_selected_state()

    def refresh_status(self) -> None:
        try:
            status = self.scheduler.status()
        except SchedulerError as exc:
            log.warning("Scheduler status unavailable: %s", exc)
            self._show_banner(f"Scheduler error: {exc}", "details")
            return

        self._last_status = status
        self.next_test_label.set_text(f"Next test: {status.next_run_display()}")
        self.auto_state_label.set_text("On" if status.enabled else "Off")

        running = self.runner.is_running or (self._test_thread is not None and self._test_thread.is_alive())
        if running:
            state = self._state_text
        elif is_test_running():
            state = "Testing (started elsewhere)"
        elif not status.enabled:
            state = "Disabled"
        else:
            state = "Idle"
            latest = self.database.latest_run()
            if latest is not None and latest.status in {RunStatus.FAILED, RunStatus.TIMEOUT}:
                state = "Idle (last test failed)"

        self.state_label.set_text(f"Current state: {state}")
        self._update_banner(status)

    def _update_banner(self, status: SchedulerStatus) -> None:
        if not available_engines():
            self._show_banner(
                "No speed-test engine is installed, so tests cannot run yet.",
                "engine",
            )
            return
        if status.enabled and not status.supported:
            self._show_banner(
                "Scheduler error: background scheduling is not available on this system. "
                "Tests started with Test Now still work.",
                "details",
            )
            return
        if status.enabled and not status.timer_active:
            self._show_banner(
                "Scheduler error: automatic testing is switched on but the background "
                "timer is not running.",
                "details",
            )
            return
        self.banner.set_visible(False)

    def _show_banner(self, message: str, action: str) -> None:
        self.banner_label.set_text(message)
        self._banner_action = action
        self.banner_button.set_label("Set up engine" if action == "engine" else "Details")
        self.banner.set_visible(True)

    def _on_banner_action(self, _button: Gtk.Button) -> None:
        if self._banner_action == "engine":
            EngineSetupDialog(self, self.database).present()
            return
        detail = (self._last_status.detail if self._last_status else "") or (
            "No further detail is available. The diagnostic log at "
            f"{paths.log_path()} may contain more."
        )
        self._show_message("Scheduler details", detail)

    # ------------------------------------------------------------------
    # Scheduling controls
    # ------------------------------------------------------------------

    def _on_auto_switch(self, _switch: Gtk.Switch, requested: bool) -> bool:
        if self._suppress_switch:
            return False
        self._apply_schedule(enabled=requested)
        return False

    def _on_interval_choice(self, dropdown: Gtk.DropDown, _param: object) -> None:
        self.custom_box.set_visible(dropdown.get_selected() >= len(INTERVAL_PRESETS))

    def _on_apply_interval(self, _button: Gtk.Button) -> None:
        self._apply_schedule(enabled=self.auto_switch.get_active(), from_apply_button=True)

    def _apply_schedule(self, *, enabled: bool, from_apply_button: bool = False) -> None:
        """Push the current controls into the real timer.

        The interval always takes effect from now, which is what restarting
        the timer means -- there is no stale countdown left over from the
        previous setting.
        """
        try:
            minutes = validate_interval(self._selected_interval_minutes())
        except ValueError as exc:
            self._show_message("Interval not accepted", str(exc))
            self._suppress_switch = True
            self.auto_switch.set_active(self.database.get_bool(SETTING_AUTO_ENABLED))
            self._suppress_switch = False
            self._select_interval(self.database.get_int(SETTING_INTERVAL_MINUTES, 30))
            return

        try:
            status = self.scheduler.apply(
                enabled=enabled,
                interval_minutes=minutes,
                run_after_login=self.database.get_bool(SETTING_RUN_AFTER_LOGIN),
            )
        except (SchedulerError, DatabaseError) as exc:
            log.exception("Could not apply the schedule")
            self._show_message(
                "Scheduler error",
                f"The schedule could not be changed.\n\n{exc}",
            )
            self.refresh_status()
            return

        self._last_status = status
        if enabled and status.supported and status.timer_active:
            self._toast(
                f"Automatic testing is on, one test every {describe_interval(minutes)}. "
                f"Next test: {status.next_run_display()}."
            )
        elif from_apply_button and not enabled:
            # Applying an interval while the switch is off saves the setting
            # and schedules nothing. Silence here reads as "done", so say what
            # actually happened and what is still needed.
            self._show_message(
                "Interval saved, but automatic testing is off",
                f"The interval is now one test every {describe_interval(minutes)}.\n\n"
                "Automatic testing is switched off, so no tests are scheduled and "
                "nothing will run on its own. Switch Automatic testing on to start "
                "the schedule.",
            )
        self.refresh_status()

    # ------------------------------------------------------------------
    # Running a test
    # ------------------------------------------------------------------

    def _on_test_now(self, _button: Gtk.Button) -> None:
        if self._test_thread is not None and self._test_thread.is_alive():
            return

        if is_test_running():
            # A scheduled test is already in flight. Say so rather than
            # queueing a second one; the runner would record it as skipped.
            self._show_message(
                "A test is already running",
                "A test is already in progress, so a second one was not started. "
                "The result will appear in the table as soon as it finishes.",
            )
            return

        if not available_engines():
            EngineSetupDialog(self, self.database).present()
            return

        self.test_now_button.set_sensitive(False)
        self.progress.set_visible(True)
        self._pulse_source = GLib.timeout_add(120, self._pulse_progress)
        self._set_state("Testing")

        self._test_thread = threading.Thread(target=self._run_test_worker, daemon=True)
        self._test_thread.start()

    def _run_test_worker(self) -> None:
        """Runs off the main loop. Every UI touch goes back through idle_add."""
        outcome = self.runner.run_test(
            TriggerType.MANUAL,
            on_state=lambda state: GLib.idle_add(self._set_state, state),
        )
        GLib.idle_add(self._on_test_finished, outcome)

    def _on_test_finished(self, outcome) -> bool:  # type: ignore[no-untyped-def]
        self.test_now_button.set_sensitive(True)
        self.progress.set_visible(False)
        if getattr(self, "_pulse_source", None):
            GLib.source_remove(self._pulse_source)
            self._pulse_source = None
        self._test_thread = None
        self._set_state("Idle")

        if not outcome.stored:
            self._show_message(
                "The result could not be saved",
                "The test finished but its record could not be written to the "
                f"database.\n\n{outcome.save_error}\n\nThe full record was written to "
                f"the diagnostic log at {paths.log_path()}.",
            )
        else:
            run = outcome.run
            if run.status == RunStatus.SUCCESS:
                self._toast(
                    f"Test complete: {format_mbps(run.download_bps)} Mbps down, "
                    f"{format_mbps(run.upload_bps)} Mbps up, "
                    f"{format_ms(run.idle_latency_ms)} ms latency."
                )
            else:
                self._toast(f"{run.status_label()}: {summarise_error(run.error_category)}")

        self.refresh_all()
        return GLib.SOURCE_REMOVE

    def _pulse_progress(self) -> bool:
        self.progress.pulse()
        return GLib.SOURCE_CONTINUE

    def _set_state(self, state: str) -> bool:
        self._state_text = state
        self.state_label.set_text(f"Current state: {state}")
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Table interaction
    # ------------------------------------------------------------------

    def _selected_runs(self) -> list[TestRun]:
        """Every row currently selected, in table order."""
        selected: list[TestRun] = []
        bitset = self.selection.get_selection()
        for index in range(self.store.get_n_items()):
            if bitset.contains(index):
                item = self.store.get_item(index)
                if item is not None:
                    selected.append(item.run)
        return selected

    def _selected_run_ids(self) -> list[int]:
        return [run.id for run in self._selected_runs() if run.id is not None]

    def _restore_selection(self, run_ids: list[int]) -> None:
        """Re-select the same records after the table is rebuilt."""
        if not run_ids:
            return
        wanted = set(run_ids)
        for index in range(self.store.get_n_items()):
            item = self.store.get_item(index)
            if item is not None and item.run.id in wanted:
                self.selection.select_item(index, False)

    def _on_row_activated(self, _view: Gtk.ColumnView, position: int) -> None:
        item = self.store.get_item(position)
        if item is None:
            return
        stored = self.database.get_run(item.run.id) if item.run.id is not None else None
        DetailsDialog(self, stored or item.run).present()

    def _on_order_changed(self, dropdown: Gtk.DropDown, _param: object) -> None:
        self.database.set_bool(SETTING_SORT_NEWEST_FIRST, dropdown.get_selected() == 0)
        self.refresh_table()

    # ------------------------------------------------------------------
    # Bottom controls
    # ------------------------------------------------------------------

    def _on_export(self, _button: Gtk.Button) -> None:
        ExportDialog(self, self.database).present()

    def _on_open_data_folder(self, _button: Gtk.Button) -> None:
        folder = Gio.File.new_for_path(str(paths.data_dir()))
        launcher = Gtk.FileLauncher.new(folder)
        launcher.launch(self, None, self._on_folder_launched)

    def _on_folder_launched(self, launcher: Gtk.FileLauncher, result: Gio.AsyncResult) -> None:
        try:
            launcher.launch_finish(result)
        except GLib.Error as error:
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                self._show_message(
                    "Could not open the folder",
                    f"The data folder is at:\n\n{paths.data_dir()}\n\n{error.message}",
                )

    def _update_delete_selected_state(self) -> None:
        count = len(self._selected_run_ids())
        button = getattr(self, "delete_selected_button", None)
        if button is None:
            return
        button.set_sensitive(count > 0)
        button.set_label(
            "Delete se_lected" if count == 0 else f"Delete se_lected ({count})"
        )

    def _on_delete_records(self, _button: Gtk.Button) -> None:
        DeleteDialog(self, self.database, on_deleted=self.refresh_all).present()

    def _on_delete_selected(self, _button: Gtk.Button) -> None:
        """Delete exactly the rows the user picked.

        Same confirmation discipline as deleting a range: the count and the
        span are stated before anything goes, because this is the one action
        here that destroys history.
        """
        runs = self._selected_runs()
        if not runs:
            return

        self._pending_selected_ids = [run.id for run in runs if run.id is not None]
        count = len(self._pending_selected_ids)
        span = _describe_span(
            min(run.started_at_utc for run in runs),
            max(run.started_at_utc for run in runs),
        )

        confirm = Gtk.AlertDialog()
        confirm.set_modal(True)
        confirm.set_message(f"Delete {count:,} selected record{'s' if count != 1 else ''}?")
        confirm.set_detail(
            f"This will permanently delete the {count:,} record"
            f"{'s' if count != 1 else ''} you selected, covering {span}.\n\n"
            "This cannot be undone. Other records are not affected."
        )
        confirm.set_buttons(["Cancel", f"Delete {count:,}"])
        confirm.set_cancel_button(0)
        confirm.set_default_button(0)
        confirm.choose(self, None, self._on_delete_selected_confirmed)

    def _on_delete_selected_confirmed(
        self, dialog: Gtk.AlertDialog, result: Gio.AsyncResult
    ) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice != 1:
            return

        try:
            removed = self.database.delete_runs_by_id(self._pending_selected_ids)
        except DatabaseError as exc:
            self._show_message(
                "Records could not be deleted",
                f"Nothing was deleted.\n\n{exc}",
            )
            return

        self.selection.unselect_all()
        self.refresh_all()
        self._toast(f"{removed:,} record{'s' if removed != 1 else ''} deleted.")

    def _open_settings(self) -> None:
        SettingsDialog(self, self.database, on_saved=self._on_settings_saved).present()

    def _on_settings_saved(self) -> None:
        # "Run scheduler after login" changes a real systemd unit, so the
        # scheduler is re-applied rather than only recorded.
        try:
            self.scheduler.apply(
                enabled=self.database.get_bool(SETTING_AUTO_ENABLED),
                interval_minutes=self.database.get_int(SETTING_INTERVAL_MINUTES, 30),
                run_after_login=self.database.get_bool(SETTING_RUN_AFTER_LOGIN),
            )
        except (SchedulerError, DatabaseError) as exc:
            log.warning("Could not re-apply the schedule after saving settings: %s", exc)
        self.refresh_all()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _first_run_checks(self) -> bool:
        """Runs once after the window appears."""
        try:
            self.scheduler.record_missed_window()
        except DatabaseError as exc:
            log.warning("Could not record an interrupted scheduling window: %s", exc)

        if not available_engines():
            EngineSetupDialog(self, self.database).present()

        self.refresh_all()
        return GLib.SOURCE_REMOVE

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        """Closing the window never silently switches scheduling off.

        The first time it happens with automatic testing enabled, the
        behaviour is explained once; after that the window just closes.
        """
        if (
            self.database.get_bool(SETTING_AUTO_ENABLED)
            and not self.database.get_bool(SETTING_CLOSE_NOTICE_SHOWN)
        ):
            self.database.set_bool(SETTING_CLOSE_NOTICE_SHOWN, True)
            interval = describe_interval(self.database.get_int(SETTING_INTERVAL_MINUTES, 30))
            self._show_message(
                "Automatic testing continues",
                "Closing this window does not stop automatic testing. Tests will keep "
                f"running in the background every {interval} and will be recorded.\n\n"
                "To stop them, switch Automatic testing off before closing, or switch "
                "it off next time you open Bandwidth Logger.\n\n"
                "This message is shown only once.",
            )
        if getattr(self, "_refresh_source", None):
            GLib.source_remove(self._refresh_source)
            self._refresh_source = None
        return False

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _show_message(self, heading: str, body: str) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_modal(True)
        dialog.set_message(heading)
        dialog.set_detail(body)
        dialog.set_buttons(["Close"])
        dialog.show(self)

    def _toast(self, message: str) -> None:
        """Transient feedback, shown in the status line.

        Plain GTK has no toast widget, so the status line carries it and the
        next refresh replaces it.
        """
        self.state_label.set_text(message)

    def _show_about(self) -> None:
        about = Gtk.AboutDialog(transient_for=self, modal=True)
        about.set_program_name(APPLICATION_NAME)
        about.set_version(APPLICATION_VERSION)
        about.set_comments(
            "Runs internet speed tests on a schedule and keeps a complete, "
            "permanent record of every attempt on this computer."
        )
        about.set_license_type(Gtk.License.GPL_3_0)
        about.set_website("https://github.com/alekpeed/bandwidth")
        about.set_logo_icon_name("bandwidth-logger")
        about.present()


# ----------------------------------------------------------------------
# Table definition
# ----------------------------------------------------------------------


def _download_cell(run: TestRun) -> str:
    return format_mbps(run.download_bps)


def _upload_cell(run: TestRun) -> str:
    return format_mbps(run.upload_bps)


def _server_cell(run: TestRun) -> str:
    if run.server_name and run.server_city:
        return f"{run.server_name} ({run.server_city})"
    return run.server_name or run.server_city or ""


def _connection_cell(run: TestRun) -> str:
    if run.interface_name and run.connection_type:
        return f"{run.connection_type} ({run.interface_name})"
    return run.connection_type or run.interface_name or ""


#: ``(heading, accessor, minimum width in characters, right-aligned)``.
#: Units are in the headings so no number in the table is ambiguous.
_TABLE_COLUMNS: tuple[tuple[str, "object", int, bool], ...] = (
    ("Local date and time", lambda run: format_for_display(run.started_at_local), 19, False),
    ("Status", lambda run: run.status_label(), 10, False),
    ("Download (Mbps)", _download_cell, 14, True),
    ("Upload (Mbps)", _upload_cell, 12, True),
    ("Ping (ms)", lambda run: format_ms(run.idle_latency_ms), 10, True),
    ("Server", _server_cell, 22, False),
    ("ISP", lambda run: run.isp_name or "", 18, False),
    ("Connection", _connection_cell, 18, False),
    ("Duration", lambda run: format_duration(run.duration_ms), 10, True),
)


def _build_column(title: str, accessor, width: int, numeric: bool) -> Gtk.ColumnViewColumn:  # type: ignore[no-untyped-def]
    factory = Gtk.SignalListItemFactory()

    def on_setup(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        label = Gtk.Label(xalign=1.0 if numeric else 0.0)
        label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        label.set_margin_start(6)
        label.set_margin_end(6)
        item.set_child(label)

    def on_bind(_factory: Gtk.SignalListItemFactory, item: Gtk.ListItem) -> None:
        label = item.get_child()
        run = item.get_item().run
        text = accessor(run)
        label.set_text(text)
        # Blank cells are common and meaningful (the value was not measured);
        # the tooltip spells that out rather than leaving an unexplained gap.
        label.set_tooltip_text(text or f"{title}: not measured")

    factory.connect("setup", on_setup)
    factory.connect("bind", on_bind)

    column = Gtk.ColumnViewColumn.new(title, factory)
    column.set_resizable(True)
    column.set_expand(not numeric)
    return column


# ----------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------


class ExportDialog(Gtk.Window):
    """Choose what to export, then where to save it."""

    def __init__(self, parent: MainWindow, database: Database) -> None:
        super().__init__(
            title="Export records to CSV",
            transient_for=parent,
            modal=True,
            default_width=520,
        )
        self.parent_window = parent
        self.database = database

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _button: self.close())
        header.pack_start(cancel)
        self.export_button = Gtk.Button(label="Choose file and export")
        self.export_button.add_css_class("suggested-action")
        self.export_button.connect("clicked", self._on_choose_file)
        header.pack_end(self.export_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        self.set_child(content)

        self.all_records_check = Gtk.CheckButton(label="Export all records")
        self.all_records_check.set_active(True)
        self.all_records_check.connect("toggled", self._on_scope_toggled)
        content.append(self.all_records_check)

        self.range_grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.range_grid.set_margin_start(24)
        self.range_grid.set_sensitive(False)

        from_label = Gtk.Label(label="_From date", use_underline=True, xalign=0.0)
        self.from_entry = Gtk.Entry()
        self.from_entry.set_placeholder_text("YYYY-MM-DD")
        self.from_entry.update_property([Gtk.AccessibleProperty.LABEL], ["From date"])
        from_label.set_mnemonic_widget(self.from_entry)

        to_label = Gtk.Label(label="_To date", use_underline=True, xalign=0.0)
        self.to_entry = Gtk.Entry()
        self.to_entry.set_placeholder_text("YYYY-MM-DD")
        self.to_entry.update_property([Gtk.AccessibleProperty.LABEL], ["To date"])
        to_label.set_mnemonic_widget(self.to_entry)

        self.range_grid.attach(from_label, 0, 0, 1, 1)
        self.range_grid.attach(self.from_entry, 1, 0, 1, 1)
        self.range_grid.attach(to_label, 0, 1, 1, 1)
        self.range_grid.attach(self.to_entry, 1, 1, 1, 1)
        content.append(self.range_grid)

        hint = Gtk.Label(
            label="Dates are inclusive whole days in local time.",
            xalign=0.0,
        )
        hint.add_css_class("dim-label")
        hint.set_margin_start(24)
        content.append(hint)

        content.append(Gtk.Separator())

        self.include_ip_check = Gtk.CheckButton(label="Include external IP address")
        self.include_ip_check.set_active(database.get_bool(SETTING_EXPORT_INCLUDE_IP))
        content.append(self.include_ip_check)

        note = Gtk.Label(
            label=(
                "Failed, timed-out and skipped attempts are always included, so the "
                "exported file matches the table exactly."
            ),
            xalign=0.0,
            wrap=True,
        )
        note.add_css_class("dim-label")
        content.append(note)

    def _on_scope_toggled(self, check: Gtk.CheckButton) -> None:
        self.range_grid.set_sensitive(not check.get_active())

    def _parse_dates(self) -> tuple[date | None, date | None] | None:
        if self.all_records_check.get_active():
            return (None, None)
        try:
            start = _parse_date(self.from_entry.get_text())
            end = _parse_date(self.to_entry.get_text())
        except ValueError as exc:
            self.parent_window._show_message("Date not understood", str(exc))
            return None
        if start and end and start > end:
            self.parent_window._show_message(
                "Date range not understood",
                "The From date is later than the To date.",
            )
            return None
        return (start, end)

    def _on_choose_file(self, _button: Gtk.Button) -> None:
        parsed = self._parse_dates()
        if parsed is None:
            return
        self._range = parsed

        dialog = Gtk.FileDialog()
        dialog.set_title("Save exported records")
        dialog.set_initial_name(default_filename())

        csv_filter = Gtk.FileFilter()
        csv_filter.set_name("CSV files")
        csv_filter.add_pattern("*.csv")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(csv_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(csv_filter)

        dialog.save(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            file = dialog.save_finish(result)
        except GLib.Error as error:
            if not error.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                self.parent_window._show_message("Export cancelled", error.message)
            return
        if file is None:
            return

        destination = file.get_path()
        start, end = self._range
        include_ip = self.include_ip_check.get_active()
        self.database.set_bool(SETTING_EXPORT_INCLUDE_IP, include_ip)

        try:
            rows = export_database(
                self.database,
                destination,
                start_date=start,
                end_date=end,
                include_external_ip=include_ip,
            )
        except (OSError, DatabaseError) as exc:
            self.parent_window._show_message(
                "Export failed",
                f"The file could not be written.\n\n{exc}",
            )
            return

        self.close()
        self.parent_window._show_message(
            "Export complete",
            f"{rows} record{'s' if rows != 1 else ''} exported to:\n\n{destination}"
            + ("" if include_ip else "\n\nThe external IP address column was excluded."),
        )


def _parse_date(text: str) -> date | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"'{text}' is not a date in YYYY-MM-DD form, for example 2026-08-04."
        ) from exc


# ----------------------------------------------------------------------
# Deletion
# ----------------------------------------------------------------------


class DeleteDialog(Gtk.Window):
    """Deletion, behind an explicit confirmation.

    The confirmation states how many records will go and the dates they span,
    because deletion is the one action here that destroys history and cannot
    be undone. Nothing deletes records automatically; there is no retention
    limit.
    """

    def __init__(self, parent: MainWindow, database: Database, *, on_deleted) -> None:  # type: ignore[no-untyped-def]
        super().__init__(
            title="Delete records",
            transient_for=parent,
            modal=True,
            default_width=520,
        )
        self.parent_window = parent
        self.database = database
        self._on_deleted = on_deleted

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _button: self.close())
        header.pack_start(cancel)
        self.delete_button = Gtk.Button(label="Delete...")
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self._on_delete_clicked)
        header.pack_end(self.delete_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        self.set_child(content)

        self.all_check = Gtk.CheckButton(label="Delete all records")
        self.all_check.set_active(True)
        self.all_check.connect("toggled", self._on_scope_toggled)
        content.append(self.all_check)

        self.range_grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.range_grid.set_margin_start(24)
        self.range_grid.set_sensitive(False)

        from_label = Gtk.Label(label="_From date", use_underline=True, xalign=0.0)
        self.from_entry = Gtk.Entry()
        self.from_entry.set_placeholder_text("YYYY-MM-DD")
        self.from_entry.update_property([Gtk.AccessibleProperty.LABEL], ["From date"])
        from_label.set_mnemonic_widget(self.from_entry)

        to_label = Gtk.Label(label="_To date", use_underline=True, xalign=0.0)
        self.to_entry = Gtk.Entry()
        self.to_entry.set_placeholder_text("YYYY-MM-DD")
        self.to_entry.update_property([Gtk.AccessibleProperty.LABEL], ["To date"])
        to_label.set_mnemonic_widget(self.to_entry)

        self.range_grid.attach(from_label, 0, 0, 1, 1)
        self.range_grid.attach(self.from_entry, 1, 0, 1, 1)
        self.range_grid.attach(to_label, 0, 1, 1, 1)
        self.range_grid.attach(self.to_entry, 1, 1, 1, 1)
        content.append(self.range_grid)

        warning = Gtk.Label(
            label=(
                "Deleted records cannot be recovered. Consider exporting to CSV first. "
                "Bandwidth Logger never deletes records on its own."
            ),
            xalign=0.0,
            wrap=True,
        )
        content.append(warning)

    def _on_scope_toggled(self, check: Gtk.CheckButton) -> None:
        self.range_grid.set_sensitive(not check.get_active())

    def _on_delete_clicked(self, _button: Gtk.Button) -> None:
        if self.all_check.get_active():
            start_utc = end_utc = None
            scope = "all records"
        else:
            try:
                start = _parse_date(self.from_entry.get_text())
                end = _parse_date(self.to_entry.get_text())
            except ValueError as exc:
                self.parent_window._show_message("Date not understood", str(exc))
                return
            if start and end and start > end:
                self.parent_window._show_message(
                    "Date range not understood",
                    "The From date is later than the To date.",
                )
                return
            start_utc = local_day_start_utc(start) if start else None
            end_utc = local_day_end_utc(end) if end else None
            scope = "records in the selected date range"

        count = self.database.count_runs(start_utc=start_utc, end_utc=end_utc)
        if count == 0:
            self.parent_window._show_message(
                "Nothing to delete",
                f"There are no {scope} to delete.",
            )
            return

        earliest, latest = self.database.run_date_range(start_utc=start_utc, end_utc=end_utc)
        span = _describe_span(earliest, latest)

        confirm = Gtk.AlertDialog()
        confirm.set_modal(True)
        confirm.set_message(f"Delete {count:,} record{'s' if count != 1 else ''}?")
        confirm.set_detail(
            f"This will permanently delete {count:,} record"
            f"{'s' if count != 1 else ''} covering {span}.\n\n"
            "This cannot be undone."
        )
        confirm.set_buttons(["Cancel", f"Delete {count:,} record{'s' if count != 1 else ''}"])
        confirm.set_cancel_button(0)
        confirm.set_default_button(0)
        self._pending = (start_utc, end_utc, count)
        confirm.choose(self, None, self._on_confirmed)

    def _on_confirmed(self, dialog: Gtk.AlertDialog, result: Gio.AsyncResult) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice != 1:
            return

        start_utc, end_utc, expected = self._pending
        try:
            removed = self.database.delete_runs(start_utc=start_utc, end_utc=end_utc)
        except DatabaseError as exc:
            self.parent_window._show_message(
                "Records could not be deleted",
                f"Nothing was deleted.\n\n{exc}",
            )
            return

        self.close()
        self._on_deleted()
        self.parent_window._show_message(
            "Records deleted",
            f"{removed:,} record{'s' if removed != 1 else ''} deleted.",
        )


def _describe_span(earliest: str | None, latest: str | None) -> str:
    start = parse_iso(earliest)
    end = parse_iso(latest)
    if start is None or end is None:
        return "an unknown date range"
    start_text = start.astimezone().strftime("%Y-%m-%d %H:%M")
    end_text = end.astimezone().strftime("%Y-%m-%d %H:%M")
    if start_text == end_text:
        return f"{start_text}"
    return f"{start_text} to {end_text}"


__all__ = ["DeleteDialog", "ExportDialog", "MainWindow", "RunObject"]
