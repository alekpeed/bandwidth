"""Data model shared by the runner, the storage layer and the interface.

Nothing here imports GTK: every rule expressed in this module is testable
without opening a window.

A note on missing data, which the specification treats as load-bearing: an
unknown measurement is ``None`` and is stored as SQL NULL. Zero is a real
measurement -- a connection that transferred nothing still measured
something -- so the two are never conflated.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

BITS_PER_MEGABIT = 1_000_000


class TriggerType(StrEnum):
    """Why a test was attempted."""

    MANUAL = "manual"
    SCHEDULED = "scheduled"
    STARTUP_RECOVERY = "startup_recovery"
    RETRY = "retry"


class RunStatus(StrEnum):
    """Outcome of an attempted test. Every attempt gets one of these."""

    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class ErrorCategory(StrEnum):
    """Normalised failure reasons, stable across engines."""

    ENGINE_MISSING = "engine_missing"
    NO_NETWORK = "no_network"
    DNS_FAILURE = "dns_failure"
    SERVER_UNAVAILABLE = "server_unavailable"
    TIMEOUT = "timeout"
    PERMISSION_ERROR = "permission_error"
    MALFORMED_OUTPUT = "malformed_output"
    PROCESS_ERROR = "process_error"
    DATABASE_ERROR = "database_error"
    SKIPPED_OVERLAP = "skipped_overlap"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


#: Short, non-technical wording for the status area. The stored record always
#: keeps the engine's complete message; this is only what the window shows.
ERROR_SUMMARIES: dict[str, str] = {
    ErrorCategory.ENGINE_MISSING: "Speed-test engine is not installed",
    ErrorCategory.NO_NETWORK: "No network connection",
    ErrorCategory.DNS_FAILURE: "Could not resolve the test server address",
    ErrorCategory.SERVER_UNAVAILABLE: "No test server was reachable",
    ErrorCategory.TIMEOUT: "The test took too long and was stopped",
    ErrorCategory.PERMISSION_ERROR: "The engine was not permitted to run",
    ErrorCategory.MALFORMED_OUTPUT: "The engine returned a result that could not be read",
    ErrorCategory.PROCESS_ERROR: "The engine stopped unexpectedly",
    ErrorCategory.DATABASE_ERROR: "The result could not be saved",
    ErrorCategory.SKIPPED_OVERLAP: "Skipped: previous test still running",
    ErrorCategory.CANCELLED: "The test was cancelled",
    ErrorCategory.UNKNOWN: "The test failed for an unrecognised reason",
}


#: Notes attached to results from engines no longer used. Records are never
#: rewritten or deleted, so a measurement taken by a retired engine keeps its
#: number and gains the context needed to read it correctly.
ENGINE_CAVEATS: dict[str, str] = {
    "speedtest-cli": (
        "This result was measured by speedtest-cli, the fallback engine included in "
        "version 1.0.0 and removed in 1.1.0. On connections faster than about "
        "300 Mbps that engine reported well below the real speed \u2014 sometimes only "
        "half of it \u2014 because it could not open enough connections at once to fill "
        "a fast line. If this figure looks too low, that is why. Results measured by "
        "the Ookla engine are not affected."
    ),
}


def engine_caveat(engine_name: str | None) -> str | None:
    """Context needed to read a result from a retired engine, if any."""
    return ENGINE_CAVEATS.get(engine_name or "")


def summarise_error(category: str | None) -> str:
    """One short sentence suitable for the status area."""
    if not category:
        return "The test failed"
    return ERROR_SUMMARIES.get(category, ERROR_SUMMARIES[ErrorCategory.UNKNOWN])


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------


def utc_now() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_utc_iso(moment: datetime) -> str:
    """ISO 8601 in UTC, e.g. ``2026-08-04T09:15:00.123456+00:00``.

    A naive datetime is assumed to be local time, matching what
    ``datetime.now()`` returns, rather than being silently labelled UTC.
    """
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc).isoformat()


def to_local_iso(moment: datetime) -> str:
    """ISO 8601 in the local zone, carrying the offset in effect at *that*
    instant -- so records either side of a daylight-saving change keep the
    offset that was actually true when the test ran.
    """
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone().isoformat()


def parse_iso(value: str | None) -> datetime | None:
    """Parse a stored ISO 8601 timestamp back into an aware datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_for_display(value: str | None) -> str:
    """Render a stored timestamp as unambiguous local date and time."""
    moment = parse_iso(value)
    if moment is None:
        return ""
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


def bps_to_mbps(bits_per_second: int | float | None) -> float | None:
    """Convert bits per second to megabits per second.

    Returns ``None`` for missing input so that "not measured" survives the
    conversion; ``0`` converts to ``0.0``.
    """
    if bits_per_second is None:
        return None
    return round(float(bits_per_second) / BITS_PER_MEGABIT, 2)


def bytes_per_second_to_bps(bytes_per_second: int | float | None) -> int | None:
    """Convert the engine's bytes-per-second figure to bits per second."""
    if bytes_per_second is None:
        return None
    return int(round(float(bytes_per_second) * 8))


def format_mbps(bits_per_second: int | float | None) -> str:
    """Display string for a bandwidth column. Blank when not measured."""
    mbps = bps_to_mbps(bits_per_second)
    if mbps is None:
        return ""
    return f"{mbps:.2f}"


def format_ms(milliseconds: float | None) -> str:
    """Display string for a latency column. Blank when not measured."""
    if milliseconds is None:
        return ""
    return f"{milliseconds:.2f}"


def format_duration(duration_ms: int | None) -> str:
    """Human-readable duration, e.g. ``12.4 s``. Blank when not recorded."""
    if duration_ms is None:
        return ""
    seconds = duration_ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)} m {remainder:.0f} s"


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

#: Column order of ``test_runs``, reused by the storage layer and CSV export
#: so the three never drift apart.
TEST_RUN_FIELDS: tuple[str, ...] = (
    "trigger_type",
    "scheduled_at_utc",
    "started_at_utc",
    "started_at_local",
    "completed_at_utc",
    "completed_at_local",
    "duration_ms",
    "status",
    "error_category",
    "error_message",
    "download_bps",
    "upload_bps",
    "idle_latency_ms",
    "download_latency_ms",
    "upload_latency_ms",
    "jitter_ms",
    "packet_loss_percent",
    "server_id",
    "server_name",
    "server_host",
    "server_city",
    "server_region",
    "server_country",
    "isp_name",
    "external_ip",
    "interface_name",
    "connection_type",
    "engine_name",
    "engine_version",
    "application_version",
    "raw_result_json",
    "created_at_utc",
    "concurrent_rx_bps",
    "concurrent_tx_bps",
)


@dataclasses.dataclass(slots=True)
class TestRun:
    """One attempted speed test, successful or not.

    ``id`` is ``None`` until the row has been written.
    """

    trigger_type: str
    started_at_utc: str
    started_at_local: str
    status: str
    engine_name: str
    application_version: str
    created_at_utc: str

    id: int | None = None
    scheduled_at_utc: str | None = None
    completed_at_utc: str | None = None
    completed_at_local: str | None = None
    duration_ms: int | None = None
    error_category: str | None = None
    error_message: str | None = None
    download_bps: int | None = None
    upload_bps: int | None = None
    idle_latency_ms: float | None = None
    download_latency_ms: float | None = None
    upload_latency_ms: float | None = None
    jitter_ms: float | None = None
    packet_loss_percent: float | None = None
    server_id: str | None = None
    server_name: str | None = None
    server_host: str | None = None
    server_city: str | None = None
    server_region: str | None = None
    server_country: str | None = None
    isp_name: str | None = None
    external_ip: str | None = None
    interface_name: str | None = None
    connection_type: str | None = None
    engine_version: str | None = None
    raw_result_json: str | None = None
    #: How much traffic the link was already carrying when the test began.
    #: A test run over a busy connection measures the capacity that was left,
    #: so these turn an otherwise inexplicable low reading into a explained
    #: one. NULL when the throughput monitor was not running.
    concurrent_rx_bps: float | None = None
    concurrent_tx_bps: float | None = None

    # -- derived views -----------------------------------------------------

    @property
    def download_mbps(self) -> float | None:
        return bps_to_mbps(self.download_bps)

    @property
    def upload_mbps(self) -> float | None:
        return bps_to_mbps(self.upload_bps)

    @property
    def succeeded(self) -> bool:
        return self.status == RunStatus.SUCCESS

    def status_label(self) -> str:
        """Word shown in the Status column -- never colour alone."""
        return {
            RunStatus.SUCCESS: "Success",
            RunStatus.FAILED: "Failed",
            RunStatus.TIMEOUT: "Timed out",
            RunStatus.CANCELLED: "Cancelled",
            RunStatus.SKIPPED: "Skipped",
        }.get(self.status, self.status.replace("_", " ").capitalize())

    def to_row(self) -> dict[str, Any]:
        """Column mapping for an INSERT, excluding the generated id."""
        return {field: getattr(self, field) for field in TEST_RUN_FIELDS}

    @classmethod
    def from_row(cls, row: Any) -> "TestRun":
        """Rebuild from a ``sqlite3.Row`` (or any mapping)."""
        data = {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclasses.dataclass(slots=True)
class EngineMeasurement:
    """Normalised successful measurement, independent of which engine ran.

    Every field is optional except the engine's own name: engines differ in
    what they report, and a field the engine did not supply must stay NULL
    rather than becoming a fabricated zero.
    """

    engine_name: str
    engine_version: str | None = None
    download_bps: int | None = None
    upload_bps: int | None = None
    idle_latency_ms: float | None = None
    download_latency_ms: float | None = None
    upload_latency_ms: float | None = None
    jitter_ms: float | None = None
    packet_loss_percent: float | None = None
    server_id: str | None = None
    server_name: str | None = None
    server_host: str | None = None
    server_city: str | None = None
    server_region: str | None = None
    server_country: str | None = None
    isp_name: str | None = None
    external_ip: str | None = None
    interface_name: str | None = None
    raw_result_json: str | None = None


@dataclasses.dataclass(slots=True)
class ThroughputSummary:
    """One minute of observed traffic on one interface.

    Distinct from a speed test in kind, not just degree: a test measures what
    the connection *could* carry by saturating it, this records what it
    actually carried without sending anything.
    """

    interface_name: str
    started_at_utc: str
    started_at_local: str
    ended_at_utc: str
    duration_ms: int
    sample_count: int
    rx_bytes: int
    tx_bytes: int
    rx_bps_mean: float
    tx_bps_mean: float
    rx_bps_peak: float
    tx_bps_peak: float
    application_version: str
    created_at_utc: str
    connection_type: str | None = None
    id: int | None = None

    def to_row(self) -> dict[str, Any]:
        return {field: getattr(self, field) for field in THROUGHPUT_FIELDS}

    @classmethod
    def from_row(cls, row: Any) -> "ThroughputSummary":
        data = {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


#: Column order of ``throughput_samples``, excluding the generated id.
THROUGHPUT_FIELDS: tuple[str, ...] = (
    "interface_name",
    "started_at_utc",
    "started_at_local",
    "ended_at_utc",
    "duration_ms",
    "sample_count",
    "rx_bytes",
    "tx_bytes",
    "rx_bps_mean",
    "tx_bps_mean",
    "rx_bps_peak",
    "tx_bps_peak",
    "connection_type",
    "application_version",
    "created_at_utc",
)
