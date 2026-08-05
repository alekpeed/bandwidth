"""Test doubles.

The fake engine here is a real program executed as a real subprocess, not a
mock. That is deliberate: the parts of the runner most worth testing are the
ones that only exist because a subprocess is involved -- timeouts,
termination, non-zero exits, garbled output. Patching those away would test
the wrong thing.

No test in the normal suite touches the internet.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from bandwidth_logger.core.models import EngineMeasurement, ErrorCategory
from bandwidth_logger.core.result_parser import ParseError, parse_ookla_json
from bandwidth_logger.engines.base import EngineError, InstallGuidance, SpeedTestEngine

FAKE_ENGINE_SCRIPT = Path(__file__).parent / "fake_engine.py"

# Behaviours the fake engine script understands.
BEHAVIOUR_SUCCESS = "success"
BEHAVIOUR_SPARSE = "sparse"
BEHAVIOUR_ZEROS = "zeros"
BEHAVIOUR_MALFORMED = "malformed"
BEHAVIOUR_FAILURE = "failure"
BEHAVIOUR_DNS_FAILURE = "dns-failure"
BEHAVIOUR_HANG = "hang"
BEHAVIOUR_SLOW = "slow"


class FakeEngine(SpeedTestEngine):
    """Drives ``tests/support/fake_engine.py`` instead of a real engine."""

    name = "fake"
    display_name = "Fake engine (tests only)"
    executable = ""
    preference = 1

    def __init__(
        self,
        behaviour: str = BEHAVIOUR_SUCCESS,
        *,
        delay_seconds: float = 0.0,
        version_string: str | None = "9.9.9",
    ) -> None:
        self.behaviour = behaviour
        self.delay_seconds = delay_seconds
        self._version = version_string
        #: Number of times build_command() was called, so a test can prove
        #: that two requests produced only one execution.
        self.invocations = 0

    def is_available(self) -> bool:
        return True

    def executable_path(self) -> str:
        return sys.executable

    def version(self) -> str | None:
        return self._version

    def build_command(self) -> list[str]:
        self.invocations += 1
        return [
            os.path.realpath(sys.executable),
            str(FAKE_ENGINE_SCRIPT),
            "--behaviour",
            self.behaviour,
            "--delay",
            str(self.delay_seconds),
        ]

    def parse_output(self, stdout: str) -> EngineMeasurement:
        try:
            measurement = parse_ookla_json(stdout, engine_version=self._version)
        except ParseError as exc:
            raise EngineError(ErrorCategory.MALFORMED_OUTPUT, str(exc)) from exc
        measurement.engine_name = self.name
        return measurement

    def install_guidance(self) -> InstallGuidance:
        return InstallGuidance(summary="Fake engine", detail="Used only by the test suite.")
