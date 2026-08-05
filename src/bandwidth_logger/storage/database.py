"""SQLite access layer.

Every result write happens inside a transaction. If the database itself is
unavailable the failure is raised as :class:`DatabaseError` and written to the
diagnostic log, because a save that quietly does nothing would break the one
guarantee this application makes: no attempted test disappears.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.models import (
    TEST_RUN_FIELDS,
    THROUGHPUT_FIELDS,
    TestRun,
    ThroughputSummary,
)
from ..system import paths
from ..system.logging_setup import get_logger
from . import migrations

log = get_logger("database")


class DatabaseError(RuntimeError):
    """The database could not be read or written."""


# Setting keys. Settings live in the database rather than a separate file so a
# single backup of the data directory carries both history and configuration.
SETTING_AUTO_ENABLED = "auto_testing_enabled"
SETTING_INTERVAL_MINUTES = "interval_minutes"
SETTING_RUN_AFTER_LOGIN = "run_scheduler_after_login"
SETTING_EXPORT_INCLUDE_IP = "export_include_external_ip"
SETTING_SORT_NEWEST_FIRST = "table_sort_newest_first"
SETTING_TIMEOUT_SECONDS = "engine_timeout_seconds"
SETTING_ENGINE_NAME = "preferred_engine"
SETTING_SERVER_ID = "preferred_server_id"
SETTING_CLOSE_NOTICE_SHOWN = "close_behaviour_notice_shown"
SETTING_MONITOR_ENABLED = "throughput_monitor_enabled"
SETTING_MONITOR_INTERVAL_SECONDS = "throughput_sample_seconds"
SETTING_MONITOR_RETENTION_DAYS = "throughput_retention_days"

DEFAULT_SETTINGS: dict[str, str] = {
    SETTING_AUTO_ENABLED: "false",
    SETTING_INTERVAL_MINUTES: "30",
    SETTING_RUN_AFTER_LOGIN: "true",
    SETTING_EXPORT_INCLUDE_IP: "false",
    SETTING_SORT_NEWEST_FIRST: "true",
    SETTING_TIMEOUT_SECONDS: "180",
    SETTING_ENGINE_NAME: "auto",
    # Empty means "let the engine choose". See docs/ARCHITECTURE.md.
    SETTING_SERVER_ID: "",
    SETTING_CLOSE_NOTICE_SHOWN: "false",
    SETTING_MONITOR_ENABLED: "false",
    SETTING_MONITOR_INTERVAL_SECONDS: "2",
    SETTING_MONITOR_RETENTION_DAYS: "30",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Connection holder for one process.

    The GUI and the scheduler run as separate processes and both write here.
    SQLite handles that with its own locking; ``busy_timeout`` makes a
    concurrent write wait rather than fail immediately, and WAL journalling
    lets the window keep reading while a scheduled test writes its result.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else paths.database_path()
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._connection is not None:
                return self._connection
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(
                    self.path,
                    timeout=30.0,
                    isolation_level="DEFERRED",
                    check_same_thread=False,
                )
            except (sqlite3.Error, OSError) as exc:
                log.exception("Cannot open database at %s", self.path)
                raise DatabaseError(f"Cannot open database at {self.path}: {exc}") from exc

            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA busy_timeout = 30000")
            self._connection = connection
            return connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> "Database":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def open(self) -> "Database":
        """Connect and migrate to the current schema version."""
        connection = self.connect()
        try:
            migrations.migrate(connection)
        except sqlite3.Error as exc:
            raise DatabaseError(f"Schema migration failed: {exc}") from exc
        self._seed_defaults()
        return self

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside one transaction, rolling back on any error."""
        connection = self.connect()
        with self._lock:
            try:
                with connection:
                    yield connection
            except sqlite3.Error as exc:
                log.exception("Transaction failed")
                raise DatabaseError(str(exc)) from exc

    @property
    def schema_version(self) -> int:
        return migrations.current_version(self.connect())

    # -- settings ----------------------------------------------------------

    def _seed_defaults(self) -> None:
        """Insert any setting the database does not have yet.

        Uses ``INSERT OR IGNORE`` so an upgrade gains new keys without
        overwriting choices the user already made.
        """
        now = _utc_now_iso()
        with self.transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO settings (key, value, updated_at_utc) VALUES (?, ?, ?)",
                [(key, value, now) for key, value in DEFAULT_SETTINGS.items()],
            )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.connect().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default if default is not None else DEFAULT_SETTINGS.get(key)
        return row["value"]

    def set_setting(self, key: str, value: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO settings (key, value, updated_at_utc) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                               updated_at_utc = excluded.updated_at_utc
                """,
                (key, value, _utc_now_iso()),
            )

    def get_bool(self, key: str) -> bool:
        return str(self.get_setting(key)).strip().lower() in {"true", "1", "yes", "on"}

    def set_bool(self, key: str, value: bool) -> None:
        self.set_setting(key, "true" if value else "false")

    def get_int(self, key: str, default: int = 0) -> int:
        raw = self.get_setting(key)
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            return default

    def set_int(self, key: str, value: int) -> None:
        self.set_setting(key, str(int(value)))

    def all_settings(self) -> dict[str, str]:
        rows = self.connect().execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    # -- test runs ---------------------------------------------------------

    def insert_run(self, run: TestRun) -> int:
        """Write one attempted test and return its assigned id."""
        columns = ", ".join(TEST_RUN_FIELDS)
        placeholders = ", ".join(f":{field}" for field in TEST_RUN_FIELDS)
        with self.transaction() as connection:
            cursor = connection.execute(
                f"INSERT INTO test_runs ({columns}) VALUES ({placeholders})",
                run.to_row(),
            )
            run.id = int(cursor.lastrowid)
        log.debug("Stored run %d (%s/%s)", run.id, run.trigger_type, run.status)
        return run.id

    def update_run(self, run: TestRun) -> None:
        """Persist changes to an already-stored run."""
        if run.id is None:
            raise DatabaseError("Cannot update a run that has never been stored")
        assignments = ", ".join(f"{field} = :{field}" for field in TEST_RUN_FIELDS)
        payload = run.to_row()
        payload["id"] = run.id
        with self.transaction() as connection:
            connection.execute(f"UPDATE test_runs SET {assignments} WHERE id = :id", payload)

    def get_run(self, run_id: int) -> TestRun | None:
        row = self.connect().execute("SELECT * FROM test_runs WHERE id = ?", (run_id,)).fetchone()
        return TestRun.from_row(row) if row is not None else None

    def list_runs(
        self,
        *,
        newest_first: bool = True,
        limit: int | None = None,
        offset: int = 0,
        start_utc: str | None = None,
        end_utc: str | None = None,
    ) -> list[TestRun]:
        """Every attempted test in the window, failures included.

        There is deliberately no status filter: the table must not omit
        failed tests.
        """
        query = "SELECT * FROM test_runs"
        clauses, parameters = self._range_clauses(start_utc, end_utc)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at_utc " + ("DESC" if newest_first else "ASC")
        query += ", id " + ("DESC" if newest_first else "ASC")
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            parameters.extend([limit, offset])
        rows = self.connect().execute(query, parameters).fetchall()
        return [TestRun.from_row(row) for row in rows]

    def iter_runs(
        self,
        *,
        newest_first: bool = False,
        start_utc: str | None = None,
        end_utc: str | None = None,
    ) -> Iterator[TestRun]:
        """Stream runs for export without holding them all in memory."""
        query = "SELECT * FROM test_runs"
        clauses, parameters = self._range_clauses(start_utc, end_utc)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at_utc " + ("DESC" if newest_first else "ASC")
        query += ", id " + ("DESC" if newest_first else "ASC")
        cursor = self.connect().execute(query, parameters)
        for row in cursor:
            yield TestRun.from_row(row)

    def count_runs(self, *, start_utc: str | None = None, end_utc: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM test_runs"
        clauses, parameters = self._range_clauses(start_utc, end_utc)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        return int(self.connect().execute(query, parameters).fetchone()[0])

    def run_date_range(
        self, *, start_utc: str | None = None, end_utc: str | None = None
    ) -> tuple[str | None, str | None]:
        """Earliest and latest ``started_at_utc`` in the window."""
        query = "SELECT MIN(started_at_utc), MAX(started_at_utc) FROM test_runs"
        clauses, parameters = self._range_clauses(start_utc, end_utc)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        row = self.connect().execute(query, parameters).fetchone()
        return row[0], row[1]

    def latest_run(self) -> TestRun | None:
        row = self.connect().execute(
            "SELECT * FROM test_runs ORDER BY started_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()
        return TestRun.from_row(row) if row is not None else None

    def delete_runs(self, *, start_utc: str | None = None, end_utc: str | None = None) -> int:
        """Delete runs in the window and return how many were removed.

        Only ever called from an explicit confirmation in the interface.
        There is no automatic deletion and no retention limit.
        """
        query = "DELETE FROM test_runs"
        clauses, parameters = self._range_clauses(start_utc, end_utc)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self.transaction() as connection:
            cursor = connection.execute(query, parameters)
            removed = cursor.rowcount
        log.info("Deleted %d run(s)", removed)
        return int(removed)

    def delete_runs_by_id(self, run_ids: "list[int]") -> int:
        """Delete exactly the given runs and return how many were removed.

        Only ever called from an explicit confirmation in the interface, the
        same as range deletion. Ids are bound as parameters, never
        interpolated, and are chunked so a very large selection cannot exceed
        SQLite's variable limit.
        """
        unique = sorted({int(run_id) for run_id in run_ids})
        if not unique:
            return 0

        removed = 0
        chunk_size = 500
        with self.transaction() as connection:
            for start in range(0, len(unique), chunk_size):
                chunk = unique[start : start + chunk_size]
                placeholders = ", ".join("?" for _ in chunk)
                cursor = connection.execute(
                    f"DELETE FROM test_runs WHERE id IN ({placeholders})", chunk
                )
                removed += cursor.rowcount
        log.info("Deleted %d selected run(s)", removed)
        return int(removed)

    def runs_by_id(self, run_ids: "list[int]") -> "list[TestRun]":
        """Fetch specific runs, for showing what a deletion will remove."""
        unique = sorted({int(run_id) for run_id in run_ids})
        if not unique:
            return []
        placeholders = ", ".join("?" for _ in unique)
        rows = self.connect().execute(
            f"SELECT * FROM test_runs WHERE id IN ({placeholders}) ORDER BY started_at_utc",
            unique,
        ).fetchall()
        return [TestRun.from_row(row) for row in rows]

    # -- throughput --------------------------------------------------------

    def insert_throughput_summary(self, summary: ThroughputSummary) -> int:
        columns = ", ".join(THROUGHPUT_FIELDS)
        placeholders = ", ".join(f":{field}" for field in THROUGHPUT_FIELDS)
        with self.transaction() as connection:
            cursor = connection.execute(
                f"INSERT INTO throughput_samples ({columns}) VALUES ({placeholders})",
                summary.to_row(),
            )
            summary.id = int(cursor.lastrowid)
        return summary.id

    def list_throughput(
        self,
        *,
        newest_first: bool = True,
        limit: int | None = None,
        start_utc: str | None = None,
        end_utc: str | None = None,
    ) -> "list[ThroughputSummary]":
        query = "SELECT * FROM throughput_samples"
        clauses, parameters = self._range_clauses(start_utc, end_utc)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at_utc " + ("DESC" if newest_first else "ASC")
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        rows = self.connect().execute(query, parameters).fetchall()
        return [ThroughputSummary.from_row(row) for row in rows]

    def iter_throughput(
        self, *, start_utc: str | None = None, end_utc: str | None = None
    ) -> Iterator[ThroughputSummary]:
        """Stream summaries for export without holding them all in memory."""
        query = "SELECT * FROM throughput_samples"
        clauses, parameters = self._range_clauses(start_utc, end_utc)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at_utc ASC"
        for row in self.connect().execute(query, parameters):
            yield ThroughputSummary.from_row(row)

    def count_throughput(self, *, start_utc: str | None = None, end_utc: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM throughput_samples"
        clauses, parameters = self._range_clauses(start_utc, end_utc)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        return int(self.connect().execute(query, parameters).fetchone()[0])

    def latest_throughput(self) -> "ThroughputSummary | None":
        row = self.connect().execute(
            "SELECT * FROM throughput_samples ORDER BY started_at_utc DESC, id DESC LIMIT 1"
        ).fetchone()
        return ThroughputSummary.from_row(row) if row is not None else None

    def throughput_around(self, moment_utc: str, *, window_minutes: int = 2) -> "ThroughputSummary | None":
        """The summary covering, or immediately preceding, a given instant.

        Used to record what else was in flight when a speed test started.
        """
        from datetime import timedelta

        from ..core.models import parse_iso, to_utc_iso

        moment = parse_iso(moment_utc)
        if moment is None:
            return None
        earliest = to_utc_iso(moment - timedelta(minutes=window_minutes))
        row = self.connect().execute(
            """
            SELECT * FROM throughput_samples
            WHERE started_at_utc >= ? AND started_at_utc <= ?
            ORDER BY started_at_utc DESC LIMIT 1
            """,
            (earliest, moment_utc),
        ).fetchone()
        return ThroughputSummary.from_row(row) if row is not None else None

    def delete_throughput_before(self, cutoff_utc: str) -> int:
        """Drop summaries older than *cutoff_utc*.

        Only throughput samples are ever pruned. Speed-test records have no
        retention limit and are never removed automatically -- that guarantee
        is what the application exists to provide.
        """
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM throughput_samples WHERE started_at_utc < ?", (cutoff_utc,)
            )
            removed = cursor.rowcount
        return int(removed)

    @staticmethod
    def _range_clauses(start_utc: str | None, end_utc: str | None) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if start_utc:
            clauses.append("started_at_utc >= ?")
            parameters.append(start_utc)
        if end_utc:
            clauses.append("started_at_utc <= ?")
            parameters.append(end_utc)
        return clauses, parameters
