#!/usr/bin/env bash
#
# Run the automated test suite.
#
#     ./packaging/run-tests.sh              # everything
#     ./packaging/run-tests.sh tests/unit   # a subset
#
# The suite uses a fake engine and recorded fixture output throughout. It
# never contacts a speed-test server and never consumes internet bandwidth.
# Every test runs against a temporary BANDWIDTH_LOGGER_HOME, so the real
# database, diagnostic log and systemd units are untouched.
#
# The GUI smoke test needs GTK 4 and a display; it skips itself when either
# is missing. To include it on a headless machine, install xvfb.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

PYTHON="${PYTHON:-python3}"

if ! "${PYTHON}" -c "import pytest" 2>/dev/null; then
    echo "pytest is not installed for ${PYTHON}." >&2
    echo "Install it with:  sudo apt-get install python3-pytest" >&2
    exit 1
fi

exec "${PYTHON}" -m pytest "${@:-tests}" -v
