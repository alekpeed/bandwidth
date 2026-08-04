"""The complete stored record for one test.

Selecting a row in the log table opens this. It shows every field that was
stored, including the raw engine output, because the point of keeping that
much detail is being able to look at it later.

Fields that were never measured are shown as "Not measured" rather than as a
blank or a zero, so the table cannot be misread as reporting a real value of
nought.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, Gtk  # noqa: E402

from ..core.models import (  # noqa: E402
    TestRun,
    format_duration,
    format_ms,
    summarise_error,
)

NOT_MEASURED = "Not measured"


def _text(value: object, suffix: str = "") -> str:
    if value is None or value == "":
        return NOT_MEASURED
    return f"{value}{suffix}"


class DetailsDialog(Gtk.Window):
    """A read-only view of one complete ``test_runs`` row."""

    def __init__(self, parent: Gtk.Window, run: TestRun) -> None:
        super().__init__(
            title=f"Test record {run.id} - {run.status_label()}",
            transient_for=parent,
            modal=True,
            default_width=640,
            default_height=720,
        )
        self.run = run

        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        copy_button = Gtk.Button(label="Copy details")
        copy_button.set_tooltip_text("Copy this record to the clipboard as text")
        copy_button.connect("clicked", self._on_copy)
        header.pack_start(copy_button)

        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda _button: self.close())
        header.pack_end(close_button)

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_child(scroller)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        scroller.set_child(content)

        for title, rows in self._sections():
            content.append(self._build_section(title, rows))

        if run.error_message:
            content.append(self._build_text_block("Full error message", run.error_message))
        if run.raw_result_json:
            content.append(
                self._build_text_block(
                    "Raw engine result",
                    run.raw_result_json,
                    monospace=True,
                )
            )

    # -- content -----------------------------------------------------------

    def _sections(self) -> list[tuple[str, list[tuple[str, str]]]]:
        run = self.run
        outcome: list[tuple[str, str]] = [
            ("Record number", str(run.id) if run.id is not None else NOT_MEASURED),
            ("Status", run.status_label()),
            ("Started by", _trigger_label(run.trigger_type)),
        ]
        if run.error_category:
            outcome.append(("Error category", run.error_category))
            outcome.append(("Error summary", summarise_error(run.error_category)))

        timing = [
            ("Scheduled for (UTC)", _text(run.scheduled_at_utc)),
            ("Started (local time)", _text(run.started_at_local)),
            ("Started (UTC)", _text(run.started_at_utc)),
            ("Completed (local time)", _text(run.completed_at_local)),
            ("Completed (UTC)", _text(run.completed_at_utc)),
            ("Duration", format_duration(run.duration_ms) or NOT_MEASURED),
            ("Recorded (UTC)", _text(run.created_at_utc)),
        ]

        measurements = [
            ("Download", _mbps_row(run.download_bps)),
            ("Upload", _mbps_row(run.upload_bps)),
            ("Idle latency (ping)", _ms_row(run.idle_latency_ms)),
            ("Latency during download", _ms_row(run.download_latency_ms)),
            ("Latency during upload", _ms_row(run.upload_latency_ms)),
            ("Jitter", _ms_row(run.jitter_ms)),
            (
                "Packet loss",
                NOT_MEASURED
                if run.packet_loss_percent is None
                else f"{run.packet_loss_percent:g} %",
            ),
        ]

        server = [
            ("Server name", _text(run.server_name)),
            ("Server ID", _text(run.server_id)),
            ("Server host", _text(run.server_host)),
            ("Server city", _text(run.server_city)),
            ("Server region", _text(run.server_region)),
            ("Server country", _text(run.server_country)),
        ]

        connection = [
            ("Internet provider (ISP)", _text(run.isp_name)),
            ("External IP address", _text(run.external_ip)),
            ("Local interface", _text(run.interface_name)),
            ("Connection type", _text(run.connection_type)),
        ]

        software = [
            ("Test engine", _text(run.engine_name)),
            ("Engine version", _text(run.engine_version)),
            ("Bandwidth Logger version", _text(run.application_version)),
        ]

        return [
            ("Outcome", outcome),
            ("Timing", timing),
            ("Measurements", measurements),
            ("Test server", server),
            ("Connection", connection),
            ("Software", software),
        ]

    def _build_section(self, title: str, rows: list[tuple[str, str]]) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        heading = Gtk.Label(label=title, xalign=0.0)
        heading.add_css_class("heading")
        box.append(heading)

        grid = Gtk.Grid(column_spacing=18, row_spacing=6)
        grid.set_margin_start(6)
        for index, (label_text, value_text) in enumerate(rows):
            name = Gtk.Label(label=label_text, xalign=0.0)
            name.add_css_class("dim-label")
            name.set_width_chars(24)
            value = Gtk.Label(label=value_text, xalign=0.0)
            value.set_selectable(True)
            value.set_wrap(True)
            value.set_hexpand(True)
            grid.attach(name, 0, index, 1, 1)
            grid.attach(value, 1, index, 1, 1)
        box.append(grid)
        return box

    def _build_text_block(self, title: str, body: str, *, monospace: bool = False) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        heading = Gtk.Label(label=title, xalign=0.0)
        heading.add_css_class("heading")
        box.append(heading)

        view = Gtk.TextView()
        view.set_editable(False)
        view.set_monospace(monospace)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(body)
        view.set_accessible_role(Gtk.AccessibleRole.LOG)

        frame = Gtk.Frame()
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(140)
        scroller.set_child(view)
        frame.set_child(scroller)
        box.append(frame)
        return box

    # -- actions -----------------------------------------------------------

    def _on_copy(self, _button: Gtk.Button) -> None:
        lines: list[str] = []
        for title, rows in self._sections():
            lines.append(title)
            lines.extend(f"  {name}: {value}" for name, value in rows)
            lines.append("")
        if self.run.error_message:
            lines.extend(["Full error message", self.run.error_message, ""])
        if self.run.raw_result_json:
            lines.extend(["Raw engine result", self.run.raw_result_json])

        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set("\n".join(lines))


def _trigger_label(trigger_type: str) -> str:
    return {
        "manual": "Manually (Test Now)",
        "scheduled": "Automatically (scheduled)",
        "startup_recovery": "Scheduling interruption note",
        "retry": "Automatic retry",
    }.get(trigger_type, trigger_type)


def _mbps_row(bits_per_second: int | None) -> str:
    if bits_per_second is None:
        return NOT_MEASURED
    mbps = bits_per_second / 1_000_000
    return f"{mbps:.2f} Mbps ({bits_per_second:,} bits per second)"


def _ms_row(milliseconds: float | None) -> str:
    if milliseconds is None:
        return NOT_MEASURED
    return f"{format_ms(milliseconds)} ms"
