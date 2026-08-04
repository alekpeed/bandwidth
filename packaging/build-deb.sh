#!/usr/bin/env bash
#
# Build the Bandwidth Logger .deb package.
#
# The Debian control files live in packaging/debian/ so that the repository
# layout stays as specified. dpkg-buildpackage insists on finding them at
# ./debian, so this script stages them there, builds, and tidies up.
#
# Usage:
#     ./packaging/build-deb.sh            # build into dist/
#     ./packaging/build-deb.sh --lint     # build, then run lintian
#
# Build dependencies (Ubuntu 24.04):
#     sudo apt-get install debhelper dh-python python3-all python3-setuptools \
#                          fakeroot dpkg-dev

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${REPO_ROOT}/dist"
RUN_LINTIAN=0

for argument in "$@"; do
    case "${argument}" in
        --lint) RUN_LINTIAN=1 ;;
        *)
            echo "Unknown option: ${argument}" >&2
            exit 2
            ;;
    esac
done

cd "${REPO_ROOT}"

if [[ -e debian && ! -L debian ]]; then
    echo "error: ./debian exists and is not the staging symlink; refusing to touch it." >&2
    exit 1
fi

cleanup() {
    rm -f "${REPO_ROOT}/debian"
}
trap cleanup EXIT

ln -sfn packaging/debian "${REPO_ROOT}/debian"

echo "==> Building bandwidth-logger"
# -us -uc: unsigned. Signing is the release step's job, not the build's.
# -b: binary only; there is no upstream tarball to produce.
dpkg-buildpackage -us -uc -b

mkdir -p "${OUTPUT_DIR}"
# dpkg-buildpackage writes its products to the parent of the source tree.
PARENT="$(cd "${REPO_ROOT}/.." && pwd)"
moved=0
for artefact in "${PARENT}"/bandwidth-logger_*.deb \
                "${PARENT}"/bandwidth-logger_*.buildinfo \
                "${PARENT}"/bandwidth-logger_*.changes; do
    [[ -e "${artefact}" ]] || continue
    mv -f "${artefact}" "${OUTPUT_DIR}/"
    moved=1
done

if [[ "${moved}" -eq 0 ]]; then
    echo "error: no package was produced." >&2
    exit 1
fi

echo "==> Package written to ${OUTPUT_DIR}"
ls -1 "${OUTPUT_DIR}"

if [[ "${RUN_LINTIAN}" -eq 1 ]]; then
    echo "==> Running lintian"
    lintian --no-tag-display-limit "${OUTPUT_DIR}"/bandwidth-logger_*.deb || true
fi
