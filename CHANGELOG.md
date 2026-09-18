# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project uses [semantic versioning](https://semver.org/).

## [Unreleased]

Nothing yet.

## [0.1.1] - 2026-09-18

The first findings from installing on a fresh SteamOS box.

### Changed

- `install.sh` now adds `~/.local/bin` to `PATH` itself when it is missing,
  by appending an `export PATH=...` line to `~/.bashrc` (or `~/.zshrc` under
  zsh) once, instead of only printing a hint. A fresh SteamOS install does
  not have that directory on `PATH`, so the previous behaviour left
  `moonlight-steam-sync` installed but not found. `NO_MODIFY_PATH=1`
  restores the print-only behaviour.
- CI and the release smoke test now run on Python 3.11 (the floor) and
  exactly 3.13.5 (what a stock SteamOS ships, verified on device) instead
  of the loose 3.11/3.12/3.13 spread.

## [0.1.0] - 2026-09-17

The first release. Cut ahead of the device checklist (spec section 8)
deliberately, so that installing on a SteamOS box is a single `curl` of
`install.sh` rather than a git checkout and a `PYTHONPATH`; the checklist
itself is what this release exists to make easy to run.

### Added

- Every subcommand in the design spec (section 3.3): `sync`, `art`, `list`,
  `status`, `ignore`, `remove`, `launch`, `doctor`.
- Binary `shortcuts.vdf` reading and writing, byte-identical round trip,
  with automatic adoption of existing (e.g. SteamTinkerLaunch-created)
  shortcuts by `Exe` path.
- Artwork resolution and download: Steam's own CDN first, SteamGridDB
  second, the Moonlight host's own box art as a last resort for the
  portrait slot -- laid out in `grid/` exactly the way Steam's UI and tools
  like SGDBoop write it.
- Moonlight host discovery via the native `moonlight` CLI or its flatpak,
  and `launch` to stream a title directly.
- The resumability contract (spec 3.9): every durable step's progress lives
  on disk, downloads are atomic (`.part` + rename), the match cache flushes
  after every title, and re-running after any interruption does only the
  remaining work.
- Rate limiting and backoff against SteamGridDB and the Steam store, with a
  hard stop after five consecutive 429s or network failures.
- A single Steam restart per run (`steam -shutdown` / `-silent`), never one
  per shortcut.
- Config via `~/.config/moonlight-steam-sync/config.toml` (`tomllib`,
  stdlib), with CLI flag and environment variable overrides; the tool never
  writes this file itself.
- `moonlight-steam-sync doctor` to report the detected environment (Steam
  install, Steam user, Python version, `moonlight` binary, API key
  presence, whether Steam is currently running).
- CI (`ci.yml`): `ruff check .` and `pytest` across Python 3.11, 3.12 and
  3.13 -- the SteamOS Python spread.
- The release pipeline (`release.yml`): builds `moonlight-steam-sync.pyz`
  with `python -m zipapp`, smoke-tests `--version` and `doctor` against it
  on all three Pythons on every pull request as well as on a release tag,
  and (tag-only) publishes a GitHub release with the zipapp and its sha256
  checksum attached.
- `install.sh`: downloads the latest (or a pinned) release's zipapp,
  verifies its checksum, and installs it to `~/.local/bin/moonlight-steam-sync`.
- `--version` reads the installed package's metadata
  (`importlib.metadata.version`) when available, and falls back to the
  module's own `__version__` for the zipapp, which is never pip-installed.
