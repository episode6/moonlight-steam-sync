#!/bin/sh
# Install (or update) moonlight-steam-sync from the latest GitHub release.
#
# Downloads the release zipapp (moonlight-steam-sync.pyz, spec 3.1) plus its
# published sha256 checksum, verifies it, and installs it as
# ~/.local/bin/moonlight-steam-sync. That is the entire install on SteamOS:
# no root, no pip, no compiler -- just curl and a Python 3.11+ already on
# PATH (stock SteamOS 3.x ships one; see README.md and AGENTS.md).
#
#   curl -fsSL https://raw.githubusercontent.com/episode6/moonlight-steam-sync/main/install.sh | sh
#
# Safe to re-run: it always fetches the latest tag and overwrites the
# previous install, so rerunning after a SteamOS update (or just to pick up
# a new release) is a no-op when already current and a plain upgrade
# otherwise. Set MOONLIGHT_STEAM_SYNC_VERSION to a specific tag (e.g. v0.1.0)
# to pin instead of tracking latest, and INSTALL_DIR to install somewhere
# other than ~/.local/bin.

set -eu

REPO="episode6/moonlight-steam-sync"
INSTALL_DIR="${INSTALL_DIR:-"$HOME/.local/bin"}"
BIN_NAME="moonlight-steam-sync"
VERSION="${MOONLIGHT_STEAM_SYNC_VERSION:-latest}"

if [ "$VERSION" = "latest" ]; then
    RELEASE_PATH="latest/download"
else
    RELEASE_PATH="download/${VERSION}"
fi

BASE_URL="https://github.com/${REPO}/releases/${RELEASE_PATH}"
ASSET="moonlight-steam-sync.pyz"

require() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "install.sh: '$1' is required but was not found on PATH." >&2
        exit 1
    }
}

require curl
require python3

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')
if [ "$PY_OK" != "1" ]; then
    echo "install.sh: python3 is $(python3 --version 2>&1), but moonlight-steam-sync needs 3.11+." >&2
    exit 1
fi

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

echo "Downloading ${ASSET} (${VERSION}) from ${REPO}..."
curl -fsSL "${BASE_URL}/${ASSET}" -o "${TMP_DIR}/${ASSET}"
curl -fsSL "${BASE_URL}/${ASSET}.sha256" -o "${TMP_DIR}/${ASSET}.sha256"

echo "Verifying checksum..."
# The published checksum file names the asset by its build path
# (dist/moonlight-steam-sync.pyz); recompute against just the basename here
# instead of relying on the recorded name matching the download layout.
EXPECTED=$(awk '{print $1}' "${TMP_DIR}/${ASSET}.sha256")
ACTUAL=$(sha256sum "${TMP_DIR}/${ASSET}" | awk '{print $1}')
if [ "$EXPECTED" != "$ACTUAL" ]; then
    echo "install.sh: checksum mismatch for ${ASSET}." >&2
    echo "  expected: ${EXPECTED}" >&2
    echo "  actual:   ${ACTUAL}" >&2
    exit 1
fi
echo "sha256: ${ACTUAL}"

mkdir -p "$INSTALL_DIR"
install -m 0755 "${TMP_DIR}/${ASSET}" "${INSTALL_DIR}/${BIN_NAME}"

echo "Installed ${BIN_NAME} to ${INSTALL_DIR}/${BIN_NAME}"

case ":$PATH:" in
    *":${INSTALL_DIR}:"*) ;;
    *)
        echo
        echo "${INSTALL_DIR} is not on your PATH. Add this to your shell's rc file:"
        echo
        echo "    export PATH=\"${INSTALL_DIR}:\$PATH\""
        echo
        ;;
esac

"${INSTALL_DIR}/${BIN_NAME}" --version
