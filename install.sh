#!/bin/sh
# Install (or update) moonlight-steam-sync from the latest GitHub release.
#
# Downloads the release zipapp (moonlight-steam-sync.pyz, spec 3.1) plus its
# published sha256 checksum, verifies it, and installs it as
# ~/.local/bin/moonlight-steam-sync. That is the entire install on SteamOS:
# no root, no pip, no compiler -- just curl and a Python 3.11+ already on
# PATH (stock SteamOS 3.x ships one -- currently 3.13.5; see
# README.md and AGENTS.md).
#
#   curl -fsSL https://raw.githubusercontent.com/episode6/moonlight-steam-sync/main/install.sh | sh
#
# A fresh SteamOS install does not have ~/.local/bin on PATH, so when the
# install directory is missing from PATH this script also appends an
# `export PATH=...` line to the shell's rc file (~/.bashrc, or ~/.zshrc for
# zsh) -- once; re-runs find the line already there. Set NO_MODIFY_PATH=1
# to skip that and only print the line instead.
#
# Safe to re-run: it always fetches the latest tag and overwrites the
# previous install (there is no version check, so it re-downloads and
# reinstalls every time even when already current -- that is what makes
# rerunning after a SteamOS update, or just to pick up a new release, safe).
# Set MOONLIGHT_STEAM_SYNC_VERSION to a specific tag (e.g. v0.1.0) to pin
# instead of tracking latest, and INSTALL_DIR to install somewhere other
# than ~/.local/bin.

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
require sha256sum

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
# Compare hashes rather than `sha256sum -c` against the recorded filename:
# harmless either way, and it keeps this working even against a
# ${ASSET}.sha256 recorded under a different name (e.g. a build path).
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
# Install to a temp name in the target directory first, then rename into
# place, so an install interrupted partway through never leaves a truncated
# executable at ${BIN_NAME}.
install -m 0755 "${TMP_DIR}/${ASSET}" "${INSTALL_DIR}/${BIN_NAME}.new"
mv -f "${INSTALL_DIR}/${BIN_NAME}.new" "${INSTALL_DIR}/${BIN_NAME}"

echo "Installed ${BIN_NAME} to ${INSTALL_DIR}/${BIN_NAME}"

# Write the rc line with a literal $HOME when installing to the default
# directory, so the rc file stays correct if the home directory ever moves.
if [ "$INSTALL_DIR" = "$HOME/.local/bin" ]; then
    PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
else
    PATH_LINE="export PATH=\"${INSTALL_DIR}:\$PATH\""
fi

case "$(basename "${SHELL:-sh}")" in
    zsh) RC_FILE="$HOME/.zshrc" ;;
    # bash is the SteamOS default, and a sensible guess for anything else;
    # ~/.bash_profile on Arch-derived systems (SteamOS included) sources
    # ~/.bashrc, so login shells pick the line up too.
    *) RC_FILE="$HOME/.bashrc" ;;
esac

case ":$PATH:" in
    *":${INSTALL_DIR}:"*) ;;
    *)
        echo
        if [ "${NO_MODIFY_PATH:-0}" != "0" ]; then
            echo "${INSTALL_DIR} is not on your PATH (NO_MODIFY_PATH is set). Add this to your shell's rc file:"
            echo
            echo "    ${PATH_LINE}"
        elif [ -f "$RC_FILE" ] && grep -qxF "$PATH_LINE" "$RC_FILE"; then
            echo "${INSTALL_DIR} is not on your PATH yet, but ${RC_FILE} already adds it."
            echo "Open a new shell, or run:  ${PATH_LINE}"
        else
            printf '\n# Added by moonlight-steam-sync install.sh\n%s\n' "$PATH_LINE" >> "$RC_FILE"
            echo "${INSTALL_DIR} was not on your PATH; added this line to ${RC_FILE}:"
            echo
            echo "    ${PATH_LINE}"
            echo
            echo "Open a new shell, or run that line now, for '${BIN_NAME}' to be found."
        fi
        echo
        ;;
esac

"${INSTALL_DIR}/${BIN_NAME}" --version
