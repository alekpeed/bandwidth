"""Entry point for the continuous throughput monitor.

This is what the ``bandwidth-logger-monitor`` user service runs. It samples
the kernel's byte counters and stores one summary a minute until told to
stop; it performs no speed tests and generates no traffic.

Kept apart from ``cli.py`` because the two have opposite shapes: that one
runs once and exits, this one runs until stopped.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import APPLICATION_NAME, APPLICATION_VERSION
from .core.monitor import DEFAULT_SAMPLE_SECONDS, ThroughputMonitor
from .storage.database import Database, DatabaseError
from .system import paths
from .system.logging_setup import configure_logging, get_logger
from .system.throughput import current_interface, format_bytes, format_rate

EXIT_OK = 0
EXIT_APPLICATION_ERROR = 2

log = get_logger("monitor_cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bandwidth-logger-monitor",
        description=(
            f"{APPLICATION_NAME} throughput monitor. Records how much traffic the "
            "connection is actually carrying, by reading the kernel's interface "
            "counters. It sends nothing and performs no speed tests."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="take a single reading, print it, and exit (for checking it works)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print recent recorded throughput and exit",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="delete samples past the retention window and exit",
    )
    parser.add_argument(
        "--sample-seconds",
        type=float,
        default=None,
        help=f"override the sampling interval (default {DEFAULT_SAMPLE_SECONDS}s)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--verbose", action="store_true", help="log at debug level")
    parser.add_argument(
        "--version", action="version", version=f"{APPLICATION_NAME} {APPLICATION_VERSION}"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    configure_logging(verbose=arguments.verbose, stream=True)
    paths.ensure_directories()

    try:
        database = Database().open()
    except DatabaseError as exc:
        print(f"Cannot open the database: {exc}", file=sys.stderr)
        return EXIT_APPLICATION_ERROR

    try:
        monitor = ThroughputMonitor(database, sample_seconds=arguments.sample_seconds)

        if arguments.once:
            return _print_one_reading(monitor, as_json=arguments.json)
        if arguments.summary:
            return _print_summary(database, as_json=arguments.json)
        if arguments.prune:
            print(f"Removed {monitor.prune()} sample(s) past the retention window.")
            return EXIT_OK

        monitor.install_signal_handlers()
        monitor.run()
        return EXIT_OK
    finally:
        database.close()


def _print_one_reading(monitor: ThroughputMonitor, *, as_json: bool) -> int:
    """Sample twice so there is an interval to measure across."""
    import time

    interface, link_type = current_interface()
    if interface is None:
        message = "No default route, so there is no interface to measure."
        print(json.dumps({"error": message}) if as_json else message, file=sys.stderr)
        return EXIT_APPLICATION_ERROR

    from .system.throughput import difference, read_counters

    first = read_counters(interface)
    time.sleep(max(1.0, monitor.sample_seconds()))
    second = read_counters(interface)

    sample = difference(first, second) if first and second else None
    if sample is None:
        message = f"Could not measure {interface}: the counters were unreadable or reset."
        print(json.dumps({"error": message}) if as_json else message, file=sys.stderr)
        return EXIT_APPLICATION_ERROR

    if as_json:
        print(json.dumps({
            "interface": interface,
            "connection_type": link_type,
            "seconds": round(sample.seconds, 3),
            "rx_bytes": sample.rx_bytes,
            "tx_bytes": sample.tx_bytes,
            "rx_bps": round(sample.rx_bits_per_second, 1),
            "tx_bps": round(sample.tx_bits_per_second, 1),
        }, indent=2, sort_keys=True))
    else:
        print(f"Interface:  {interface} ({link_type or 'unknown type'})")
        print(f"Over:       {sample.seconds:.1f} s")
        print(f"Download:   {format_rate(sample.rx_bits_per_second)}  ({format_bytes(sample.rx_bytes)})")
        print(f"Upload:     {format_rate(sample.tx_bits_per_second)}  ({format_bytes(sample.tx_bytes)})")
    return EXIT_OK


def _print_summary(database: Database, *, as_json: bool) -> int:
    recent = database.list_throughput(limit=10)
    total = database.count_throughput()

    if as_json:
        print(json.dumps({
            "sample_count": total,
            "recent": [
                {
                    "started_at_utc": s.started_at_utc,
                    "interface": s.interface_name,
                    "rx_bps_mean": s.rx_bps_mean,
                    "tx_bps_mean": s.tx_bps_mean,
                    "rx_bps_peak": s.rx_bps_peak,
                    "tx_bps_peak": s.tx_bps_peak,
                    "rx_bytes": s.rx_bytes,
                    "tx_bytes": s.tx_bytes,
                }
                for s in recent
            ],
        }, indent=2, sort_keys=True))
        return EXIT_OK

    print(f"Recorded minutes: {total:,}")
    if not recent:
        print("\nNothing recorded yet. The monitor stores one row a minute while it runs.")
        return EXIT_OK

    print(f"\n{'Local time':<21}{'Down (mean)':>14}{'Down (peak)':>14}{'Up (mean)':>13}{'Up (peak)':>13}")
    print("-" * 75)
    for sample in recent:
        from .core.models import format_for_display

        print(
            f"{format_for_display(sample.started_at_local):<21}"
            f"{format_rate(sample.rx_bps_mean):>14}"
            f"{format_rate(sample.rx_bps_peak):>14}"
            f"{format_rate(sample.tx_bps_mean):>13}"
            f"{format_rate(sample.tx_bps_peak):>13}"
        )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
