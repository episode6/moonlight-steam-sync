# moonlight-steam-sync

Sync the game list published by a [Moonlight](https://moonlight-stream.org/) host
into Steam as non-Steam shortcuts, and dress each one with artwork from Steam's
own CDN and [SteamGridDB](https://www.steamgriddb.com/) -- the same file layout
Steam's own library UI and tools like SGDBoop use, so it disappears into a
normal Steam library instead of looking like a hand-added shortcut.

This is aimed at SteamOS (Steam Deck / Deck-likes) as a client of a Moonlight
host such as [Sunshine](https://github.com/LizardByte/Sunshine), but nothing
about it is SteamOS-specific beyond assuming a Linux Steam install and a
`moonlight` client (native binary or the Flathub flatpak) on `PATH`.

**Status:** feature complete; every subcommand below works (`sync`, `list`,
`ignore`, `remove`, `art`, `status`, `launch`, `doctor`), and the release
pipeline (a `.pyz` zipapp plus `install.sh`) is in place. **Not yet
verified against a real Steam Deck** -- the device checklist in the design
spec (section 8) is the gate before the first tagged release, `v0.1.0`,
which has not been cut yet. See [`AGENTS.md`](AGENTS.md) for the work plan
and the exact release procedure.

## Install

Once `v0.1.0` (or later) has been released:

```sh
curl -fsSL https://raw.githubusercontent.com/episode6/moonlight-steam-sync/main/install.sh | sh
```

This downloads the latest release's `moonlight-steam-sync.pyz` -- a single
executable zipapp with no dependencies to install -- verifies its published
sha256 checksum, and installs it as `~/.local/bin/moonlight-steam-sync`
(printing a `PATH` hint if that directory is not already on it). Re-run the
same command any time to upgrade to the latest release; it is a no-op when
already current. Set `MOONLIGHT_STEAM_SYNC_VERSION=vX.Y.Z` to pin a specific
release instead of tracking latest, or `INSTALL_DIR=/some/other/dir` to
install somewhere other than `~/.local/bin`.

**Until the first release is cut**, run from a git checkout instead:

```sh
git clone https://github.com/episode6/moonlight-steam-sync.git
cd moonlight-steam-sync
python3 -m moonlight_steam_sync --version
```

Either way, requires Python 3.11+ (for `tomllib`) and nothing else: the tool
has zero runtime dependencies. Stock SteamOS 3.x ships a Python new enough
for this already.

## Configure

`~/.config/moonlight-steam-sync/config.toml`, read once per run, never written
by the tool:

```toml
host = "MY-GAMING-PC"            # Moonlight host name, or --host
name_suffix = " (streaming)"     # appended to the Steam shortcut name, default ""
exe = "/home/deck/server-scripts/chimera/stream.sh"   # default: the tool's own path
launch_options = '"{name}"'      # template; default 'launch "{name}"' when exe is the tool
start_dir = ""                   # default: dirname(exe)
ignore = ["Desktop", "Steam Big Picture"]   # Moonlight app names never to add
restart_steam = true             # shut Steam down before writing and relaunch after
request_interval_ms = 250        # polite pacing between SteamGridDB/Steam calls

[steamgriddb]
api_key = ""                     # or env SGDB_API_KEY, or file ~/.config/moonlight-steam-sync/sgdb-api-key
community_fallback = true        # use community assets when no official one exists

[overrides]                      # per-title pins when auto-match is wrong
"Some Weird Launcher Name" = { steam = 1245620 }
"Fan Game" = { sgdb = 5247018 }
"Desktop" = { art = false }
```

Every key has a CLI flag override; flags win over the file, the file wins
over built-in defaults. The SteamGridDB API key is optional -- without it the
tool still resolves "official Steam art" via Steam's own store search and
CDN, it just skips community-submitted art. When set, the key is looked up in
this order: the config file (or its flag), then the `SGDB_API_KEY`
environment variable, then the contents of
`~/.config/moonlight-steam-sync/sgdb-api-key`.

Run `moonlight-steam-sync doctor` to see what the tool detects on your
machine (Steam install, Python version, `moonlight` binary, whether an API
key is configured, whether Steam is currently running).

The `moonlight` CLI is found in this order: the `MOONLIGHT_BIN` environment
variable (a full command line, e.g. `flatpak run --command=moonlight
com.moonlight_stream.Moonlight`), then a native `moonlight` on `PATH`, then
the [Flathub flatpak](https://flathub.org/apps/com.moonlight_stream.Moonlight)
if it is installed.

**Steam must restart** to notice new shortcuts and new artwork files. `sync`
does this once per run (`steam -shutdown`, wait for it to exit, write,
`steam -silent`) rather than per game, and only when something actually
changed. If Steam is not running, the file is simply written and Steam is
left alone. With `restart_steam = false` (or `--no-restart-steam`) the tool
never stops or starts Steam: if Steam is running when there is something to
write, it exits 2 without writing -- the artwork already on disk stays and is
picked up on the next restart, so quitting Steam and rerunning finishes the
job with no network calls.

## What it touches on disk

| path | why |
|---|---|
| `<steam>/userdata/<steamid3>/config/shortcuts.vdf` | the non-Steam shortcut store; rewritten once per run |
| `<steam>/userdata/<steamid3>/config/shortcuts.vdf.bak-<timestamp>` | a backup per write, newest five kept |
| `<steam>/userdata/<steamid3>/config/grid/` | artwork: `<appid>p`, `<appid>`, `<appid>_hero`, `<appid>_logo`, `<appid>_icon` |
| `~/.cache/moonlight-steam-sync/matches.json` | the title -> Steam/SteamGridDB match cache (`XDG_CACHE_HOME` honoured) |

`<steam>` is found automatically (`~/.local/share/Steam`, then `~/.steam/steam`
and `~/.steam/root`, which are symlinks to it on SteamOS); set `STEAM_ROOT` to
override that. With more than one Steam account on the machine, the one
`config/loginusers.vdf` marks `MostRecent` is used -- `doctor` prints which.

Steam holds `shortcuts.vdf` in memory and rewrites it on exit, so edits made
while it is running are lost: that is why the tool shuts Steam down before
writing. The file is written atomically (temp file plus rename) and never
rewritten at all when nothing changed, and artwork you picked by hand in the
Steam UI is never overwritten (same filenames, and an existing image means
"done"). Only images count for that: the `<appid>.json` the Steam UI drops
next to a logo records where the logo sits, so it never stands in for the
landscape art that shares its name.

## Usage

```
moonlight-steam-sync sync      [--host H] [--dry-run] [--no-art] [--limit N] [--retry-missing] [--no-restart-steam]
moonlight-steam-sync art       [--force] [--retry-missing] [--only "Name"] [--explain]
moonlight-steam-sync list      [--host H]
moonlight-steam-sync status
moonlight-steam-sync ignore    --all | "Name"...
moonlight-steam-sync remove    --all | "Name"...
moonlight-steam-sync launch    "Name" [-- extra moonlight flags]
moonlight-steam-sync doctor
```

### A first import

```sh
moonlight-steam-sync doctor                 # finds Steam, the user, moonlight, the key
moonlight-steam-sync list --host MY-GAMING-PC   # every app the host publishes: added / ignored / new
moonlight-steam-sync sync --dry-run         # the plan, with the art URL per slot; writes nothing under Steam
moonlight-steam-sync sync --limit 5         # five titles, to look at the tiles before doing 300
moonlight-steam-sync sync                   # the rest
```

`sync` works out what to add as *(what the host publishes) minus (the
`ignore` list) minus (what is already in Steam)*. "Already in Steam" means a
shortcut whose `Exe` is the configured `exe` -- not a name match -- so
shortcuts created earlier by other tools that already point at your stream
script are adopted as-is (same appid, so they keep launching), get their
artwork, and are never duplicated. There is no state file: delete a
shortcut in the Steam UI and the next `sync` brings it back unless you
ignore it.

The order within a run is deliberate: first the artwork for every planned
and adopted shortcut is fetched with Steam still running (grid files are
inert until a restart), then Steam is shut down, `shortcuts.vdf` is written
once, and Steam is relaunched. New tiles therefore appear fully dressed on
that single restart, and interrupting the long art phase costs nothing:
`shortcuts.vdf` is not touched until the art phase has finished.

Each run ends with a summary: shortcuts added and adopted, art slots filled
and still missing, titles with no match at all (an accepted outcome, see
below), and how many are still pending behind `--limit`.

Exit codes: `0` done (including "some titles or slots had no art"); `1`
usage or config error, no Steam install, or an unreadable `shortcuts.vdf`
(nothing is touched); `2` Steam is running and may not be restarted; `3`
Moonlight unreachable; `4` stopped early by repeated 429s or a dead network
(everything done so far is kept); `130` interrupted with Ctrl-C (same).
After `4` or `130`, rerun the same command to continue.

### Ignoring, removing, listing

- `ignore --all` prints an `ignore = [...]` TOML block of your current
  ignore list plus every host app that has no shortcut yet -- "draw a line
  under everything on the PC today". `ignore "Name"...` prints the block
  with those names added. Either way you paste it into `config.toml`
  yourself; the tool never edits the config.
- `remove "Name"...` (or `--all`) deletes the named owned shortcuts and their
  five grid files, with the same one-restart write as `sync`. Shortcuts that
  do not point at the configured `exe` are never touched. An unknown name is
  an error before anything happens.
- `list --host H` prints every app the host publishes with `added`,
  `ignored` or `new` in front of it, and the same totals line `sync` starts
  with.

### Artwork

Each shortcut gets the five files Steam's own library UI reads out of
`userdata/<id>/config/grid/`: a portrait capsule, a landscape header, a hero
banner, a logo and an icon. Per slot, the first source that has an image
wins:

1. **Steam's own CDN**, when the title resolves to a Steam appid -- the
   official store art, byte-for-byte what Steam shows for the real game.
2. **SteamGridDB**, for anything the CDN has no image for (and for non-Steam
   titles), preferring the styles that look like official box art and taking
   the highest-scored result.
3. **The Moonlight host's own box art**, as a last resort for the portrait.

Rules worth knowing:

- **Artwork you set by hand is never overwritten.** A slot that already has a
  file in `grid/` is skipped, because the Steam UI writes those very
  filenames. `art --force` is the way to re-fetch anyway; it *replaces* what
  is in the slot, including a file whose extension differs from the new one,
  so a slot never ends up holding both a `.png` and a `.jpg`.
- **A title or a slot with no art is not an error.** It is listed in the
  summary; fill it in from the Steam UI and the rule above keeps it.
- Unmatched titles and empty slots are remembered for 7 days so a re-run does
  not search for them again; `--retry-missing` asks anyway.
- `art --explain` prints the whole match chain for each title: what was
  searched, what matched, and which URL each slot came from.
- Pin a title that matches badly with `[overrides]` in the config, or turn
  art off for it entirely with `{ art = false }`.
- No WebP and no animated art: Steam cannot read either out of `grid/`.

A large Moonlight library (500+ titles) means a first `sync` can be a couple
thousand HTTP calls; at the default pacing that is on the order of 10-20
minutes, and it is safe to interrupt (`Ctrl-C`) and resume with the same
command -- nothing already written is redone: a downloaded image, a resolved
title and a written shortcut each record themselves on disk as they happen.
The only moment Ctrl-C is held off is the few seconds between Steam being
shut down and relaunched, so you can never end up with Steam down and the
file unwritten. Use `--limit N` to bring in a handful of titles at a time
instead of the whole library at once.

The tool paces itself between calls (`request_interval_ms`), backs off on
429s and 5xxs, and stops outright after five 429s in a row rather than burn
your API key -- or after five network failures in a row, which is the Wi-Fi
having gone rather than the art being missing. Either way everything already
written is kept, and re-running the same command picks up where it left off.

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```

The artwork tests run against recorded API responses in
`tests/fixtures/art/`, so the suite never touches the network. Those
recordings are currently synthetic -- see
[`tests/fixtures/art/README.md`](tests/fixtures/art/README.md) for what each
one covers and how to replace them with real captures via
`SGDB_API_KEY=... python3 scripts/record_fixtures.py`. The end-to-end tests
in `tests/test_sync_e2e.py` run the whole `sync` / `remove` / `ignore` /
`list` chain against a temporary Steam tree, a fake `moonlight` on `PATH`
and a fake Steam process, including the 500-title resumability test that
kills a run after N calls and checks the rerun does exactly the remaining
work.

See [`AGENTS.md`](AGENTS.md) for the module map, coding rules, and the
resumability contract every durable step in this codebase has to keep.

`.github/workflows/release.yml` builds the release zipapp with
`python -m zipapp` and smoke-runs `--version` and `doctor` against it on
Python 3.11/3.12/3.13 -- on every pull request as well as on a `v*` tag, so
the release pipeline itself is exercised by ordinary CI rather than only
proven the day a tag is pushed. Only attaching the built zipapp to a GitHub
release is tag-only. See [`CHANGELOG.md`](CHANGELOG.md) for what has
changed and [`AGENTS.md`](AGENTS.md#cutting-a-release) for the exact steps
to cut one.

## License

MIT, see [`LICENSE`](LICENSE).
