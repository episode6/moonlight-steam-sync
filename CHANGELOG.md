# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project uses [semantic versioning](https://semver.org/).

## [Unreleased]

The first slices of the Decky-plugin CLI work
(`~/specs/moonlight-steam-sync/decky-plugin.md`, PR-1 and PR-2): additive
and optional, so every command's behaviour is unchanged when none of the
flags below are passed and no title has been pinned.

### Added

- `--json` (before the subcommand): switches stdout to one JSON object per
  line, moving human progress to stderr. See the README's "Machine-readable
  output" section for the event schema.
- `search "term"`: looks a title up against SteamGridDB and Steam's store
  the same way `sync`'s art phase does, and prints the candidates without
  writing anything.
- `host show|set|clear`: an active-host state file, so `launch`, `sync`,
  `list`, `ignore`, `status` and `doctor` can target a host other than the
  one in `config.toml` without editing it.
- `list --cached` and `status --host`: read the new per-host `moonlight
  list` cache (written automatically by `sync`, `list` and `ignore --all`
  on every successful run) instead of asking the host live.
- `list` reports `same-game-as: <name> (<host>)` when a second host's cache
  shows the same title published under a different name.
- `status --owned-apps PATH`: accepted and validated (a later release uses
  it to mark owned titles); no output changes yet.
- `doctor` gains `session:`, `active host:` and `cached hosts:` lines, plus
  an `owned-apps file:` line when `--owned-apps` is passed and an `ignore
  file:` line when `--ignore-file` is.
- `match "Name" --steam APPID | --sgdb ID | --none | --unpin`: pins what a
  title is matched to in the match cache (`"how": "pinned"`), the one cache
  entry `art --force` and `--retry-missing` never overwrite, and prints the
  equivalent `[overrides]` line to paste into `config.toml` (which still
  wins over a pin). A title that already has a shortcut loses its grid
  files and icon so the next `sync` fetches the new match's art (one Steam
  restart, like `remove`, reported under `--json` by the same `commit`
  event); `--defer-art` instead only marks the cached
  entry `stale_art` (acted on by `sync` in a later change) and leaves Steam
  alone. The mark survives re-resolution: `--unpin --defer-art` leaves a
  `"how": "unpinned"` placeholder that carries it, and the next run's
  search inherits it. `--force-name` accepts a title the host does not
  publish yet. Under `--json` it emits a `pinned` event.
- `--ignore-file PATH` on `sync`, `list` and `ignore`: a JSON list of
  Moonlight names ignored together with `config.toml`'s `ignore` (the
  union), so the Decky plugin can keep its own ignore list without editing
  the config. A missing or malformed file is exit 1 before anything is
  touched.
- `status --json`: `entry.stale_art` now reflects the match cache.

### Fixed

- `--json launch`: `start` and `exec` are now flushed before `moonlight
  stream` replaces the process, so a reader on the other end of a pipe (as
  the Decky plugin uses) actually sees them; a missing `moonlight` binary
  now yields `start`, `error` rather than `start`, `exec`, `error`.
- `sync --json`'s `summary` event: `stopped_early` now always agrees with
  `stop_reason` (both null, or both set), including on the `--dry-run`
  hard-stop and `Ctrl-C` paths.

## [0.2.0] - 2026-09-18

Stops the host app listing from crashing a large Apollo host. Minor bump
rather than patch because two host-list behaviours are removed with it.

### Changed

- The host's app list is now read with the plain `moonlight list <host>`
  instead of `moonlight list <host> --csv`. moonlight-qt's CSV mode loads
  box art for every app before it prints, which fires one box-art fetch per
  title at the host in a burst; on a large library that burst crashed an
  Apollo host outright. The plain form only asks for the app list.

### Removed

- The Moonlight host's own box art as a last-resort source for the portrait
  slot: it only ever came from the `--csv` output. A title that neither
  Steam's CDN nor SteamGridDB has art for now gets an empty portrait slot
  like any other empty slot.
- The `Hidden` and `App Collection Game` filtering of the host list, which
  were `--csv`-only columns too. An app hidden in the Moonlight client now
  syncs like any other; add it to `ignore` in `config.toml` to keep it out.

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
