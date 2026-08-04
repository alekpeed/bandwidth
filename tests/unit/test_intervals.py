"""Interval validation.

The five-minute floor is a real requirement, not a nicety: it is what stops a
mistyped interval from testing continuously.
"""

from __future__ import annotations

import pytest

from bandwidth_logger.core.scheduler import (
    INTERVAL_PRESETS,
    MAXIMUM_INTERVAL_MINUTES,
    MINIMUM_INTERVAL_MINUTES,
    describe_interval,
    validate_interval,
)


class TestPresets:
    def test_the_specified_presets_are_offered(self):
        assert [minutes for minutes, _label in INTERVAL_PRESETS] == [
            5, 10, 15, 30, 60, 120, 240, 360, 720, 1440
        ]

    def test_every_preset_is_itself_valid(self):
        for minutes, _label in INTERVAL_PRESETS:
            assert validate_interval(minutes) == minutes


class TestValidation:
    @pytest.mark.parametrize("minutes", [5, 6, 30, 90, 1440, 10080])
    def test_accepts_permitted_intervals(self, minutes):
        assert validate_interval(minutes) == minutes

    @pytest.mark.parametrize("minutes", [4, 1, 0, -5, -1440])
    def test_rejects_anything_below_five_minutes(self, minutes):
        with pytest.raises(ValueError, match="shortest permitted interval is 5"):
            validate_interval(minutes)

    def test_rejects_intervals_beyond_a_week(self):
        with pytest.raises(ValueError, match="7 days"):
            validate_interval(MAXIMUM_INTERVAL_MINUTES + 1)

    @pytest.mark.parametrize("value", ["", "  ", "half an hour", "5.5", None, "1e3"])
    def test_rejects_values_that_are_not_whole_numbers(self, value):
        with pytest.raises(ValueError):
            validate_interval(value)

    def test_accepts_a_numeric_string_from_the_interface(self):
        assert validate_interval(" 45 ") == 45

    def test_the_boundary_itself_is_permitted(self):
        assert validate_interval(MINIMUM_INTERVAL_MINUTES) == MINIMUM_INTERVAL_MINUTES


class TestDescription:
    @pytest.mark.parametrize(
        ("minutes", "expected"),
        [
            (5, "5 minutes"),
            (30, "30 minutes"),
            (60, "1 hour"),
            (120, "2 hours"),
            (1440, "24 hours"),
            (45, "45 minutes"),
            (180, "3 hours"),
            (2880, "2 days"),
        ],
    )
    def test_intervals_are_described_in_plain_words(self, minutes, expected):
        assert describe_interval(minutes) == expected


class TestApplyWithoutEnabling:
    """Pressing Apply while automatic testing is off.

    Reported in use: the interval was set to 5 minutes and Apply pressed, and
    nothing ran. The setting had saved correctly -- the switch was simply off
    -- but the interface said nothing either way, so "saved" was
    indistinguishable from "scheduled".
    """

    def test_the_apply_handler_explains_when_nothing_was_scheduled(self):
        """Checked against the source so it runs without GTK installed."""
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2]
            / "src/bandwidth_logger/ui/main_window.py"
        ).read_text(encoding="utf-8")

        body = source.split("def _apply_schedule")[1].split("\n    def ")[0]

        assert "from_apply_button and not enabled" in body, (
            "applying an interval with the switch off must be acknowledged"
        )
        assert "Interval saved, but automatic testing is off" in body
        assert "Switch Automatic testing on" in body, (
            "the message must say what the user needs to do next"
        )

    def test_toggling_the_switch_off_does_not_show_the_message(self):
        """The message belongs to the Apply button only.

        Switching automatic testing off is unambiguous on its own; an
        explanation there would just be noise.
        """
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2]
            / "src/bandwidth_logger/ui/main_window.py"
        ).read_text(encoding="utf-8")

        handler = source.split("def _on_auto_switch")[1].split("\n    def ")[0]
        assert "from_apply_button" not in handler
