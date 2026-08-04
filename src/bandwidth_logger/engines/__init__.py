"""Speed-test engine adapters.

Everything engine-specific lives behind :class:`~.base.SpeedTestEngine`, so a
different provider can be substituted without touching the database, the
scheduler or the interface.
"""

from .base import EngineError, SpeedTestEngine
from .registry import (
    available_engines,
    engine_by_name,
    known_engines,
    select_engine,
)

__all__ = [
    "EngineError",
    "SpeedTestEngine",
    "available_engines",
    "engine_by_name",
    "known_engines",
    "select_engine",
]
