# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project uses [semantic versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-09-20

A new flag, so a minor bump.

### Added

- **`--hide-host-apps`** on `sync`, `list` and `status`: the two apps every
  Sunshine / Apollo host publishes out of the box, `Desktop` and `Steam Big
  Picture` (matched trimmed, case-insensitively), become a new title kind,
  `host-app`, written as a **hidden** shortcut. The Decky plugin launches
  them from buttons on its Quick Access panel, and with this flag those
  buttons replace the two library tiles. The entry keeps its ordinary
  `AppName` (name + `name_suffix`) and so its appid: an existing tile is
  hidden in place (`N to hide`, never a `replace`), with its artwork, icon
  and controller layout untouched, and it still gets artwork. `ignore`
  wins over the kind; the kind wins over `stream`, so a `Desktop` that
  matches an owned game never takes that game's Stream button or makes a
  `duplicate`. `--park-unpublished` parks an unpublished one like any
  entry and leaves a published one hidden; without the flag the two are
  ordinary shortcuts again and `sync --park-unpublished` shows them.
  `status --hide-host-apps` no longer reports such an entry as `parked`
  unless the active host stopped publishing it. Off by default: a CLI user
  without the plugin's buttons would lose their only launcher for the two
  (decky spec 3.14).
- `--json`: `"host-app"` as a `title.kind` / `app.kind` value, and the keys
  `plan.host_app`, `summary.added_by_kind["host-app"]` and
  `entry.host_app` -- all only when `--hide-host-apps` is passed, so every
  other run's events are unchanged key for key and `schema` stays 1. `art
  --json` reports a hidden default host app as `host-app`, not `stream`.

## [0.3.1] - 2026-09-20

A patch release from the first device run of the Decky plugin.

### Fixed

- **"moonlight CLI not found" and "5 consecutive network failures" when run
  by the Decky plugin.** Decky Loader is a PyInstaller-frozen binary and
  exports `LD_LIBRARY_PATH=/tmp/_MEI...` (its bundled, older OpenSSL) to
  every child. Under it `flatpak list` died in the dynamic linker
  (``version `OPENSSL_3.4.0' not found``), which the tool read as "the
  flatpak is not installed", and the tool's own `import ssl` failed the same
  way, so `search`, `sync` and `art` stopped after five network failures.
  The tool now detects a frozen parent (`LD_LIBRARY_PATH_ORIG`, or a `_MEI*`
  entry on `LD_LIBRARY_PATH`), restores the original library path and
  re-executes itself once, same pid and argv (new module `procenv.py`). With
  neither trace in the environment nothing changes.
- A `flatpak list` that fails or times out is no longer reported as a bare
  `not found`: `doctor`'s `moonlight:` line and the not-found error carry
  the exit code and the first line of flatpak's stderr. A flatpak that runs
  and simply does not list the app keeps the old wording byte for byte.

## [0.3.0] - 2026-09-20

The CLI half of the Decky-plugin work
(`~/specs/moonlight-steam-sync/decky-plugin.md`, PR-1 through PR-4): a
machine-readable event stream, an active-host state file and per-host list
cache, title-matching pins, and owned-app awareness with hidden shortcuts,
so a companion Decky plugin (`episode6/moonlight-steam-sync-decky`, which
pins this release) can drive `sync` from Steam's Game Mode. Every addition
is a new flag, a new subcommand or an additive event key: with none of
them passed, and no `matches.json` entry pinned or flagged stale, every
command's stdout, stderr, exit code and on-disk bytes are unchanged from
`v0.2.0` (the three sanctioned exceptions -- the new `doctor` lines, the
per-host list cache, and `list`'s `same-game-as` line -- are called out
below).

### Added

- **`--json`** (before the subcommand, every command): switches stdout to
  one JSON object per line and nothing else, moving all human progress to
  stderr, so a caller like the Decky plugin can read a line-oriented event
  stream without parsing prose. Every object has an `"event"` key; the
  stream always opens with `start` (`schema`, `version`, `command`) and, on
  a non-zero exit, closes with `error` (`exit`, `message`). `--json launch`
  flushes `start` and `exec` before `moonlight stream` replaces the
  process, so a reader on the other end of a pipe (the Decky plugin) sees
  them even though the process never returns; a missing `moonlight` binary
  yields `start`, `error` rather than `start`, `exec`, `error`. `sync`'s
  `summary` event's `stopped_early` always agrees with `stop_reason` (both
  null, or both set), including on the `--dry-run` hard-stop and `Ctrl-C`
  paths. See the README's "Machine-readable output" section for the full
  schema, which is versioned (`schema: 1`) and additive-only.
- **`search "term"`**: looks a title up against SteamGridDB (when a key is
  configured) and Steam's own store, the same way `sync`'s art phase does,
  and prints the candidates -- with `--owned-apps`, flagged `owned` -- to
  fix a wrong match by hand or from the plugin's Titles page. Writes
  nothing.
- **`match "Name" (--steam APPID | --sgdb ID | --none | --unpin) [--defer-art] [--force-name]`**:
  pins what a title resolves to in the match cache (`matches.json`,
  `"how": "pinned"`), the one cache entry `art --force` and
  `--retry-missing` never overwrite, and prints the equivalent
  `[overrides]` line to paste into `config.toml` (an `[overrides]` entry
  still wins over a pin). A title that already has a shortcut loses its
  grid files and icon so the next `sync` fetches the new match's art, with
  the same one-restart write as `remove`; `--defer-art` instead only flags
  the cached entry `stale_art` and leaves Steam alone, and that flag now
  outlives the entry it was set on -- `--unpin --defer-art` leaves a
  `"how": "unpinned"` placeholder carrying it, and the next resolution
  inherits it. A name the resolved host does not publish and that has no
  shortcut is a usage error unless `--force-name` pins it ahead of time.
  Under `--json` it emits a `pinned` event, preceded on the immediate
  path by the same `commit` event `remove` reports its write with.
- **`--ignore-file PATH`** on `sync`, `list` and `ignore`: a JSON list of
  Moonlight names ignored together with `config.toml`'s `ignore` (the
  union), so the Decky plugin can keep its own ignore list without editing
  the config. A missing or malformed file is exit 1 before anything is
  touched.
- **`host show|set|clear`**: an active-host state file
  (`$XDG_STATE_HOME/moonlight-steam-sync/active-host`, never
  `config.toml`), so `launch`, `sync`, `list`, `ignore`, `status`, `client`
  and `match` can target a host other than the one in `config.toml`
  without editing it; `--host` on any command is still per-invocation and
  never writes the state file.
- **The per-host `moonlight list` cache** (`<cache dir>/hosts/<slug>.json`),
  written automatically by every successful `sync`, `list` and
  `ignore --all`: `list --cached` and `status --host`/`status --owned-apps`
  read it instead of asking the host live, `match` uses it to check a
  pinned name is one the host actually publishes, and `list` reports
  `same-game-as: <name> (<host>)` when a second host's cache shows the
  same title under a different name.
- **`sync --owned-apps PATH`** (also accepted by `list`, `status` and
  `search`): a JSON file of the Steam games the account owns (the Decky
  plugin writes it before every run; its `steamid3` must match the Steam
  user `sync` would write to, else exit 1 with nothing touched). A
  published title that resolves -- by an exact, override or pinned match,
  never a fuzzy one -- to an appid in the file gets a **hidden** shortcut
  named exactly as the owned game (no `name_suffix`) instead of a visible
  tile, so the plugin can put a Stream button on the real game's own
  library page; `sync` resolves every published title before planning
  (cache-first, so a finished library still makes zero calls, and also
  under `--no-art`, which then downloads nothing). A change of ownership,
  display name or match is a *replacement*, never an in-place rename: it
  swaps the shortcut and renames (or, for a stale match, deletes and
  re-fetches) its five grid files to the new appid, downloading nothing
  for a plain rename; `--limit` counts replacements and additions together
  and never splits one, and `--dry-run` prints each replacement. Two
  titles resolving to the same owned game collide on `AppName`: the first
  in host-list order gets the hidden entry, the rest are reported
  `duplicate` and get no tile at all (an entry either already had is
  removed with its art); pinning a duplicate to `--none` restores its
  tile. `--owned-apps` refuses a `launch_options` template without
  `{name}` (exit 1, nothing touched), since a hidden entry's name is the
  owned game's and the launch options are the only way back to the
  Moonlight name.
- **`sync --park-unpublished`**: hides in place every owned shortcut the
  active host does not publish (same appid; art, pin and controller layout
  kept) and shows published ones again, so switching hosts and back is one
  write and no downloads. `status` reports `parked` per entry and
  `list --owned-apps` labels parked titles.
- **`sync --client-shortcut`, the `client [-- extra]` subcommand,
  `remove --client` and `doctor --client-shortcut`**: one hidden
  `Moonlight` shortcut that runs `moonlight-steam-sync client` to open the
  Moonlight client itself (no host, no game) -- the plugin's *Open
  Moonlight* button needs a Steam-launched app in Game Mode. It always
  runs this tool (even when `exe` is a user script), gets no artwork, and
  is identified by its launch options plus its exe rather than by name;
  `remove --all` deletes it only when `exe` is the tool itself.
- **`--commit {restart,await-exit,refuse}`** on `sync`, `art`, `remove` and
  `match`: which of three ways `commit_shortcuts()` gets `shortcuts.vdf`
  onto disk while Steam might be running. `restart` is today's
  shutdown-write-relaunch (now available even when `restart_steam = false`
  says otherwise); `refuse` is `--no-restart-steam` under a shared name
  (`sync` keeps the old flag too; the two are mutually exclusive); and
  `await-exit` is for the plugin running in Game Mode, where the tool must
  never start Steam itself (gamescope-session restarts the client on its
  own once it exits): when there is something to write and Steam is
  running, the tool announces it is waiting (`awaiting-steam-exit` under
  `--json`, `{"timeout_s": 60}`), polls every 100 ms for up to 60 s for
  Steam to exit, writes the instant it is gone, and never starts it
  (`commit.restarted` is `false`). Still up after 60 s is exit 2 with the
  file untouched; `Ctrl-C` during that wait exits 130 immediately, also
  untouched (`summary.stop_reason: "interrupted"`), since the tool has not
  touched Steam and there is nothing to protect -- only the write itself
  (backup rotation plus the atomic replace) defers `SIGINT`. Steam not
  running writes at once in every mode, and an art-only run with nothing
  to write returns `commit.written: false` without waiting, leaving the
  restart to the caller.
- **`doctor`** gains `session:` (`gamescope`/`desktop`/`unknown`, for the
  plugin's own device probes), `active host:` and `cached hosts:` lines
  unconditionally, plus an `owned-apps file:` line when `--owned-apps` is
  passed, an `ignore file:` line when `--ignore-file` is, and a
  `client shortcut:` line when `--client-shortcut` is. Being a diagnostic,
  a bad `--owned-apps`/`--ignore-file` file is reported inline rather than
  exiting non-zero.
- `sync`'s and `list`'s JSON events gained the fields the sections above
  need: `plan` carries the kind counts, replacements, park flips, removals
  and `duplicates`; a `replace` event precedes the art phase per
  replacement; `title.kind`, `app.kind` and `app.duplicate_of` are real
  (previously always `"shortcut"`/`null`); `entry.parked`, `entry.client`,
  `entry.stale_art`, `entry.cached` and `entry.cached_when` are real; and
  `sync`'s `summary` gains `replaced`, `removed`, `duplicates` and
  `added_by_kind` (`{"stream": n, "shortcut": n}`, new entries only,
  decky spec 3.13 A3).

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
