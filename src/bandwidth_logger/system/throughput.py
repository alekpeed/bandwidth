"""Reading how much traffic is actually flowing.

This is a different measurement from a speed test, and the distinction
matters. A speed test measures **capacity** by deliberately saturating the
link. This measures **usage** by reading counters the kernel maintains
anyway: no traffic is generated, nothing is transmitted, and the cost is one
file read per interface.

The counters live in ``/sys/class/net/<interface>/statistics/`` and count
bytes since the interface came up. Throughput is the difference between two
readings divided by the time between them.

Two facts about those counters shape the code:

* **They reset.** An interface that goes down and up starts from zero, and a
  reboot resets everything. A naive difference then yields a huge negative
  number, or -- worse, if the sign is discarded -- a huge positive one. A
  reading lower than its predecessor is treated as a reset and produces no
  measurement rather than a fabricated one.
* **They wrap.** 64-bit counters do not realistically wrap, but 32-bit ones
  can, and the two are indistinguishable from userspace. Since a wrap and a
  reset are handled the same way -- discard the interval -- this needs no
  special case, only that the discard exists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .logging_setup import get_logger
from .network_info import connection_type, default_interface

log = get_logger("throughput")

SYS_CLASS_NET = Path("/sys/class/net")

#: Bytes per second above which a reading is treated as implausible. 100 GB/s
#: is far beyond any consumer link, so a value this large means the counter
#: was reset or misread rather than that the traffic really happened.
IMPLAUSIBLE_BYTES_PER_SECOND = 100_000_000_000


@dataclass(frozen=True, slots=True)
class CounterReading:
    """One reading of an interface's byte counters."""

    interface: str
    rx_bytes: int
    tx_bytes: int
    #: Monotonic clock, so a clock adjustment between readings cannot produce
    #: a negative or absurd interval.
    monotonic: float


@dataclass(frozen=True, slots=True)
class ThroughputSample:
    """Traffic observed between two readings."""

    interface: str
    rx_bytes: int
    tx_bytes: int
    seconds: float

    @property
    def rx_bits_per_second(self) -> float:
        return (self.rx_bytes * 8) / self.seconds if self.seconds > 0 else 0.0

    @property
    def tx_bits_per_second(self) -> float:
        return (self.tx_bytes * 8) / self.seconds if self.seconds > 0 else 0.0


def read_counters(interface: str, root: Path = SYS_CLASS_NET) -> CounterReading | None:
    """Read one interface's byte counters, or ``None`` if unreadable.

    Unreadable is a normal outcome, not an error: the interface may have been
    removed between choosing it and reading it.
    """
    statistics = root / interface / "statistics"
    try:
        rx = int((statistics / "rx_bytes").read_text().strip())
        tx = int((statistics / "tx_bytes").read_text().strip())
    except (OSError, ValueError):
        return None
    return CounterReading(interface=interface, rx_bytes=rx, tx_bytes=tx, monotonic=time.monotonic())


def difference(previous: CounterReading, current: CounterReading) -> ThroughputSample | None:
    """Traffic between two readings, or ``None`` when it cannot be trusted.

    Returns nothing when the readings are from different interfaces, when no
    time has passed, when either counter went backwards (a reset), or when
    the implied rate is physically implausible. In every one of those cases a
    number could be produced -- and would be wrong.
    """
    if previous.interface != current.interface:
        return None

    seconds = current.monotonic - previous.monotonic
    if seconds <= 0:
        return None

    rx_delta = current.rx_bytes - previous.rx_bytes
    tx_delta = current.tx_bytes - previous.tx_bytes
    if rx_delta < 0 or tx_delta < 0:
        log.debug("Counter reset on %s; discarding the interval", current.interface)
        return None

    if max(rx_delta, tx_delta) / seconds > IMPLAUSIBLE_BYTES_PER_SECOND:
        log.warning(
            "Implausible throughput on %s (%d/%d bytes in %.2fs); discarding",
            current.interface, rx_delta, tx_delta, seconds,
        )
        return None

    return ThroughputSample(
        interface=current.interface, rx_bytes=rx_delta, tx_bytes=tx_delta, seconds=seconds
    )


def current_interface() -> tuple[str | None, str | None]:
    """The interface carrying the default route, and how it is connected."""
    interface = default_interface()
    return interface, connection_type(interface)


def format_rate(bits_per_second: float | None) -> str:
    """Human-readable rate, scaled to a sensible unit.

    ``None`` is blank rather than zero -- the same rule the rest of the
    application follows, because "not measured" and "no traffic" are
    different facts.
    """
    if bits_per_second is None:
        return ""
    if bits_per_second < 1_000:
        return f"{bits_per_second:.0f} bit/s"
    if bits_per_second < 1_000_000:
        return f"{bits_per_second / 1_000:.1f} kbit/s"
    if bits_per_second < 1_000_000_000:
        return f"{bits_per_second / 1_000_000:.2f} Mbps"
    return f"{bits_per_second / 1_000_000_000:.2f} Gbps"


def format_bytes(total: int | None) -> str:
    """Human-readable byte total, in the binary units file managers use."""
    if total is None:
        return ""
    value = float(total)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover - unreachable, loop returns
