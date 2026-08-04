"""Engine discovery and selection.

The Ookla Speedtest CLI is the only supported engine.

An open-source fallback (``speedtest-cli``) shipped in 1.0.0 and was removed
in 1.1.0, for two reasons found in use:

* It under-reported badly on fast connections -- roughly half the real figure
  on a gigabit line, because it cannot open enough parallel connections to
  fill one. For an application whose purpose is diagnosing a slow connection,
  a number that is quietly wrong by half is worse than no number at all.
* Ubuntu's ``speedtest-cli`` package owns ``/usr/bin/speedtest``, the same
  path Ookla's package installs to, so having it present made the better
  engine impossible to install.

Every row still records ``engine_name``, so history measured by the old
fallback stays identifiable and is annotated as such in the record details.
"""

from __future__ import annotations

from .base import SpeedTestEngine
from .ookla import OoklaEngine

#: Every engine the application knows how to drive, best first. The adapter
#: layer remains so another provider can be added without touching the
#: database, the scheduler or the interface.
ENGINE_CLASSES: tuple[type[SpeedTestEngine], ...] = (OoklaEngine,)

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
    installed engine is used. ``None`` means nothing is installed, which the
    interface reports rather than quietly measuring with something else.
    """
    if preference and preference != AUTOMATIC:
        chosen = engine_by_name(preference)
        if chosen is not None and chosen.is_available():
            return chosen
        if chosen is not None:
            return None

    available = available_engines()
    return available[0] if available else None
