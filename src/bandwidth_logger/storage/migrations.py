"""Versioned, transactional, non-destructive schema migrations.

Rules that hold for every migration added here:

* Each one runs inside a single transaction, so a failure leaves the database
  exactly as it was rather than half-upgraded.
* No migration drops a table or a column, and none deletes rows. An upgrade
  must never cost the user history -- that history is the whole point of the
  application.
* Migrations are append-only. Once a version has shipped its SQL is frozen;
  corrections arrive as a new version.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from ..system.logging_setup import get_logger

log = get_logger("migrations")

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _run_statements(connection: sqlite3.Connection, script: str) -> None:
    """Execute a multi-statement script one statement at a time.

    Deliberately not ``executescript``: that commits any open transaction
    before it runs, which would silently break the atomicity the migration
    runner sets up.
    """
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)


def _migration_001(connection: sqlite3.Connection) -> None:
    """Initial schema: settings, test_runs and their indexes."""
    _run_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS settings (
            key             TEXT PRIMARY KEY,
            value           TEXT NOT NULL,
            updated_at_utc  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS test_runs (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            trigger_type          TEXT NOT NULL,
            scheduled_at_utc      TEXT NULL,
            started_at_utc        TEXT NOT NULL,
            started_at_local      TEXT NOT NULL,
            completed_at_utc      TEXT NULL,
            completed_at_local    TEXT NULL,
            duration_ms           INTEGER NULL,
            status                TEXT NOT NULL,
            error_category        TEXT NULL,
            error_message         TEXT NULL,
            download_bps          INTEGER NULL,
            upload_bps            INTEGER NULL,
            idle_latency_ms       REAL NULL,
            download_latency_ms   REAL NULL,
            upload_latency_ms     REAL NULL,
            jitter_ms             REAL NULL,
            packet_loss_percent   REAL NULL,
            server_id             TEXT NULL,
            server_name           TEXT NULL,
            server_host           TEXT NULL,
            server_city           TEXT NULL,
            server_region         TEXT NULL,
            server_country        TEXT NULL,
            isp_name              TEXT NULL,
            external_ip           TEXT NULL,
            interface_name        TEXT NULL,
            connection_type       TEXT NULL,
            engine_name           TEXT NOT NULL,
            engine_version        TEXT NULL,
            application_version   TEXT NOT NULL,
            raw_result_json       TEXT NULL,
            created_at_utc        TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_test_runs_started_at_utc
            ON test_runs (started_at_utc);
        CREATE INDEX IF NOT EXISTS idx_test_runs_status
            ON test_runs (status);
        CREATE INDEX IF NOT EXISTS idx_test_runs_trigger_type
            ON test_runs (trigger_type);
        """
    )


#: Ordered list of every migration. Append only.
MIGRATIONS: list[Migration] = [
    (1, "initial schema", _migration_001),
]

LATEST_VERSION = max(version for version, _, _ in MIGRATIONS)


def _ensure_migrations_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version          INTEGER PRIMARY KEY,
            applied_at_utc   TEXT NOT NULL
        )
        """
    )


def applied_versions(connection: sqlite3.Connection) -> set[int]:
    _ensure_migrations_table(connection)
    rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(row[0]) for row in rows}


def current_version(connection: sqlite3.Connection) -> int:
    """Highest applied migration, or 0 for a database that has none."""
    applied = applied_versions(connection)
    return max(applied) if applied else 0


def migrate(connection: sqlite3.Connection) -> list[int]:
    """Bring *connection* up to :data:`LATEST_VERSION`.

    Returns the versions applied by this call -- empty when already current.
    Each migration commits on its own, so a later failure cannot undo an
    earlier success.

    The transaction is begun explicitly rather than through ``with
    connection``. Python's sqlite3 module does not open an implicit
    transaction for DDL, so a migration that creates a table and then fails
    would otherwise leave that table behind -- a half-applied schema
    recorded as not applied at all. SQLite's own DDL *is* transactional, so
    an explicit BEGIN makes the whole migration atomic.
    """
    _ensure_migrations_table(connection)
    connection.commit()

    already = applied_versions(connection)
    performed: list[int] = []

    previous_isolation = connection.isolation_level
    connection.isolation_level = None  # take manual control of transactions
    try:
        for version, description, apply in MIGRATIONS:
            if version in already:
                continue
            log.info("Applying migration %d (%s)", version, description)
            connection.execute("BEGIN IMMEDIATE")
            try:
                apply(connection)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at_utc) VALUES (?, ?)",
                    (version, datetime.now(timezone.utc).isoformat()),
                )
            except sqlite3.Error:
                connection.execute("ROLLBACK")
                log.exception(
                    "Migration %d failed; database left at version %d",
                    version,
                    current_version(connection),
                )
                raise
            connection.execute("COMMIT")
            performed.append(version)
    finally:
        connection.isolation_level = previous_isolation

    return performed
