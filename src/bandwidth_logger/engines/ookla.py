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

#: Verified working on Ubuntu 24.04. Two details are load-bearing and were
#: both found by the commands failing in real use:
#:
#: * ``dist=jammy`` is forced. Ookla publishes no repository for Ubuntu 24.04
#:   ("noble") -- that path returns 404 -- so the unmodified upstream script
#:   reports the distribution as unsupported. The jammy build runs correctly
#:   on 24.04.
#: * ``speedtest-cli`` is removed first. Ubuntu's package for it owns
#:   ``/usr/bin/speedtest``, the same path Ookla's package installs to, so
#:   dpkg refuses to unpack while it is present.
INSTALL_COMMANDS: tuple[str, ...] = (
    "sudo apt-get remove -y speedtest-cli",
    "curl -s https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh"
    " | sudo env os=ubuntu dist=jammy bash",
    "sudo apt-get install -y speedtest",
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
                "Speedtest CLI \u2014 the same engine speedtest.net uses. That program is "
                "made and licensed by Ookla, not by this application, and its licence "
                "does not allow it to be included in this package, so it has to be "
                "installed separately.\n\n"
                "Run the three commands below in a terminal, in order. They need an "
                "administrator password because they install system software; "
                "Bandwidth Logger itself never needs administrator rights.\n\n"
                "The first command removes speedtest-cli if you have it. That package "
                "claims the same /usr/bin/speedtest filename, so Ookla's engine cannot "
                "install while it is present. The second forces the jammy repository "
                "because Ookla publishes no packages for Ubuntu 24.04 yet; the jammy "
                "build runs correctly on 24.04.\n\n"
                "Until this is installed, Bandwidth Logger records each attempt as a "
                "failure rather than measuring with something less accurate."
            ),
            commands=INSTALL_COMMANDS,
            url="https://www.speedtest.net/apps/cli",
            bundled=False,
        )
