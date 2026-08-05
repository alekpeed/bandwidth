"""Shared fixtures.

Every test runs against a temporary ``BANDWIDTH_LOGGER_HOME``. Nothing in the
suite reads or writes the real database, the real diagnostic log, or the real
systemd units.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point every application path at a throwaway directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("BANDWIDTH_LOGGER_HOME", str(home))

    from bandwidth_logger.system import logging_setup

    logging_setup.reset_for_tests()
    logging_setup.configure_logging(stream=False)
    yield home
    logging_setup.reset_for_tests()


@pytest.fixture(autouse=True)
def no_real_engines(monkeypatch, request):
    """Make a real speed-test engine undiscoverable.

    Without this, a suite run on a machine that happens to have
    ``speedtest`` or ``speedtest-cli`` installed would resolve a real engine
    wherever a test did not pass an explicit fake -- and quietly run a live
    speed test, consuming bandwidth and taking minutes. The specification is
    explicit that the routine suite must not do that, so the guarantee is
    enforced here rather than left to each test remembering.

    Tests that pass an engine explicitly are unaffected: the runner uses the
    engine it was given without consulting discovery. A test that genuinely
    needs discovery can opt out with ``@pytest.mark.allow_engine_discovery``.
    """
    if request.node.get_closest_marker("allow_engine_discovery"):
        yield
        return

    import shutil

    real_which = shutil.which
    engine_binaries = {"speedtest", "speedtest-cli"}

    def which(command, *args, **kwargs):
        name = str(command).rsplit("/", 1)[-1]
        if name in engine_binaries:
            return None
        return real_which(command, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    yield


@pytest.fixture()
def database(isolated_home):
    """An open, migrated database in the temporary home."""
    from bandwidth_logger.storage.database import Database

    db = Database().open()
    yield db
    db.close()


@pytest.fixture()
def fake_engine():
    from support import FakeEngine

    return FakeEngine()


@pytest.fixture()
def runner(database, fake_engine):
    """A runner wired to the fake engine and a short timeout."""
    from bandwidth_logger.core.test_runner import TestRunner

    return TestRunner(database, engine=fake_engine, timeout_seconds=30)


def make_run(**overrides):
    """Build a :class:`TestRun` with sensible defaults for storage tests."""
    from bandwidth_logger.core.models import (
        RunStatus,
        TestRun,
        TriggerType,
        to_local_iso,
        to_utc_iso,
        utc_now,
    )

    moment = overrides.pop("moment", utc_now())
    defaults = {
        "trigger_type": TriggerType.MANUAL,
        "started_at_utc": to_utc_iso(moment),
        "started_at_local": to_local_iso(moment),
        "completed_at_utc": to_utc_iso(moment),
        "completed_at_local": to_local_iso(moment),
        "duration_ms": 10_000,
        "status": RunStatus.SUCCESS,
        "engine_name": "fake",
        "engine_version": "9.9.9",
        "application_version": "1.0.0",
        "created_at_utc": to_utc_iso(moment),
        "download_bps": 95_000_000,
        "upload_bps": 19_000_000,
        "idle_latency_ms": 10.5,
        "isp_name": "Example Internet",
        "server_name": "Example Telecom",
        "external_ip": "203.0.113.42",
    }
    defaults.update(overrides)
    return TestRun(**defaults)
