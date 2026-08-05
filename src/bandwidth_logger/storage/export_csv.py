"""CSV export.

Written for LibreOffice Calc and for ordinary data-analysis tools, which
means: UTF-8, one header row, one row per attempted test, ISO 8601
timestamps, and standard quoting handled by :mod:`csv` rather than by hand.

Raw and display measurements are separate columns. ``download_bps`` is what
the engine reported; ``download_mbps`` is what the window showed. Keeping
both means a spreadsheet can be checked against the interface, and neither
number has to be reverse-engineered from the other.

Export never writes to the database.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import date, datetime, time, timezone
from pathlib import Path

from ..core.models import TestRun, ThroughputSummary, bps_to_mbps
from ..system.logging_setup import get_logger
from .database import Database

log = get_logger("export_csv")

EXTERNAL_IP_COLUMN = "external_ip"

#: Column order of the exported file. Stable across releases so a saved
#: spreadsheet template keeps working.
CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "trigger_type",
    "status",
    "error_category",
    "error_message",
    "scheduled_at_utc",
    "started_at_utc",
    "started_at_local",
    "completed_at_utc",
    "completed_at_local",
    "duration_ms",
    "download_bps",
    "download_mbps",
    "upload_bps",
    "upload_mbps",
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
    EXTERNAL_IP_COLUMN,
    "interface_name",
    "connection_type",
    "engine_name",
    "engine_version",
    "application_version",
    "created_at_utc",
    "concurrent_rx_bps",
    "concurrent_tx_bps",
)

#: Column order for exported throughput summaries.
THROUGHPUT_CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "interface_name",
    "connection_type",
    "started_at_utc",
    "started_at_local",
    "ended_at_utc",
    "duration_ms",
    "sample_count",
    "rx_bytes",
    "tx_bytes",
    "rx_bps_mean",
    "rx_mbps_mean",
    "rx_bps_peak",
    "rx_mbps_peak",
    "tx_bps_mean",
    "tx_mbps_mean",
    "tx_bps_peak",
    "tx_mbps_peak",
    "application_version",
    "created_at_utc",
)


def default_filename(moment: datetime | None = None) -> str:
    """``bandwidth-log-YYYY-MM-DD_HH-MM.csv`` in local time."""
    stamp = (moment or datetime.now()).astimezone()
    return f"bandwidth-log-{stamp:%Y-%m-%d_%H-%M}.csv"


def columns(*, include_external_ip: bool) -> list[str]:
    """Header row, minus the external IP address when it is excluded."""
    if include_external_ip:
        return list(CSV_COLUMNS)
    return [column for column in CSV_COLUMNS if column != EXTERNAL_IP_COLUMN]


def _cell(value: object) -> str:
    """Render one value.

    Missing data becomes a blank cell -- never ``None``, never ``0``, so a
    spreadsheet does not average a gap as if it were a zero measurement.

    Floats are written in plain decimal, never scientific notation. ``%g``
    renders 12,400,000.0 as ``1.24e+07``, which is not what anyone opening a
    spreadsheet of bits per second wants to see and is a needless risk when
    another tool parses the column. Trailing zeros are trimmed so a whole
    number reads as one.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def row_for(run: TestRun, *, include_external_ip: bool) -> dict[str, str]:
    values: dict[str, object] = {
        "id": run.id,
        "trigger_type": run.trigger_type,
        "status": run.status,
        "error_category": run.error_category,
        "error_message": run.error_message,
        "scheduled_at_utc": run.scheduled_at_utc,
        "started_at_utc": run.started_at_utc,
        "started_at_local": run.started_at_local,
        "completed_at_utc": run.completed_at_utc,
        "completed_at_local": run.completed_at_local,
        "duration_ms": run.duration_ms,
        "download_bps": run.download_bps,
        "download_mbps": bps_to_mbps(run.download_bps),
        "upload_bps": run.upload_bps,
        "upload_mbps": bps_to_mbps(run.upload_bps),
        "idle_latency_ms": run.idle_latency_ms,
        "download_latency_ms": run.download_latency_ms,
        "upload_latency_ms": run.upload_latency_ms,
        "jitter_ms": run.jitter_ms,
        "packet_loss_percent": run.packet_loss_percent,
        "server_id": run.server_id,
        "server_name": run.server_name,
        "server_host": run.server_host,
        "server_city": run.server_city,
        "server_region": run.server_region,
        "server_country": run.server_country,
        "isp_name": run.isp_name,
        EXTERNAL_IP_COLUMN: run.external_ip,
        "interface_name": run.interface_name,
        "connection_type": run.connection_type,
        "engine_name": run.engine_name,
        "engine_version": run.engine_version,
        "application_version": run.application_version,
        "created_at_utc": run.created_at_utc,
        "concurrent_rx_bps": run.concurrent_rx_bps,
        "concurrent_tx_bps": run.concurrent_tx_bps,
    }
    return {
        column: _cell(values.get(column))
        for column in columns(include_external_ip=include_external_ip)
    }


def write_runs(
    destination: Path | str,
    runs: Iterable[TestRun],
    *,
    include_external_ip: bool = False,
) -> int:
    """Write *runs* to *destination* and return the number of data rows.

    ``newline=""`` lets :mod:`csv` control line endings, which is what keeps
    a message containing a line break inside one properly quoted field.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    header = columns(include_external_ip=include_external_ip)

    written = 0
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for run in runs:
            writer.writerow(row_for(run, include_external_ip=include_external_ip))
            written += 1

    log.info("Exported %d row(s) to %s", written, destination)
    return written


def export_database(
    database: Database,
    destination: Path | str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    include_external_ip: bool = False,
    newest_first: bool = False,
) -> int:
    """Export the whole history, or a date range, to a CSV file.

    The From and To dates are inclusive whole local days, which is what a
    person means by "the 3rd to the 5th"; they are converted to a UTC window
    before the query so a range near midnight does not lose rows.
    """
    start_utc = local_day_start_utc(start_date) if start_date else None
    end_utc = local_day_end_utc(end_date) if end_date else None
    runs = database.iter_runs(newest_first=newest_first, start_utc=start_utc, end_utc=end_utc)
    return write_runs(destination, runs, include_external_ip=include_external_ip)


def local_day_start_utc(day: date) -> str:
    return datetime.combine(day, time.min).astimezone().astimezone(timezone.utc).isoformat()


def local_day_end_utc(day: date) -> str:
    return datetime.combine(day, time.max).astimezone().astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Throughput
# --------------------------------------------------------------------------


def throughput_row(summary: ThroughputSummary) -> dict[str, str]:
    """One exported throughput row.

    Raw bits per second and display megabits sit side by side, the same way
    speed-test rows carry both, so a spreadsheet never has to convert.
    """
    values: dict[str, object] = {
        "id": summary.id,
        "interface_name": summary.interface_name,
        "connection_type": summary.connection_type,
        "started_at_utc": summary.started_at_utc,
        "started_at_local": summary.started_at_local,
        "ended_at_utc": summary.ended_at_utc,
        "duration_ms": summary.duration_ms,
        "sample_count": summary.sample_count,
        "rx_bytes": summary.rx_bytes,
        "tx_bytes": summary.tx_bytes,
        "rx_bps_mean": summary.rx_bps_mean,
        "rx_mbps_mean": bps_to_mbps(summary.rx_bps_mean),
        "rx_bps_peak": summary.rx_bps_peak,
        "rx_mbps_peak": bps_to_mbps(summary.rx_bps_peak),
        "tx_bps_mean": summary.tx_bps_mean,
        "tx_mbps_mean": bps_to_mbps(summary.tx_bps_mean),
        "tx_bps_peak": summary.tx_bps_peak,
        "tx_mbps_peak": bps_to_mbps(summary.tx_bps_peak),
        "application_version": summary.application_version,
        "created_at_utc": summary.created_at_utc,
    }
    return {column: _cell(values.get(column)) for column in THROUGHPUT_CSV_COLUMNS}


def default_throughput_filename(moment: datetime | None = None) -> str:
    """``bandwidth-throughput-YYYY-MM-DD_HH-MM.csv`` in local time."""
    stamp = (moment or datetime.now()).astimezone()
    return f"bandwidth-throughput-{stamp:%Y-%m-%d_%H-%M}.csv"


def export_throughput(
    database: Database,
    destination: Path | str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> int:
    """Export throughput summaries to CSV. Never alters the database."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    start_utc = local_day_start_utc(start_date) if start_date else None
    end_utc = local_day_end_utc(end_date) if end_date else None

    written = 0
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(THROUGHPUT_CSV_COLUMNS), quoting=csv.QUOTE_MINIMAL
        )
        writer.writeheader()
        for summary in database.iter_throughput(start_utc=start_utc, end_utc=end_utc):
            writer.writerow(throughput_row(summary))
            written += 1

    log.info("Exported %d throughput row(s) to %s", written, destination)
    return written
