"""Open-source ``speedtest-cli`` adapter.

This is the fallback engine. It is Apache-2.0 licensed and packaged in Ubuntu
as ``speedtest-cli``, so unlike the Ookla CLI it can be declared as an
ordinary package dependency and the application works the moment it is
installed.

It measures less than Ookla's engine -- no jitter, no packet loss, no
per-direction latency. Those columns stay empty for its results instead of
being filled with zeros.
"""

from __future__ import annotations

import re
import subprocess

from ..core.models import EngineMeasurement, ErrorCategory
from ..core.result_parser import ParseError, parse_speedtest_cli_json
from .base import EngineError, InstallGuidance, SpeedTestEngine

_VERSION_PATTERN = re.compile(r"(\d+\.\d+(?:\.\d+)*)")


class SpeedtestCliEngine(SpeedTestEngine):
    """Runs ``speedtest-cli --json``."""

    name = "speedtest-cli"
    display_name = "speedtest-cli (open source)"
    executable = "speedtest-cli"
    preference = 20

    def version(self) -> str | None:
        path = self.executable_path()
        if path is None:
            return None
        try:
            completed = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        match = _VERSION_PATTERN.search(f"{completed.stdout} {completed.stderr}")
        return match.group(1) if match else None

    def build_command(self) -> list[str]:
        path = self.executable_path()
        if path is None:
            raise EngineError(
                ErrorCategory.ENGINE_MISSING,
                "'speedtest-cli' was not found on PATH.",
            )
        return [path, "--json", "--secure"]

    def parse_output(self, stdout: str) -> EngineMeasurement:
        try:
            return parse_speedtest_cli_json(stdout, engine_version=self.version())
        except ParseError as exc:
            raise EngineError(ErrorCategory.MALFORMED_OUTPUT, str(exc)) from exc

    def install_guidance(self) -> InstallGuidance:
        return InstallGuidance(
            summary="speedtest-cli is not installed",
            detail=(
                "speedtest-cli is a free, open-source speed-test program available "
                "from Ubuntu's own software archive. It is normally installed "
                "automatically alongside Bandwidth Logger."
            ),
            commands=("sudo apt-get install speedtest-cli",),
            url="https://github.com/sivel/speedtest-cli",
            bundled=False,
        )
