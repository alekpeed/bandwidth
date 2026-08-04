"""Which local network interface the test went out through.

Read from the kernel via ``/proc`` and ``/sys`` rather than by shelling out,
so it costs nothing and cannot fail in interesting ways. Anything that cannot
be determined is returned as ``None`` and stored as NULL -- a guess would be
worse than a blank, since the point of this column is to tell Wi-Fi results
apart from Ethernet ones after the fact.
"""

from __future__ import annotations

from pathlib import Path

from .logging_setup import get_logger

log = get_logger("network_info")

PROC_ROUTE = Path("/proc/net/route")
SYS_CLASS_NET = Path("/sys/class/net")

#: Interface-name prefixes under systemd's predictable naming scheme.
_PREFIX_TYPES: tuple[tuple[str, str], ...] = (
    ("en", "Ethernet"),
    ("eth", "Ethernet"),
    ("wl", "Wi-Fi"),
    ("wlan", "Wi-Fi"),
    ("ww", "Mobile broadband"),
    ("tun", "VPN"),
    ("tap", "VPN"),
    ("wg", "VPN"),
    ("ppp", "Dial-up or PPP"),
    ("br", "Bridge"),
    ("docker", "Container bridge"),
    ("veth", "Virtual"),
    ("lo", "Loopback"),
)


def default_interface(proc_route: Path = PROC_ROUTE) -> str | None:
    """Name of the interface carrying the default route."""
    try:
        lines = proc_route.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for line in lines[1:]:
        fields = line.split()
        # Columns: Iface Destination Gateway Flags RefCnt Use Metric Mask ...
        if len(fields) < 8:
            continue
        interface, destination, flags = fields[0], fields[1], fields[3]
        try:
            route_is_up = int(flags, 16) & 0x1
        except ValueError:
            continue
        if destination == "00000000" and route_is_up:
            return interface
    return None


def connection_type(interface: str | None, sys_class_net: Path = SYS_CLASS_NET) -> str | None:
    """Best available description of the link, e.g. ``Ethernet`` or ``Wi-Fi``.

    The kernel's wireless marker is authoritative when present; the name
    prefix is only a fallback.
    """
    if not interface:
        return None

    device = sys_class_net / interface
    try:
        if (device / "wireless").exists() or (device / "phy80211").exists():
            return "Wi-Fi"
    except OSError:  # pragma: no cover - /sys unreadable
        pass

    for prefix, label in _PREFIX_TYPES:
        if interface.startswith(prefix):
            return label
    return None


def describe_connection() -> tuple[str | None, str | None]:
    """``(interface_name, connection_type)`` for the current default route."""
    interface = default_interface()
    return interface, connection_type(interface)
