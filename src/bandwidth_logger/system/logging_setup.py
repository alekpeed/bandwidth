"""Diagnostic logging.

The SQLite database is the authoritative test history. This log exists only to
diagnose problems -- above all, failures to write to the database itself. It
rotates so it cannot grow without bound.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from . import paths

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3

_configured = False


def configure_logging(*, verbose: bool = False, stream: bool = True) -> logging.Logger:
    """Install the rotating file handler exactly once per process."""
    global _configured
    logger = logging.getLogger("bandwidth_logger")
    if _configured:
        return logger

    level = logging.DEBUG if verbose or os.environ.get("BANDWIDTH_LOGGER_DEBUG") else logging.INFO
    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter(LOG_FORMAT)

    try:
        paths.state_dir().mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            paths.log_path(),
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - depends on a broken filesystem
        print(f"bandwidth-logger: cannot open diagnostic log: {exc}", file=sys.stderr)

    if stream:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger below the application root logger."""
    return logging.getLogger(f"bandwidth_logger.{name}")


def reset_for_tests() -> None:
    """Drop handlers so a subsequent configure_logging() takes effect."""
    global _configured
    logger = logging.getLogger("bandwidth_logger")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    _configured = False
