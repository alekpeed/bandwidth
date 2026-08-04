"""Official Ookla Speedtest CLI adapter.

Licensing, which decides how this engine is delivered: the Ookla CLI is
proprietary freeware distributed under Ookla's own End User Licence
Agreement. It permits personal use but not redistribution inside a
third-party package, so the .deb must not bundle it and does not declare it
as a Depends. Instead the application detects whether it is installed and, if
not, walks the user through installing it from Ookla's own apt repository --
naming it as Ookla's software rather than part of this application. See
docs/DEPENDENCIES.md.

The first invocation of the Ookla CLI normally prompts for acceptance of that
licence and Ookla's privacy notice. A prompt would hang a scheduled test with
no window attached, so acceptance is passed explicitly on the command line;
the user is shown both documents in the setup dialog before the first test
runs.
"""

from __future__ import annotations

import re
import subprocess

from ..core.models import EngineMeasurement, ErrorCategory
from ..core.result_parser import ParseError, parse_ookla_json
from .base import EngineError, InstallGuidance, SpeedTestEngine

_VERSION_PATTERN = re.compile(r"(\d+\.\d+(?:\.\d+)*)")

INSTALL_COMMANDS: tuple[str, ...] = (
    "curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | sudo bash",
    "sudo apt-get install speedtest",
)


class OoklaEngine(SpeedTestEngine):
    """Runs ``speedtest --format=json``."""

    name = "ookla"
    display_name = "Ookla Speedtest CLI"
    executable = "speedtest"
    preference = 10

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

    def is_available(self) -> bool:
        """Confirm the binary on PATH is really Ookla's.

        ``speedtest`` is an ambiguous name -- on some systems it is a symlink
        to the unrelated ``speedtest-cli``. Running the wrong program would
        make ``--format=json`` fail in a confusing way, so the identity is
        checked rather than assumed.
        """
        path = self.executable_path()
        if path is None:
            return False
        try:
            completed = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        banner = f"{completed.stdout} {completed.stderr}".lower()
        return "ookla" in banner or "speedtest by ookla" in banner

    def build_command(self) -> list[str]:
        path = self.executable_path()
        if path is None:
            raise EngineError(
                ErrorCategory.ENGINE_MISSING,
                "The Ookla Speedtest CLI ('speedtest') was not found on PATH.",
            )
        return [
            path,
            "--format=json",
            "--accept-license",
            "--accept-gdpr",
            "--progress=no",
        ]

    def parse_output(self, stdout: str) -> EngineMeasurement:
        try:
            return parse_ookla_json(stdout, engine_version=self.version())
        except ParseError as exc:
            raise EngineError(ErrorCategory.MALFORMED_OUTPUT, str(exc)) from exc

    def install_guidance(self) -> InstallGuidance:
        return InstallGuidance(
            summary="Ookla Speedtest CLI is not installed",
            detail=(
                "Bandwidth Logger measures your connection with the official Ookla "
                "Speedtest CLI. That program is made and licensed by Ookla, not by "
                "this application, and its licence does not allow it to be included "
                "in this package, so it has to be installed separately.\n\n"
                "Running the two commands below adds Ookla's own software source and "
                "installs the engine. They need an administrator password because "
                "they install system software; Bandwidth Logger itself never needs "
                "administrator rights.\n\n"
                "If you would rather not install Ookla's software, Bandwidth Logger "
                "can use the open-source speedtest-cli engine instead."
            ),
            commands=INSTALL_COMMANDS,
            url="https://www.speedtest.net/apps/cli",
            bundled=False,
        )
