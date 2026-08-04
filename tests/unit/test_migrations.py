"""Schema migrations.

The requirement these tests defend is that an upgrade never costs the user
history. A migration is allowed to add; it is not allowed to drop a table,
drop a column, or delete a row.
"""

from __future__ import annotations

import sqlite3

import pytest
from conftest import make_run

from bandwidth_logger.core.models import TEST_RUN_FIELDS
from bandwidth_logger.storage import migrations
from bandwidth_logger.storage.database import Database, DatabaseError


def table_names(connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def column_names(connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


class TestFromEmpty:
    def test_every_migration_runs_on_an_empty_database(self, tmp_path):
        connection = sqlite3.connect(tmp_path / "fresh.sqlite3")
        applied = migrations.migrate(connection)

        assert applied == [version for version, _, _ in migrations.MIGRATIONS]
        assert migrations.current_version(connection) == migrations.LATEST_VERSION

    def test_the_specified_tables_exist(self, tmp_path):
        connection = sqlite3.connect(tmp_path / "fresh.sqlite3")
        migrations.migrate(connection)

        assert {"settings", "test_runs", "schema_migrations"} <= table_names(connection)

    def test_test_runs_has_every_specified_column(self, tmp_path):
        connection = sqlite3.connect(tmp_path / "fresh.sqlite3")
        migrations.migrate(connection)

        columns = column_names(connection, "test_runs")
        assert "id" in columns
        assert set(TEST_RUN_FIELDS) <= columns

    def test_the_specified_indexes_exist(self, tmp_path):
        connection = sqlite3.connect(tmp_path / "fresh.sqlite3")
        migrations.migrate(connection)

        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'test_runs'"
        ).fetchall()
        names = {row[0] for row in rows}
        assert "idx_test_runs_started_at_utc" in names
        assert "idx_test_runs_status" in names
        assert "idx_test_runs_trigger_type" in names

    def test_migrating_twice_changes_nothing(self, tmp_path):
        connection = sqlite3.connect(tmp_path / "fresh.sqlite3")
        migrations.migrate(connection)

        assert migrations.migrate(connection) == []
        assert migrations.current_version(connection) == migrations.LATEST_VERSION


class TestUpgradePreservesData:
    def test_an_older_fixture_database_keeps_every_row(self, tmp_path, monkeypatch):
        """Simulate an upgrade: build a v1 database with real rows, add a
        migration, then migrate and check nothing was lost.
        """
        path = tmp_path / "existing.sqlite3"
        database = Database(path).open()
        for index in range(7):
            database.insert_run(make_run(isp_name=f"Provider {index}"))
        database.set_setting("interval_minutes", "45")
        original_ids = [run.id for run in database.list_runs()]
        database.close()

        # A later release adds a column. Appending, never rewriting.
        def migration_002(connection):
            connection.execute("ALTER TABLE test_runs ADD COLUMN notes TEXT NULL")

        monkeypatch.setattr(
            migrations,
            "MIGRATIONS",
            [*migrations.MIGRATIONS, (2, "add notes column", migration_002)],
        )

        upgraded = Database(path).open()
        try:
            assert upgraded.schema_version == 2
            assert [run.id for run in upgraded.list_runs()] == original_ids
            assert upgraded.count_runs() == 7
            # The user's setting survived, rather than being reset to default.
            assert upgraded.get_setting("interval_minutes") == "45"
            assert "notes" in column_names(upgraded.connect(), "test_runs")
        finally:
            upgraded.close()

    def test_a_new_setting_is_added_without_overwriting_existing_choices(self, tmp_path):
        path = tmp_path / "existing.sqlite3"
        database = Database(path).open()
        database.set_setting("interval_minutes", "120")
        database.close()

        reopened = Database(path).open()
        try:
            assert reopened.get_setting("interval_minutes") == "120"
        finally:
            reopened.close()


class TestFailureHandling:
    def test_a_failing_migration_leaves_the_previous_version_intact(self, tmp_path, monkeypatch):
        path = tmp_path / "db.sqlite3"
        connection = sqlite3.connect(path)
        migrations.migrate(connection)

        def broken(conn):
            conn.execute("CREATE TABLE half_done (id INTEGER)")
            conn.execute("THIS IS NOT SQL")

        monkeypatch.setattr(
            migrations,
            "MIGRATIONS",
            [*migrations.MIGRATIONS, (2, "broken", broken)],
        )

        with pytest.raises(sqlite3.Error):
            migrations.migrate(connection)

        # Rolled back whole: neither the version nor the partial table stuck.
        assert migrations.current_version(connection) == 1
        assert "half_done" not in table_names(connection)

    def test_an_unopenable_database_raises_rather_than_failing_silently(self, tmp_path):
        directory = tmp_path / "not-a-file"
        directory.mkdir()

        with pytest.raises(DatabaseError):
            Database(directory).open()


class TestMigrationDiscipline:
    def test_versions_are_unique_and_ascending(self):
        versions = [version for version, _, _ in migrations.MIGRATIONS]
        assert versions == sorted(versions)
        assert len(versions) == len(set(versions))

    def test_no_shipped_migration_drops_or_deletes(self):
        """Guards the append-only rule against a future edit.

        Reads the module source rather than running the migrations, because
        the point is to catch destructive SQL before anyone executes it.
        """
        import inspect

        source = inspect.getsource(migrations).lower()
        # Ignore this docstring's own mention of the words.
        body = source.split("migrations: list[migration]")[0]
        for forbidden in ("drop table", "drop column", "delete from test_runs", "truncate"):
            assert forbidden not in body, f"a migration must never {forbidden}"
