"""Standard user directories for application data, configuration and state.

Follows the XDG Base Directory Specification so that everything the
application writes lives under the user's home directory and never requires
elevated privileges.

Every location can be redirected with ``BANDWIDTH_LOGGER_HOME``, which the
test suite uses to keep real user data untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIRNAME = "bandwidth-logger"
DATABASE_FILENAME = "bandwidth-logger.sqlite3"
LOG_FILENAME = "application.log"

_OVERRIDE_ENV = "BANDWIDTH_LOGGER_HOME"


def _override_root() -> Path | None:
    raw = os.environ.get(_OVERRIDE_ENV)
    if not raw:
        return None
    return Path(raw).expanduser()


def _xdg_dir(env_var: str, default_relative: str) -> Path:
    """Resolve an XDG directory, honouring the test override."""
    override = _override_root()
    if override is not None:
        return override / default_relative / APP_DIRNAME

    raw = os.environ.get(env_var)
    if raw and raw.startswith("/"):
        base = Path(raw)
    else:
        base = Path.home() / default_relative
    return base / APP_DIRNAME


def data_dir() -> Path:
    """Durable application data. Holds the authoritative SQLite database."""
    return _xdg_dir("XDG_DATA_HOME", ".local/share")


def config_dir() -> Path:
    """User configuration. Settings are database-backed; this holds little."""
    return _xdg_dir("XDG_CONFIG_HOME", ".config")


def state_dir() -> Path:
    """Volatile-but-persistent state such as the rotating diagnostic log."""
    return _xdg_dir("XDG_STATE_HOME", ".local/state")


def database_path() -> Path:
    return data_dir() / DATABASE_FILENAME


def log_path() -> Path:
    return state_dir() / LOG_FILENAME


def systemd_user_unit_dir() -> Path:
    """Directory holding per-user systemd units.

    Deliberately a user-level path: the scheduler must never install a
    root-level system service.
    """
    override = _override_root()
    if override is not None:
        return override / ".config/systemd/user"

    raw = os.environ.get("XDG_CONFIG_HOME")
    base = Path(raw) if raw and raw.startswith("/") else Path.home() / ".config"
    return base / "systemd/user"


def ensure_directories() -> None:
    """Create every directory the application writes to.

    Safe to call repeatedly; called once during start-up before any write.
    """
    for path in (data_dir(), config_dir(), state_dir()):
        path.mkdir(parents=True, exist_ok=True)
