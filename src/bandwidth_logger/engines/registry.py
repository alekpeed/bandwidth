"""Engine discovery and selection.

Selection order, and why: the Ookla CLI is preferred because it is the
measurement most users mean by "speed test" and it reports jitter, packet
loss and per-direction latency. It cannot be shipped in the package, so when
it is absent the open-source ``speedtest-cli`` is used instead and the
application still works out of the box. Whichever ran is recorded on every
row, so results are never silently comparing two different measurements.
"""

from __future__ import annotations

from .base import SpeedTestEngine
from .ookla import OoklaEngine
from .speedtest_cli import SpeedtestCliEngine

#: Every engine the application knows how to drive, best first.
ENGINE_CLASSES: tuple[type[SpeedTestEngine], ...] = (OoklaEngine, SpeedtestCliEngine)

AUTOMATIC = "auto"


def known_engines() -> list[SpeedTestEngine]:
    """One instance of each supported engine, in preference order."""
    engines = [cls() for cls in ENGINE_CLASSES]
    engines.sort(key=lambda engine: engine.preference)
    return engines


def available_engines() -> list[SpeedTestEngine]:
    """Only the engines actually installed on this system."""
    return [engine for engine in known_engines() if engine.is_available()]


def engine_by_name(name: str) -> SpeedTestEngine | None:
    """Look up an engine by its stable identifier."""
    for engine in known_engines():
        if engine.name == name:
            return engine
    return None


def select_engine(preference: str = AUTOMATIC) -> SpeedTestEngine | None:
    """Choose the engine to run.

    With ``preference`` set to a specific engine name that engine is used if
    installed, and no substitution happens behind the user's back -- a
    deliberate choice is honoured or it fails visibly. With ``auto`` the best
    installed engine is used. ``None`` means nothing is installed.
    """
    if preference and preference != AUTOMATIC:
        chosen = engine_by_name(preference)
        if chosen is not None and chosen.is_available():
            return chosen
        if chosen is not None:
            return None

    available = available_engines()
    return available[0] if available else None
