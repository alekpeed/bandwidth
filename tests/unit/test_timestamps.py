"""Timestamps.

The specification requires ISO 8601 timestamps that stay correct across local
offset changes, which is the case that actually bites: a record written the
hour before a daylight-saving transition and one written the hour after must
each carry the offset that was really in force when the test ran, and both
must still sort correctly in UTC.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from bandwidth_logger.core.models import (
    format_for_display,
    parse_iso,
    to_local_iso,
    to_utc_iso,
    utc_now,
)

LONDON = ZoneInfo("Europe/London")
KATHMANDU = ZoneInfo("Asia/Kathmandu")  # UTC+05:45 -- a non-hour offset


@pytest.fixture()
def london_time(monkeypatch):
    """Run the body with the process's local zone set to Europe/London."""
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


class TestUtcConversion:
    def test_utc_timestamp_round_trips(self):
        moment = datetime(2026, 8, 4, 9, 15, 0, tzinfo=timezone.utc)
        assert parse_iso(to_utc_iso(moment)) == moment

    def test_an_aware_non_utc_time_is_converted_not_relabelled(self):
        moment = datetime(2026, 8, 4, 10, 15, 0, tzinfo=timezone(timedelta(hours=1)))
        assert to_utc_iso(moment) == "2026-08-04T09:15:00+00:00"

    def test_utc_now_is_aware(self):
        assert utc_now().tzinfo is not None


class TestLocalOffsets:
    def test_local_timestamp_carries_the_offset_in_force_at_that_instant(self, london_time):
        winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)

        # London is UTC+00:00 in January and UTC+01:00 in July.
        assert to_local_iso(winter).endswith("+00:00")
        assert to_local_iso(summer).endswith("+01:00")

    def test_records_either_side_of_a_transition_keep_their_own_offsets(self, london_time):
        # British Summer Time ended at 02:00 local on 2026-10-25.
        before = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
        after = datetime(2026, 10, 25, 2, 30, tzinfo=timezone.utc)

        before_local = to_local_iso(before)
        after_local = to_local_iso(after)

        assert before_local.endswith("+01:00")
        assert after_local.endswith("+00:00")

        # Both still describe the same instants, and still sort correctly.
        assert parse_iso(before_local) == before
        assert parse_iso(after_local) == after
        assert to_utc_iso(before) < to_utc_iso(after)

    def test_the_repeated_local_hour_is_unambiguous_in_utc(self, london_time):
        """01:30 local happens twice on the night the clocks go back.

        The two records show the same local wall-clock time; only the offset
        and the UTC timestamp tell them apart. Both must be preserved.
        """
        first = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)  # 01:30 BST
        second = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)  # 01:30 GMT

        first_local = to_local_iso(first)
        second_local = to_local_iso(second)

        assert first_local.startswith("2026-10-25T01:30")
        assert second_local.startswith("2026-10-25T01:30")
        assert first_local != second_local
        assert parse_iso(first_local) != parse_iso(second_local)

    def test_a_non_hour_offset_survives(self):
        moment = datetime(2026, 8, 4, 9, 15, tzinfo=timezone.utc)
        local = moment.astimezone(KATHMANDU)
        assert local.isoformat().endswith("+05:45")
        assert parse_iso(local.isoformat()) == moment


class TestParsing:
    def test_missing_values_parse_to_none(self):
        assert parse_iso(None) is None
        assert parse_iso("") is None

    def test_unparseable_values_return_none_rather_than_raising(self):
        assert parse_iso("not a timestamp") is None

    def test_a_naive_stored_value_is_read_as_utc(self):
        assert parse_iso("2026-08-04T09:15:00") == datetime(
            2026, 8, 4, 9, 15, tzinfo=timezone.utc
        )

    def test_display_format_is_local_and_unambiguous(self, london_time):
        assert format_for_display("2026-07-15T12:00:00+00:00") == "2026-07-15 13:00:00"

    def test_display_of_a_missing_timestamp_is_blank(self):
        assert format_for_display(None) == ""
