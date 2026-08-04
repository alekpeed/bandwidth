"""The interface every speed-test engine implements.

An engine's job is narrow: say whether it is installed, produce an argument
vector, and turn that command's structured output into an
:class:`~..core.models.EngineMeasurement`. It does not run the process, keep
time, or touch the database -- the test runner owns all of that, so both
engines take exactly the same execution path.
"""

from __future__ import annotations

import abc
import shutil
from dataclasses import dataclass

from ..core.models import EngineMeasurement, ErrorCategory


class EngineError(Exception):
    """An engine could not produce a usable measurement.

    Carries a normalised :class:`~..core.models.ErrorCategory` alongside the
    full text, so the interface can show one short line while the stored
    record keeps the complete diagnostic message.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True, slots=True)
class InstallGuidance:
    """How a user can install a missing engine, without leaving the window.

    ``commands`` are shown as copyable text and, where the engine's licence
    permits redistribution, offered as a one-click install. Ookla's CLI is
    proprietary and is not bundled: the application explains what it is and
    where it comes from rather than pretending it is part of this program.
    """

    summary: str
    detail: str
    commands: tuple[str, ...] = ()
    url: str | None = None
    bundled: bool = False


class SpeedTestEngine(abc.ABC):
    """Adapter for one speed-test command-line program."""

    #: Stable identifier stored in ``test_runs.engine_name``.
    name: str = "engine"

    #: Human-readable name for the interface.
    display_name: str = "Speed-test engine"

    #: Executable looked up on PATH.
    executable: str = ""

    #: Lower sorts first when choosing automatically.
    preference: int = 100

    #: Test server to pin, or None to let the engine choose.
    #:
    #: Engines pick a server by lowest latency, which is not the same as
    #: fastest -- on a gigabit line the difference between the nearest and the
    #: quickest server can be hundreds of Mbps. Left unpinned, the engine may
    #: also choose differently between runs, which makes a history of results
    #: incomparable: a change in the number could mean the connection changed,
    #: or merely that a different server answered.
    server_id: str | None = None

    # -- availability ------------------------------------------------------

    def executable_path(self) -> str | None:
        """Absolute path of the engine binary, or ``None`` if not installed."""
        return shutil.which(self.executable) if self.executable else None

    def is_available(self) -> bool:
        return self.executable_path() is not None

    @abc.abstractmethod
    def version(self) -> str | None:
        """Engine version string, or ``None`` when it cannot be determined."""

    @abc.abstractmethod
    def install_guidance(self) -> InstallGuidance:
        """What to tell the user when this engine is missing."""

    # -- execution ---------------------------------------------------------

    @abc.abstractmethod
    def build_command(self) -> list[str]:
        """Argument vector for one test.

        Returned as a list and executed without a shell. No element is ever
        derived from user input.
        """

    @abc.abstractmethod
    def parse_output(self, stdout: str) -> EngineMeasurement:
        """Turn structured engine output into a normalised measurement.

        Raises :class:`EngineError` with
        :attr:`~..core.models.ErrorCategory.MALFORMED_OUTPUT` when the output
        cannot be read as a complete result.
        """

    def classify_failure(self, exit_code: int, stdout: str, stderr: str) -> tuple[str, str]:
        """Map a non-zero exit into ``(error_category, message)``."""
        combined = f"{stderr}\n{stdout}".strip()
        category = classify_error_text(combined)
        message = combined or f"Engine exited with status {exit_code} and produced no output."
        return category, message


#: Substrings that identify a failure reason, checked in order. First match
#: wins, so the more specific patterns come first.
_ERROR_PATTERNS: tuple[tuple[str, str], ...] = (
    (ErrorCategory.DNS_FAILURE, "temporary failure in name resolution"),
    (ErrorCategory.DNS_FAILURE, "name or service not known"),
    (ErrorCategory.DNS_FAILURE, "could not resolve host"),
    (ErrorCategory.DNS_FAILURE, "nodename nor servname"),
    (ErrorCategory.DNS_FAILURE, "getaddrinfo"),
    (ErrorCategory.NO_NETWORK, "network is unreachable"),
    (ErrorCategory.NO_NETWORK, "network unreachable"),
    (ErrorCategory.NO_NETWORK, "no route to host"),
    (ErrorCategory.NO_NETWORK, "connection refused"),
    (ErrorCategory.NO_NETWORK, "cannot assign requested address"),
    (ErrorCategory.SERVER_UNAVAILABLE, "no servers"),
    (ErrorCategory.SERVER_UNAVAILABLE, "noservers"),
    (ErrorCategory.SERVER_UNAVAILABLE, "unable to connect to server"),
    (ErrorCategory.SERVER_UNAVAILABLE, "could not retrieve or read the configuration"),
    (ErrorCategory.SERVER_UNAVAILABLE, "cannot retrieve speedtest configuration"),
    (ErrorCategory.SERVER_UNAVAILABLE, "failed to fetch server list"),
    (ErrorCategory.SERVER_UNAVAILABLE, "unable to retrieve server list"),
    (ErrorCategory.SERVER_UNAVAILABLE, "503 service unavailable"),
    (ErrorCategory.PERMISSION_ERROR, "permission denied"),
    (ErrorCategory.PERMISSION_ERROR, "operation not permitted"),
    (ErrorCategory.PERMISSION_ERROR, "access is denied"),
    (ErrorCategory.TIMEOUT, "timed out"),
    (ErrorCategory.TIMEOUT, "timeout"),
    (ErrorCategory.MALFORMED_OUTPUT, "json"),
    (ErrorCategory.ENGINE_MISSING, "command not found"),
    (ErrorCategory.ENGINE_MISSING, "no such file or directory"),
)


def classify_error_text(text: str) -> str:
    """Normalise engine chatter into a stable error category.

    Engines word their failures differently and change that wording between
    releases, so this is best-effort by design: anything unrecognised becomes
    ``process_error`` and the full text is still stored on the record.
    """
    if not text or not text.strip():
        return ErrorCategory.PROCESS_ERROR
    lowered = text.lower()
    for category, needle in _ERROR_PATTERNS:
        if needle in lowered:
            return category
    return ErrorCategory.PROCESS_ERROR
