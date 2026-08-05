"""Reading throughput from the kernel's interface counters.

The measurement itself is a subtraction, so the tests are almost entirely
about the cases where subtracting would produce a confident wrong answer:
counter resets, clock oddities, interfaces changing underfoot.
"""

from __future__ import annotations

import pytest

from bandwidth_logger.system.throughput import (
    IMPLAUSIBLE_BYTES_PER_SECOND,
    CounterReading,
    difference,
    format_bytes,
    format_rate,
    read_counters,
)


def reading(interface="eth0", rx=0, tx=0, monotonic=0.0):
    return CounterReading(interface=interface, rx_bytes=rx, tx_bytes=tx, monotonic=monotonic)


class TestReadingCounters:
    def test_counters_are_read_from_the_interface(self, tmp_path):
        statistics = tmp_path / "eth0" / "statistics"
        statistics.mkdir(parents=True)
        (statistics / "rx_bytes").write_text("1234\n")
        (statistics / "tx_bytes").write_text("5678\n")

        result = read_counters("eth0", root=tmp_path)

        assert result is not None
        assert result.rx_bytes == 1234
        assert result.tx_bytes == 5678
        assert result.interface == "eth0"

    def test_a_missing_interface_reads_as_nothing(self, tmp_path):
        """Normal, not an error: an interface can vanish between being
        chosen and being read.
        """
        assert read_counters("does-not-exist", root=tmp_path) is None

    def test_unparseable_counters_read_as_nothing(self, tmp_path):
        statistics = tmp_path / "eth0" / "statistics"
        statistics.mkdir(parents=True)
        (statistics / "rx_bytes").write_text("not a number")
        (statistics / "tx_bytes").write_text("5678")

        assert read_counters("eth0", root=tmp_path) is None


class TestDifference:
    def test_throughput_is_the_delta_over_the_interval(self):
        sample = difference(
            reading(rx=1_000, tx=500, monotonic=10.0),
            reading(rx=1_000_000, tx=125_500, monotonic=18.0),
        )

        assert sample is not None
        assert sample.rx_bytes == 999_000
        assert sample.tx_bytes == 125_000
        assert sample.seconds == 8.0
        # 999,000 bytes in 8s is 999 kB/s, which is 999 kbit * 8 = ~999 kbps.
        assert sample.rx_bits_per_second == pytest.approx(999_000 * 8 / 8)
        assert sample.tx_bits_per_second == pytest.approx(125_000 * 8 / 8)

    def test_no_traffic_is_zero_not_missing(self):
        """A quiet minute really did measure nothing. That is a measurement
        of zero, not an absence of one -- the same distinction the rest of
        the application keeps.
        """
        sample = difference(
            reading(rx=1_000, tx=500, monotonic=0.0),
            reading(rx=1_000, tx=500, monotonic=2.0),
        )

        assert sample is not None
        assert sample.rx_bytes == 0
        assert sample.rx_bits_per_second == 0.0

    def test_a_counter_reset_yields_nothing(self):
        """An interface that went down and up starts from zero again.

        Subtracting would give a large negative delta, and taking its
        magnitude would invent an enormous burst of traffic that never
        happened.
        """
        assert difference(
            reading(rx=5_000_000, monotonic=0.0),
            reading(rx=1_000, monotonic=2.0),
        ) is None

    def test_a_reset_on_only_one_direction_still_yields_nothing(self):
        assert difference(
            reading(rx=1_000, tx=5_000_000, monotonic=0.0),
            reading(rx=2_000, tx=1_000, monotonic=2.0),
        ) is None

    def test_readings_from_different_interfaces_are_not_compared(self):
        assert difference(
            reading(interface="eth0", rx=1_000, monotonic=0.0),
            reading(interface="wlan0", rx=2_000, monotonic=2.0),
        ) is None

    def test_no_elapsed_time_yields_nothing(self):
        """Dividing by zero would be a crash; dividing by a negative interval
        would be a lie.
        """
        assert difference(reading(monotonic=5.0), reading(monotonic=5.0)) is None
        assert difference(reading(monotonic=5.0), reading(monotonic=4.0)) is None

    def test_an_implausible_rate_is_discarded(self):
        """Beyond any real link, so the counters were reset or misread."""
        absurd = int(IMPLAUSIBLE_BYTES_PER_SECOND * 10)
        assert difference(
            reading(rx=0, monotonic=0.0),
            reading(rx=absurd, monotonic=1.0),
        ) is None

    def test_a_fast_but_believable_rate_is_kept(self):
        """A gigabit line moves ~125 MB/s. That must not be discarded."""
        sample = difference(
            reading(rx=0, monotonic=0.0),
            reading(rx=125_000_000, monotonic=1.0),
        )

        assert sample is not None
        assert sample.rx_bits_per_second == pytest.approx(1_000_000_000)


class TestFormatting:
    @pytest.mark.parametrize(
        ("bits", "expected"),
        [
            (0, "0 bit/s"),
            (512, "512 bit/s"),
            (1_500, "1.5 kbit/s"),
            (12_400_000, "12.40 Mbps"),
            (930_000_000, "930.00 Mbps"),
            (2_500_000_000, "2.50 Gbps"),
        ],
    )
    def test_rates_are_scaled_to_a_sensible_unit(self, bits, expected):
        assert format_rate(bits) == expected

    def test_a_missing_rate_is_blank_not_zero(self):
        """The same rule the whole application follows: not measured and
        measured-as-nothing are different facts.
        """
        assert format_rate(None) == ""
        assert format_rate(0) == "0 bit/s"

    @pytest.mark.parametrize(
        ("total", "expected"),
        [(0, "0 B"), (900, "900 B"), (2048, "2.0 KiB"), (5_242_880, "5.0 MiB")],
    )
    def test_byte_totals_are_scaled(self, total, expected):
        assert format_bytes(total) == expected

    def test_a_missing_total_is_blank(self):
        assert format_bytes(None) == ""
