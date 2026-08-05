"""CSV export.

Covers the specification's export requirements: quoting, blank cells for
missing values, separate raw and display columns, and the external IP
exclusion.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta

from conftest import make_run

from bandwidth_logger.core.models import ErrorCategory, RunStatus, utc_now
from bandwidth_logger.storage.export_csv import (
    CSV_COLUMNS,
    EXTERNAL_IP_COLUMN,
    columns,
    default_filename,
    export_database,
    write_runs,
)


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


class TestStructure:
    def test_header_and_one_row_per_attempt(self, tmp_path):
        destination = tmp_path / "out.csv"
        runs = [make_run(), make_run(status=RunStatus.FAILED), make_run(status=RunStatus.SKIPPED)]

        written = write_runs(destination, runs, include_external_ip=True)

        assert written == 3
        rows = read_csv(destination)
        assert len(rows) == 3
        assert list(rows[0].keys()) == list(CSV_COLUMNS)

    def test_file_is_utf8(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(destination, [make_run(isp_name="Fibre Ötökkä ISP")])
        assert "Ötökkä" in destination.read_text(encoding="utf-8")

    def test_failed_attempts_are_never_omitted(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(
            destination,
            [
                make_run(status=RunStatus.SUCCESS),
                make_run(status=RunStatus.FAILED, error_category=ErrorCategory.NO_NETWORK),
                make_run(status=RunStatus.TIMEOUT, error_category=ErrorCategory.TIMEOUT),
                make_run(status=RunStatus.SKIPPED, error_category=ErrorCategory.SKIPPED_OVERLAP),
                make_run(status=RunStatus.CANCELLED, error_category=ErrorCategory.CANCELLED),
            ],
        )
        statuses = [row["status"] for row in read_csv(destination)]
        assert statuses == ["success", "failed", "timeout", "skipped", "cancelled"]


class TestQuoting:
    def test_commas_quotes_and_line_breaks_survive(self, tmp_path):
        destination = tmp_path / "out.csv"
        awkward = 'Engine failed: "no route", then\ngave up on line two'
        write_runs(destination, [make_run(status=RunStatus.FAILED, error_message=awkward)])

        rows = read_csv(destination)
        assert len(rows) == 1
        assert rows[0]["error_message"] == awkward

    def test_a_field_containing_a_comma_does_not_shift_later_columns(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(destination, [make_run(server_name="Telecom, Manchester Ltd")])

        rows = read_csv(destination)
        assert rows[0]["server_name"] == "Telecom, Manchester Ltd"
        assert rows[0]["isp_name"] == "Example Internet"


class TestMissingValues:
    def test_missing_values_are_blank_cells_not_zeros(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(
            destination,
            [
                make_run(
                    status=RunStatus.FAILED,
                    download_bps=None,
                    upload_bps=None,
                    idle_latency_ms=None,
                    jitter_ms=None,
                    packet_loss_percent=None,
                    server_name=None,
                )
            ],
        )

        row = read_csv(destination)[0]
        for column in (
            "download_bps",
            "download_mbps",
            "upload_bps",
            "idle_latency_ms",
            "jitter_ms",
            "packet_loss_percent",
            "server_name",
        ):
            assert row[column] == "", f"{column} should be blank, got {row[column]!r}"

    def test_measured_zeros_are_written_as_zero(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(
            destination,
            [make_run(download_bps=0, upload_bps=0, idle_latency_ms=0.0, packet_loss_percent=0.0)],
        )

        row = read_csv(destination)[0]
        assert row["download_bps"] == "0"
        assert row["download_mbps"] == "0"
        assert row["idle_latency_ms"] == "0"
        assert row["packet_loss_percent"] == "0"


class TestRawAndDisplayColumns:
    def test_both_bits_and_megabits_are_present(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(destination, [make_run(download_bps=95_000_000, upload_bps=19_000_000)])

        row = read_csv(destination)[0]
        assert row["download_bps"] == "95000000"
        assert row["download_mbps"] == "95"
        assert row["upload_bps"] == "19000000"
        assert row["upload_mbps"] == "19"

    def test_both_utc_and_local_timestamps_are_present(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(destination, [make_run()])

        row = read_csv(destination)[0]
        assert row["started_at_utc"].endswith("+00:00")
        assert row["started_at_local"]
        assert row["completed_at_utc"]


class TestExternalIpExclusion:
    def test_column_is_absent_by_default(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(destination, [make_run()])

        rows = read_csv(destination)
        assert EXTERNAL_IP_COLUMN not in rows[0]
        assert "203.0.113.42" not in destination.read_text(encoding="utf-8")

    def test_column_is_present_when_requested(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(destination, [make_run()], include_external_ip=True)

        rows = read_csv(destination)
        assert rows[0][EXTERNAL_IP_COLUMN] == "203.0.113.42"

    def test_only_that_one_column_is_removed(self):
        with_ip = columns(include_external_ip=True)
        without_ip = columns(include_external_ip=False)
        assert set(with_ip) - set(without_ip) == {EXTERNAL_IP_COLUMN}


class TestDatabaseExport:
    def test_exports_every_stored_record(self, database, tmp_path):
        for index in range(5):
            database.insert_run(make_run(moment=utc_now() - timedelta(hours=index)))

        destination = tmp_path / "all.csv"
        assert export_database(database, destination) == 5
        assert len(read_csv(destination)) == 5

    def test_date_range_includes_only_matching_records(self, database, tmp_path):
        today = date.today()
        for offset in (0, 1, 2, 5, 10):
            database.insert_run(make_run(moment=utc_now() - timedelta(days=offset)))

        destination = tmp_path / "range.csv"
        rows = export_database(
            database,
            destination,
            start_date=today - timedelta(days=2),
            end_date=today,
        )

        assert rows == 3
        assert len(read_csv(destination)) == 3

    def test_export_does_not_alter_the_database(self, database, tmp_path):
        for _ in range(3):
            database.insert_run(make_run())
        before = [run.id for run in database.list_runs()]

        export_database(database, tmp_path / "out.csv")

        assert [run.id for run in database.list_runs()] == before
        assert database.count_runs() == 3

    def test_empty_database_still_produces_a_header(self, database, tmp_path):
        destination = tmp_path / "empty.csv"
        assert export_database(database, destination) == 0

        text = destination.read_text(encoding="utf-8")
        assert text.strip().splitlines()[0].startswith("id,trigger_type,status")


class TestFilename:
    def test_default_name_matches_the_specified_pattern(self):
        name = default_filename()
        assert name.startswith("bandwidth-log-")
        assert name.endswith(".csv")
        # bandwidth-log-YYYY-MM-DD_HH-MM.csv
        stamp = name[len("bandwidth-log-") : -len(".csv")]
        assert len(stamp) == len("2026-08-04_09-15")
        assert stamp[4] == stamp[7] == "-"
        assert stamp[10] == "_"


class TestNumberFormatting:
    """Plain decimal, never scientific notation.

    ``%g`` renders 12,400,000.0 as ``1.24e+07``. Nobody opening a spreadsheet
    of bits per second wants that, and it is a needless parsing risk for any
    other tool reading the column.
    """

    def test_large_values_are_written_in_full(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(destination, [make_run(download_bps=930_000_000, upload_bps=56_000_000)])

        row = read_csv(destination)[0]
        assert row["download_bps"] == "930000000"
        assert "e+" not in destination.read_text(encoding="utf-8")

    def test_fractional_values_keep_their_precision(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(destination, [make_run(idle_latency_ms=10.56, jitter_ms=1.234)])

        row = read_csv(destination)[0]
        assert row["idle_latency_ms"] == "10.56"
        assert row["jitter_ms"] == "1.234"

    def test_a_whole_number_float_has_no_trailing_zeros(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(destination, [make_run(idle_latency_ms=11.0)])

        assert read_csv(destination)[0]["idle_latency_ms"] == "11"

    def test_zero_is_still_zero(self, tmp_path):
        destination = tmp_path / "out.csv"
        write_runs(destination, [make_run(packet_loss_percent=0.0)])

        assert read_csv(destination)[0]["packet_loss_percent"] == "0"
