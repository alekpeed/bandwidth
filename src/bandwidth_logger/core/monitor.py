"""The continuous throughput monitor.

Runs as a long-lived user service, reads the kernel's byte counters every few
seconds, and writes one summary row per minute.

Why per-minute summaries rather than raw samples: at a two-second cadence a
day of raw readings is 43,000 rows, and almost all of them say "nothing much
happened". Summarising to one row a minute keeps the interesting part -- the
peak -- while storing 1,440 rows a day. A ten-second burst at full line rate
is visible in ``rx_bps_peak`` even though the minute's mean is unremarkable,
which is exactly the event a coarser sample would erase.

The monitor is deliberately separate from the test scheduler. Scheduled tests
are short, occasional and expensive; this is continuous and nearly free, and
mixing them would mean one could not run without the other.
"""

from __future__ import annotations

import signal
import threading
from dataclasses import dataclass, field
from datetime import timedelta

from .. import APPLICATION_VERSION
from ..storage.database import (
    SETTING_MONITOR_INTERVAL_SECONDS,
    SETTING_MONITOR_RETENTION_DAYS,
    Database,
    DatabaseError,
    ThroughputSummary,
)
from ..system.logging_setup import get_logger
from ..system.throughput import (
    CounterReading,
    current_interface,
    difference,
    read_counters,
)
from .models import to_local_iso, to_utc_iso, utc_now

log = get_logger("monitor")

DEFAULT_SAMPLE_SECONDS = 2
#: One summary row covers this long. Chosen so a day is 1,440 rows.
SUMMARY_SECONDS = 60
#: Housekeeping is hourly: often enough to bound growth, rare enough to cost
#: nothing measurable.
PRUNE_EVERY_SECONDS = 3600


@dataclass
class _Accumulator:
    """Collects readings until a summary is due."""

    interface: str
    connection_type: str | None
    started_at: object
    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_peak: float = 0.0
    tx_peak: float = 0.0
    seconds: float = 0.0
    count: int = 0
    _rates: list[float] = field(default_factory=list)

    def add(self, sample) -> None:  # type: ignore[no-untyped-def]
        self.rx_bytes += sample.rx_bytes
        self.tx_bytes += sample.tx_bytes
        self.seconds += sample.seconds
        self.count += 1
        self.rx_peak = max(self.rx_peak, sample.rx_bits_per_second)
        self.tx_peak = max(self.tx_peak, sample.tx_bits_per_second)

    def summarise(self) -> ThroughputSummary | None:
        """Turn the collected readings into one row, or nothing if empty."""
        if self.count == 0 or self.seconds <= 0:
            return None
        ended_at = utc_now()
        return ThroughputSummary(
            interface_name=self.interface,
            started_at_utc=to_utc_iso(self.started_at),
            started_at_local=to_local_iso(self.started_at),
            ended_at_utc=to_utc_iso(ended_at),
            duration_ms=int(round(self.seconds * 1000)),
            sample_count=self.count,
            rx_bytes=self.rx_bytes,
            tx_bytes=self.tx_bytes,
            rx_bps_mean=(self.rx_bytes * 8) / self.seconds,
            tx_bps_mean=(self.tx_bytes * 8) / self.seconds,
            rx_bps_peak=self.rx_peak,
            tx_bps_peak=self.tx_peak,
            connection_type=self.connection_type,
            application_version=APPLICATION_VERSION,
            created_at_utc=to_utc_iso(ended_at),
        )


class ThroughputMonitor:
    """Samples the interface counters and stores per-minute summaries."""

    def __init__(
        self,
        database: Database,
        *,
        sample_seconds: float | None = None,
        summary_seconds: float = SUMMARY_SECONDS,
    ) -> None:
        self.database = database
        self._forced_sample_seconds = sample_seconds
        self.summary_seconds = summary_seconds
        self._stop = threading.Event()
        self._previous: CounterReading | None = None
        self._accumulator: _Accumulator | None = None
        self._elapsed_since_summary = 0.0
        self._elapsed_since_prune = 0.0

    # -- configuration -----------------------------------------------------

    def sample_seconds(self) -> float:
        """How often the counters are read.

        Clamped so a stray setting can neither spin the CPU nor coarsen the
        data past usefulness.
        """
        if self._forced_sample_seconds is not None:
            return float(self._forced_sample_seconds)
        configured = self.database.get_int(
            SETTING_MONITOR_INTERVAL_SECONDS, DEFAULT_SAMPLE_SECONDS
        )
        return float(max(1, min(configured, 60)))

    def retention_days(self) -> int:
        return max(1, min(self.database.get_int(SETTING_MONITOR_RETENTION_DAYS, 30), 3650))

    # -- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        """Ask the loop to finish after the current interval."""
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """Stop cleanly on SIGTERM, which is how systemd asks."""
        for received in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(received, lambda *_: self.stop())
            except ValueError:  # pragma: no cover - not the main thread
                pass

    def run(self, *, max_iterations: int | None = None) -> int:
        """Sample until stopped. Returns the number of summaries written.

        ``max_iterations`` exists so tests can run the real loop for a
        bounded number of passes rather than against a mock.
        """
        log.info(
            "Throughput monitor starting (sampling every %.0fs, %ds summaries, %d-day retention)",
            self.sample_seconds(), int(self.summary_seconds), self.retention_days(),
        )
        written = 0
        iterations = 0

        while not self._stop.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1

            interval = self.sample_seconds()
            if self.tick():
                written += 1

            self._elapsed_since_prune += interval
            if self._elapsed_since_prune >= PRUNE_EVERY_SECONDS:
                self._elapsed_since_prune = 0.0
                self.prune()

            # Interruptible: SIGTERM does not have to wait out the interval.
            self._stop.wait(interval)

        # Do not discard a partial minute on the way out.
        if self._flush():
            written += 1
        log.info("Throughput monitor stopped after writing %d summaries", written)
        return written

    def tick(self) -> bool:
        """One sampling pass. True when a summary row was written."""
        interface, link_type = current_interface()
        if interface is None:
            # No default route: nothing is reaching the internet, so there is
            # nothing to attribute traffic to. Recording zero would claim a
            # measurement that was not made.
            self._previous = None
            return False

        reading = read_counters(interface)
        if reading is None:
            self._previous = None
            return False

        wrote = False
        if self._previous is not None:
            sample = difference(self._previous, reading)
            if sample is not None:
                if self._accumulator is None or self._accumulator.interface != interface:
                    # The route moved, e.g. Ethernet unplugged and Wi-Fi took
                    # over. Close the old summary rather than blending two
                    # interfaces into one row.
                    wrote = self._flush()
                    self._accumulator = _Accumulator(
                        interface=interface, connection_type=link_type, started_at=utc_now()
                    )
                    self._elapsed_since_summary = 0.0

                self._accumulator.add(sample)
                self._elapsed_since_summary += sample.seconds

                if self._elapsed_since_summary >= self.summary_seconds:
                    wrote = self._flush() or wrote
        elif self._accumulator is None:
            self._accumulator = _Accumulator(
                interface=interface, connection_type=link_type, started_at=utc_now()
            )

        self._previous = reading
        return wrote

    def _flush(self) -> bool:
        """Write the accumulated summary, if there is one."""
        if self._accumulator is None:
            return False
        summary = self._accumulator.summarise()
        self._accumulator = None
        self._elapsed_since_summary = 0.0
        if summary is None:
            return False

        try:
            self.database.insert_throughput_summary(summary)
        except DatabaseError as exc:
            # A lost summary is regrettable but not worth stopping the
            # monitor for: the next minute will try again, and the speed-test
            # history -- the record that must not have holes -- is unaffected.
            log.error("Could not store a throughput summary: %s", exc)
            return False
        return True

    def prune(self) -> int:
        """Delete summaries older than the retention window."""
        cutoff = utc_now() - timedelta(days=self.retention_days())
        try:
            removed = self.database.delete_throughput_before(to_utc_iso(cutoff))
        except DatabaseError as exc:
            log.error("Could not prune old throughput samples: %s", exc)
            return 0
        if removed:
            log.info("Pruned %d throughput sample(s) older than %d days", removed, self.retention_days())
        return removed
