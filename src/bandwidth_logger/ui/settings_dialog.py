"""Settings, and the first-run engine setup it shares.

Only settings that a person has a reason to change are here. The engine
timeout is deliberately at the bottom under "Advanced" rather than in the
main window, because it is an internal safeguard and not a normal control.
"""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gio, Gtk  # noqa: E402

from ..core.test_runner import DEFAULT_TIMEOUT_SECONDS  # noqa: E402
from ..engines import known_engines  # noqa: E402
from ..engines.registry import AUTOMATIC  # noqa: E402
from ..storage.database import (  # noqa: E402
    SETTING_ENGINE_NAME,
    SETTING_EXPORT_INCLUDE_IP,
    SETTING_RUN_AFTER_LOGIN,
    SETTING_TIMEOUT_SECONDS,
    Database,
)
from ..system import paths  # noqa: E402


def _labelled_row(label_text: str, control: Gtk.Widget, description: str | None = None) -> Gtk.Widget:
    """One settings row: a text label, an optional explanation, a control.

    Every control gets a real label rather than an icon alone, and the label
    is wired to the control so a screen reader announces the pair.
    """
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)

    label = Gtk.Label(label=label_text, xalign=0.0)
    label.set_mnemonic_widget(control)
    text_box.append(label)

    if description:
        detail = Gtk.Label(label=description, xalign=0.0, wrap=True)
        detail.add_css_class("dim-label")
        detail.add_css_class("caption")
        text_box.append(detail)

    row.append(text_box)
    control.set_valign(Gtk.Align.CENTER)
    row.append(control)

    control.update_property([Gtk.AccessibleProperty.LABEL], [label_text])
    return row


class SettingsDialog(Gtk.Window):
    """Application settings. Changes are saved when Save is pressed."""

    def __init__(
        self,
        parent: Gtk.Window,
        database: Database,
        *,
        on_saved: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            title="Bandwidth Logger Settings",
            transient_for=parent,
            modal=True,
            default_width=560,
            default_height=560,
        )
        self.database = database
        self._on_saved = on_saved

        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _button: self.close())
        header.pack_start(cancel)

        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save)
        header.pack_end(save)

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_child(scroller)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        scroller.set_child(content)

        content.append(self._build_engine_section())
        content.append(self._build_startup_section())
        content.append(self._build_privacy_section())
        content.append(self._build_storage_section())
        content.append(self._build_advanced_section())

    # -- sections ----------------------------------------------------------

    @staticmethod
    def _section(title: str) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        heading = Gtk.Label(label=title, xalign=0.0)
        heading.add_css_class("heading")
        box.append(heading)
        return box

    def _build_engine_section(self) -> Gtk.Widget:
        box = self._section("Speed-test engine")

        self._engines = known_engines()
        names = ["Choose automatically"] + [engine.display_name for engine in self._engines]
        self._engine_ids = [AUTOMATIC] + [engine.name for engine in self._engines]

        self.engine_dropdown = Gtk.DropDown.new_from_strings(names)
        current = self.database.get_setting(SETTING_ENGINE_NAME) or AUTOMATIC
        if current in self._engine_ids:
            self.engine_dropdown.set_selected(self._engine_ids.index(current))

        box.append(
            _labelled_row(
                "_Engine to use",
                self.engine_dropdown,
                "The Ookla Speedtest CLI is the only supported engine \u2014 the same one "
                "speedtest.net uses. Every result records which engine measured it.",
            )
        )

        for engine in self._engines:
            installed = engine.is_available()
            version = engine.version() if installed else None
            status = Gtk.Label(
                label=(
                    f"{engine.display_name}: installed"
                    + (f" (version {version})" if version else "")
                    if installed
                    else f"{engine.display_name}: not installed"
                ),
                xalign=0.0,
                wrap=True,
            )
            status.add_css_class("dim-label")
            box.append(status)

        help_button = Gtk.Button(label="How to install an engine")
        help_button.set_halign(Gtk.Align.START)
        help_button.connect(
            "clicked", lambda _button: EngineSetupDialog(self, self.database).present()
        )
        box.append(help_button)
        return box

    def _build_startup_section(self) -> Gtk.Widget:
        box = self._section("Background operation")

        self.login_switch = Gtk.Switch()
        self.login_switch.set_active(self.database.get_bool(SETTING_RUN_AFTER_LOGIN))
        box.append(
            _labelled_row(
                "Run scheduler after _login",
                self.login_switch,
                "Starts the background timer automatically when you log in, so "
                "automatic testing resumes after a restart without opening this window.",
            )
        )
        return box

    def _build_privacy_section(self) -> Gtk.Widget:
        box = self._section("Privacy")

        self.include_ip_switch = Gtk.Switch()
        self.include_ip_switch.set_active(self.database.get_bool(SETTING_EXPORT_INCLUDE_IP))
        box.append(
            _labelled_row(
                "Include external _IP address in exports",
                self.include_ip_switch,
                "Off by default. The address is still stored with each record and shown "
                "in the record details; this only controls whether it appears in "
                "exported CSV files.",
            )
        )

        note = Gtk.Label(
            label=(
                "Bandwidth Logger keeps everything on this computer. It sends nothing "
                "anywhere except the speed test itself, which necessarily talks to a "
                "test server. It collects no analytics and stores no passwords."
            ),
            xalign=0.0,
            wrap=True,
        )
        note.add_css_class("dim-label")
        box.append(note)
        return box

    def _build_storage_section(self) -> Gtk.Widget:
        box = self._section("Where your records are kept")

        for label_text, path in (
            ("Database", paths.database_path()),
            ("Diagnostic log", paths.log_path()),
        ):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            name = Gtk.Label(label=label_text, xalign=0.0)
            value = Gtk.Label(label=str(path), xalign=0.0, wrap=True, selectable=True)
            value.add_css_class("dim-label")
            value.add_css_class("monospace")
            row.append(name)
            row.append(value)
            box.append(row)

        note = Gtk.Label(
            label=(
                "Removing the Bandwidth Logger package does not delete these files. "
                "Delete the folders above if you want the history gone."
            ),
            xalign=0.0,
            wrap=True,
        )
        note.add_css_class("dim-label")
        box.append(note)
        return box

    def _build_advanced_section(self) -> Gtk.Widget:
        box = self._section("Advanced")

        self.timeout_spin = Gtk.SpinButton.new_with_range(30, 3600, 10)
        self.timeout_spin.set_value(
            self.database.get_int(SETTING_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS)
        )
        box.append(
            _labelled_row(
                "Engine _timeout (seconds)",
                self.timeout_spin,
                "How long a single test may take before it is stopped and recorded as "
                "timed out. The default of 180 seconds suits almost every connection.",
            )
        )
        return box

    # -- saving ------------------------------------------------------------

    def _on_save(self, _button: Gtk.Button) -> None:
        selected = self.engine_dropdown.get_selected()
        if 0 <= selected < len(self._engine_ids):
            self.database.set_setting(SETTING_ENGINE_NAME, self._engine_ids[selected])

        self.database.set_bool(SETTING_RUN_AFTER_LOGIN, self.login_switch.get_active())
        self.database.set_bool(SETTING_EXPORT_INCLUDE_IP, self.include_ip_switch.get_active())
        self.database.set_int(SETTING_TIMEOUT_SECONDS, int(self.timeout_spin.get_value()))

        if self._on_saved is not None:
            self._on_saved()
        self.close()


class EngineSetupDialog(Gtk.Window):
    """Explains how to install a speed-test engine.

    Shown on first run when nothing is installed, and reachable from Settings
    afterwards. It states plainly that the Ookla CLI is Ookla's software and
    is not part of this package, rather than presenting it as a component
    that failed to install.
    """

    def __init__(self, parent: Gtk.Window, database: Database) -> None:
        super().__init__(
            title="Set up a speed-test engine",
            transient_for=parent,
            modal=True,
            default_width=620,
            default_height=620,
        )
        self.database = database

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda _button: self.close())
        header.pack_end(close)

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_child(scroller)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        scroller.set_child(content)

        intro = Gtk.Label(
            label=(
                "Bandwidth Logger measures your connection using a separate speed-test "
                "program. At least one of the following has to be installed."
            ),
            xalign=0.0,
            wrap=True,
        )
        content.append(intro)

        for engine in known_engines():
            content.append(self._engine_card(engine))

    def _engine_card(self, engine) -> Gtk.Widget:  # type: ignore[no-untyped-def]
        guidance = engine.install_guidance()
        installed = engine.is_available()

        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        frame.set_child(box)

        title = Gtk.Label(label=engine.display_name, xalign=0.0)
        title.add_css_class("heading")
        box.append(title)

        state = Gtk.Label(
            label="Installed and ready" if installed else "Not installed",
            xalign=0.0,
        )
        state.add_css_class("dim-label")
        box.append(state)

        if not installed:
            detail = Gtk.Label(label=guidance.detail, xalign=0.0, wrap=True)
            box.append(detail)

            if guidance.commands:
                commands = "\n".join(guidance.commands)
                view = Gtk.TextView()
                view.set_editable(False)
                view.set_monospace(True)
                view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
                view.get_buffer().set_text(commands)
                command_frame = Gtk.Frame()
                command_frame.set_child(view)
                box.append(command_frame)

                buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                copy = Gtk.Button(label="Copy commands")
                copy.connect("clicked", lambda _b, text=commands: self._copy(text))
                buttons.append(copy)

                terminal = Gtk.Button(label="Open a terminal here")
                terminal.set_tooltip_text(
                    "Opens your terminal application so the commands can be pasted in"
                )
                terminal.connect("clicked", lambda _b: self._open_terminal())
                buttons.append(terminal)

                if guidance.url:
                    website = Gtk.Button(label="Open the engine's website")
                    website.connect("clicked", lambda _b, url=guidance.url: self._open_uri(url))
                    buttons.append(website)

                box.append(buttons)

        return frame

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _copy(text: str) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(text)

    @staticmethod
    def _open_uri(uri: str) -> None:
        Gtk.UriLauncher.new(uri).launch(None, None, None, None)

    @staticmethod
    def _open_terminal() -> None:
        """Launch the desktop's terminal application.

        Installing system software genuinely needs a terminal and an
        administrator password; this only saves the user from having to find
        the terminal themselves. Nothing in normal operation needs either.
        """
        for candidate in (
            "org.gnome.Terminal.desktop",
            "gnome-terminal.desktop",
            "org.gnome.Console.desktop",
            "kgx.desktop",
            "xterm.desktop",
        ):
            launcher = Gio.DesktopAppInfo.new(candidate)
            if launcher is not None:
                try:
                    launcher.launch([], None)
                    return
                except Exception:  # noqa: BLE001 - try the next candidate
                    continue
