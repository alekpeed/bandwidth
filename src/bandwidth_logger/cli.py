"""Headless entry point.

This is what the systemd timer runs, and it is also the developer command for
inspecting the application without opening a window. It does no scheduling of
its own: it performs one test through the same :class:`TestRunner` the window
uses, records it, and exits.

Exit codes matter here. A *failed test* is a normal, recorded outcome and
exits 0, so one bad test can never mark the unit failed and stop future runs.
Only a failure of the application itself -- above all a database that cannot
be written -- exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from . import APPLICATION_NAME, APPLICATION_VERSION
from .core.models import (
    RunStatus,
    TriggerType,
    format_duration,
    format_mbps,
    format_ms,
    summarise_error,
    to_utc_iso,
    utc_now,
)
from .core.scheduler import Scheduler, describe_interval
from .core.test_runner import TestRunner
from .storage.database import Database, DatabaseError
from .storage.export_csv import default_filename, export_database
from .system import paths
from .system.logging_setup import configure_logging, get_logger

EXIT_OK = 0
EXIT_APPLICATION_ERROR = 2

log = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bandwidth-logger-run",
        description=(
            f"{APPLICATION_NAME} command-line helper. Runs one speed test and records it. "
            "Normal use of the application does not require this command."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--scheduled",
        action="store_true",
        help="run one test as a scheduled test (used by the background timer)",
    )
    mode.add_argument(
        "--now",
        action="store_true",
        help="run one test as a manual test and print the stored record",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="print scheduler state, engine availability and the most recent record",
    )
    mode.add_argument(
        "--export",
        metavar="PATH",
        help="export the full history to a CSV file and print the row count",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable output instead of a summary",
    )
    parser.add_argument(
        "--include-external-ip",
        action="store_true",
        help="include the external IP address column when exporting",
    )
    parser.add_argument("--verbose", action="store_true", help="log at debug level")
    parser.add_argument("--version", action="version", version=f"{APPLICATION_NAME} {APPLICATION_VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    configure_logging(verbose=arguments.verbose, stream=True)
    paths.ensure_directories()

    try:
        database = Database().open()
    except DatabaseError as exc:
        log.error("Cannot open the database: %s", exc)
        print(f"Cannot open the database: {exc}", file=sys.stderr)
        return EXIT_APPLICATION_ERROR

    try:
        if arguments.status:
            return _print_status(database, as_json=arguments.json)
        if arguments.export:
            return _export(database, arguments)
        return _run_test(database, arguments)
    finally:
        database.close()


def _run_test(database: Database, arguments: argparse.Namespace) -> int:
    scheduled = arguments.scheduled
    scheduler = Scheduler(database)

    if scheduled:
        # A long gap since the last attempt means the machine was off, asleep
        # or logged out. Note it once, then carry on with this single test --
        # never a burst of catch-up runs.
        try:
            scheduler.record_missed_window()
        except DatabaseError:
            log.exception("Could not record the interrupted scheduling window")

    trigger = TriggerType.SCHEDULED if scheduled else TriggerType.MANUAL
    scheduled_at = to_utc_iso(utc_now()) if scheduled else None

    runner = TestRunner(database)
    outcome = runner.run_test(trigger, scheduled_at_utc=scheduled_at)

    if not outcome.stored:
        # The record could not be written. This is the one failure the timer
        # should surface, because the history is now incomplete.
        print(f"The test result could not be saved: {outcome.save_error}", file=sys.stderr)
        return EXIT_APPLICATION_ERROR

    if arguments.json:
        print(json.dumps(_record_payload(outcome.run), indent=2, sort_keys=True))
    else:
        print(_describe_run(outcome.run))
    return EXIT_OK


def _print_status(database: Database, *, as_json: bool) -> int:
    from .engines.registry import available_engines, known_engines, select_engine
    from .storage.database import SETTING_ENGINE_NAME

    scheduler = Scheduler(database)
    status = scheduler.status()
    preference = database.get_setting(SETTING_ENGINE_NAME) or "auto"
    chosen = select_engine(preference)
    latest = database.latest_run()

    if as_json:
        print(
            json.dumps(
                {
                    "application_version": APPLICATION_VERSION,
                    "database_path": str(database.path),
                    "schema_version": database.schema_version,
                    "record_count": database.count_runs(),
                    "scheduler": {
                        "supported": status.supported,
                        "enabled": status.enabled,
                        "timer_active": status.timer_active,
                        "starts_at_login": status.timer_enabled_at_login,
                        "interval_minutes": status.interval_minutes,
                        "next_run_utc": status.next_run_utc.isoformat() if status.next_run_utc else None,
                        "healthy": status.healthy,
                        "detail": status.detail,
                    },
                    "engines": {
                        "preference": preference,
                        "selected": chosen.name if chosen else None,
                        "installed": [engine.name for engine in available_engines()],
                        "known": [engine.name for engine in known_engines()],
                    },
                    "latest_run": _record_payload(latest) if latest else None,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_OK

    lines = [
        f"{APPLICATION_NAME} {APPLICATION_VERSION}",
        f"Database:            {database.path} (schema v{database.schema_version})",
        f"Records stored:      {database.count_runs()}",
        f"Automatic testing:   {'On' if status.enabled else 'Off'}",
        f"Interval:            {describe_interval(status.interval_minutes)}",
        f"Next test:           {status.next_run_display()}",
        f"Scheduler healthy:   {'yes' if status.healthy else 'no'}",
        f"Starts at login:     {'yes' if status.timer_enabled_at_login else 'no'}",
        f"Engine preference:   {preference}",
        f"Engine selected:     {chosen.display_name if chosen else 'none installed'}",
        f"Engines installed:   {', '.join(e.name for e in available_engines()) or 'none'}",
    ]
    if status.detail:
        lines.extend(["", "Scheduler detail:", status.detail])
    if latest is not None:
        lines.extend(["", "Most recent record:", _describe_run(latest)])
    print("\n".join(lines))
    return EXIT_OK


def _export(database: Database, arguments: argparse.Namespace) -> int:
    destination = arguments.export
    try:
        rows = export_database(
            database,
            destination,
            include_external_ip=arguments.include_external_ip,
        )
    except OSError as exc:
        print(f"Could not write {destination}: {exc}", file=sys.stderr)
        return EXIT_APPLICATION_ERROR
    print(f"Exported {rows} record(s) to {destination}")
    return EXIT_OK


def _describe_run(run) -> str:  # type: ignore[no-untyped-def]
    lines = [
        f"  id                {run.id}",
        f"  trigger           {run.trigger_type}",
        f"  status            {run.status_label()}",
        f"  started (local)   {run.started_at_local}",
        f"  completed (local) {run.completed_at_local or ''}",
        f"  duration          {format_duration(run.duration_ms)}",
        f"  engine            {run.engine_name} {run.engine_version or ''}".rstrip(),
    ]
    if run.status == RunStatus.SUCCESS:
        lines.extend(
            [
                f"  download          {format_mbps(run.download_bps)} Mbps",
                f"  upload            {format_mbps(run.upload_bps)} Mbps",
                f"  idle latency      {format_ms(run.idle_latency_ms)} ms",
                f"  server            {run.server_name or ''} ({run.server_city or 'unknown location'})",
                f"  ISP               {run.isp_name or ''}",
            ]
        )
    else:
        lines.extend(
            [
                f"  error category    {run.error_category or ''}",
                f"  error summary     {summarise_error(run.error_category)}",
                f"  error message     {(run.error_message or '').splitlines()[0] if run.error_message else ''}",
            ]
        )
    return "\n".join(lines)


def _record_payload(run) -> dict:  # type: ignore[no-untyped-def]
    payload = run.to_row()
    payload["id"] = run.id
    payload["download_mbps"] = run.download_mbps
    payload["upload_mbps"] = run.upload_mbps
    return payload


def suggested_export_name(moment: datetime | None = None) -> str:
    return default_filename(moment)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
