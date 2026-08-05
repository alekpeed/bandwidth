"""The continuous throughput monitor, end to end.

Counters are supplied from a temporary directory shaped like
``/sys/class/net``, so the real sampling loop runs against readings the test
controls -- no network, no waiting, and the reset and interface-change cases
can actually be provoked.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import make_run

from bandwidth_logger.core.models import to_utc_iso, utc_now
from bandwidth_logger.core.monitor import ThroughputMonitor
from bandwidth_logger.storage.database import (
    SETTING_MONITOR_RETENTION_DAYS,
    ThroughputSummary,
)

pytestmark = pytest.mark.integration


class FakeCounters:
    """A controllable pair of interface counters."""

    def __init__(self, interface="eth0"):
        self.interface = interface
        self.rx = 0
        self.tx = 0
        self.monotonic = 0.0

    def advance(self, *, rx_bytes=0, tx_bytes=0, seconds=2.0):
        self.rx += rx_bytes
        self.tx += tx_bytes
        self.monotonic += seconds

    def install(self, monkeypatch):
        from bandwidth_logger.core import monitor as monitor_module
        from bandwidth_logger.system.throughput import CounterReading

        monkeypatch.setattr(
            monitor_module, "current_interface", lambda: (self.interface, "Ethernet")
        )
        monkeypatch.setattr(
            monitor_module,
            "read_counters",
            lambda _interface: CounterReading(
                interface=self.interface, rx_bytes=self.rx, tx_bytes=self.tx,
                monotonic=self.monotonic,
            ),
        )


@pytest.fixture()
def counters(monkeypatch):
    fake = FakeCounters()
    fake.install(monkeypatch)
    return fake


def make_summary(database, moment, rx_mean=1_000_000.0, tx_mean=100_000.0):
    return database.insert_throughput_summary(
        ThroughputSummary(
            interface_name="eth0",
            started_at_utc=to_utc_iso(moment),
            started_at_local=to_utc_iso(moment),
            ended_at_utc=to_utc_iso(moment + timedelta(minutes=1)),
            duration_ms=60_000,
            sample_count=30,
            rx_bytes=int(rx_mean * 60 / 8),
            tx_bytes=int(tx_mean * 60 / 8),
            rx_bps_mean=rx_mean,
            tx_bps_mean=tx_mean,
            rx_bps_peak=rx_mean * 4,
            tx_bps_peak=tx_mean * 4,
            connection_type="Ethernet",
            application_version="test",
            created_at_utc=to_utc_iso(moment),
        )
    )


class TestSampling:
    def test_a_summary_is_written_once_the_window_elapses(self, database, counters):
        monitor = ThroughputMonitor(database, sample_seconds=2, summary_seconds=10)

        # Six passes at two seconds each: the first establishes a baseline,
        # the rest accumulate, and the window closes at ten seconds.
        for _ in range(7):
            counters.advance(rx_bytes=250_000, tx_bytes=25_000, seconds=2.0)
            monitor.tick()

        assert database.count_throughput() >= 1
        summary = database.latest_throughput()
        assert summary.interface_name == "eth0"
        assert summary.rx_bytes > 0
        # 250,000 bytes every 2s is 1 Mbit/s.
        assert summary.rx_bps_mean == pytest.approx(1_000_000, rel=0.01)

    def test_the_peak_survives_the_averaging(self, database, counters):
        """A short burst is the event worth keeping. A mean alone erases it."""
        monitor = ThroughputMonitor(database, sample_seconds=2, summary_seconds=10)

        counters.advance(seconds=2.0)
        monitor.tick()
        for _ in range(4):  # quiet
            counters.advance(rx_bytes=1_000, seconds=2.0)
            monitor.tick()
        counters.advance(rx_bytes=25_000_000, seconds=2.0)  # burst
        monitor.tick()
        counters.advance(rx_bytes=1_000, seconds=2.0)
        monitor.tick()

        summary = database.latest_throughput()
        assert summary is not None
        assert summary.rx_bps_peak > summary.rx_bps_mean * 3
        assert summary.rx_bps_peak == pytest.approx(100_000_000, rel=0.01)

    def test_a_quiet_window_records_zero_rather_than_nothing(self, database, counters):
        """Zero traffic is a measurement. Skipping the row would leave a gap
        indistinguishable from the monitor not running.
        """
        monitor = ThroughputMonitor(database, sample_seconds=2, summary_seconds=4)

        for _ in range(4):
            counters.advance(seconds=2.0)
            monitor.tick()

        summary = database.latest_throughput()
        assert summary is not None
        assert summary.rx_bytes == 0
        assert summary.rx_bps_mean == 0.0

    def test_a_counter_reset_does_not_invent_traffic(self, database, counters):
        monitor = ThroughputMonitor(database, sample_seconds=2, summary_seconds=4)

        counters.advance(rx_bytes=5_000_000, seconds=2.0)
        monitor.tick()
        counters.advance(rx_bytes=5_000_000, seconds=2.0)
        monitor.tick()

        # The interface restarts from zero.
        counters.rx = 0
        counters.monotonic += 2.0
        monitor.tick()

        for summary in database.list_throughput():
            assert summary.rx_bps_mean < 100_000_000

    def test_switching_interface_closes_the_previous_summary(self, database, counters, monkeypatch):
        """Ethernet unplugged, Wi-Fi takes over. Blending the two into one
        row would attribute traffic to the wrong link.
        """
        monitor = ThroughputMonitor(database, sample_seconds=2, summary_seconds=600)

        for _ in range(3):
            counters.advance(rx_bytes=250_000, seconds=2.0)
            monitor.tick()

        counters.interface = "wlan0"
        counters.rx = 0
        counters.monotonic += 2.0
        monitor.tick()
        counters.advance(rx_bytes=100_000, seconds=2.0)
        monitor.tick()

        interfaces = {s.interface_name for s in database.list_throughput()}
        assert "eth0" in interfaces

    def test_no_default_route_records_nothing(self, database, monkeypatch):
        """Nothing is reaching the internet, so there is nothing to
        attribute. A zero row here would claim a measurement never made.
        """
        from bandwidth_logger.core import monitor as monitor_module

        monkeypatch.setattr(monitor_module, "current_interface", lambda: (None, None))
        monitor = ThroughputMonitor(database, sample_seconds=1, summary_seconds=2)

        assert monitor.tick() is False
        assert database.count_throughput() == 0

    def test_a_partial_window_is_flushed_on_shutdown(self, database, counters):
        """SIGTERM should not silently discard the minute in progress."""
        monitor = ThroughputMonitor(database, sample_seconds=1, summary_seconds=3600)

        counters.advance(seconds=1.0)
        monitor.tick()
        counters.advance(rx_bytes=125_000, seconds=1.0)
        monitor.tick()

        assert database.count_throughput() == 0
        monitor.stop()
        monitor.run(max_iterations=0)
        assert database.count_throughput() == 1


class TestRetention:
    def test_samples_past_the_window_are_removed(self, database):
        database.set_int(SETTING_MONITOR_RETENTION_DAYS, 30)
        now = utc_now()
        make_summary(database, now - timedelta(days=45))
        make_summary(database, now - timedelta(days=31))
        kept = make_summary(database, now - timedelta(days=2))

        removed = ThroughputMonitor(database).prune()

        assert removed == 2
        assert [s.id for s in database.list_throughput()] == [kept]

    def test_pruning_never_touches_speed_test_records(self, database):
        """Throughput samples have a retention window. Test attempts do not,
        and never will -- that permanence is what the application is for.
        """
        database.set_int(SETTING_MONITOR_RETENTION_DAYS, 1)
        old = utc_now() - timedelta(days=400)
        database.insert_run(make_run(moment=old))
        make_summary(database, old)

        ThroughputMonitor(database).prune()

        assert database.count_runs() == 1
        assert database.count_throughput() == 0

    def test_the_retention_setting_is_honoured_and_clamped(self, database):
        monitor = ThroughputMonitor(database)

        database.set_int(SETTING_MONITOR_RETENTION_DAYS, 30)
        assert monitor.retention_days() == 30

        database.set_int(SETTING_MONITOR_RETENTION_DAYS, 0)
        assert monitor.retention_days() == 1


class TestConcurrentUsageOnTests:
    def test_a_test_records_what_else_was_in_flight(self, database, runner):
        """The point of the column: a test run over a busy link measures the
        capacity that was left, so a low reading needs this context to be
        interpretable rather than alarming.
        """
        from bandwidth_logger.core.models import TriggerType

        make_summary(database, utc_now() - timedelta(seconds=30), rx_mean=400_000_000.0)

        run = runner.run_test(TriggerType.MANUAL).run

        assert run.concurrent_rx_bps == pytest.approx(400_000_000.0)
        stored = database.get_run(run.id)
        assert stored.concurrent_rx_bps == pytest.approx(400_000_000.0)

    def test_no_monitor_data_leaves_the_columns_null(self, database, runner):
        """NULL, not zero. 'The monitor was not running' and 'the link was
        idle' are different facts.
        """
        from bandwidth_logger.core.models import TriggerType

        run = runner.run_test(TriggerType.MANUAL).run

        assert run.concurrent_rx_bps is None
        assert run.concurrent_tx_bps is None

    def test_stale_throughput_is_not_attributed_to_a_test(self, database, runner):
        from bandwidth_logger.core.models import TriggerType

        make_summary(database, utc_now() - timedelta(hours=6), rx_mean=400_000_000.0)

        run = runner.run_test(TriggerType.MANUAL).run

        assert run.concurrent_rx_bps is None


class TestExport:
    def test_throughput_exports_with_raw_and_display_columns(self, database, tmp_path):
        import csv

        from bandwidth_logger.storage.export_csv import export_throughput

        make_summary(database, utc_now() - timedelta(minutes=2), rx_mean=12_400_000.0)
        destination = tmp_path / "throughput.csv"

        assert export_throughput(database, destination) == 1

        with destination.open(encoding="utf-8", newline="") as handle:
            row = next(iter(csv.DictReader(handle)))
        assert row["rx_bps_mean"] == "12400000"
        assert row["rx_mbps_mean"] == "12.4"
        assert row["interface_name"] == "eth0"

    def test_export_does_not_alter_the_database(self, database, tmp_path):
        from bandwidth_logger.storage.export_csv import export_throughput

        make_summary(database, utc_now() - timedelta(minutes=1))
        before = database.count_throughput()

        export_throughput(database, tmp_path / "out.csv")

        assert database.count_throughput() == before
