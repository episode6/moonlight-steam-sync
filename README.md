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

**Status:** early scaffold. `doctor` works -- it now also reports which Steam
account `sync` would write to -- and so does `launch`, backed by the Moonlight
side (binary discovery, game list, streaming). The Steam side underneath
(binary VDF codec, the `shortcuts.vdf` reader/writer, Steam discovery and
restart) is in place too, and the artwork engine behind `art` / `status` is
implemented and tested on top of it. Every other subcommand below is still a
stub that prints "not implemented" and exits 1 until its PR lands. See the
work plan in [`AGENTS.md`](AGENTS.md) for what's coming and in what order.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/episode6/moonlight-steam-sync/main/install.sh | sh
```

*(placeholder -- `install.sh` and the release zipapp it downloads land in a
later PR; for now, run from a git checkout with `python3 -m moonlight_steam_sync`.)*

Requires Python 3.11+ (for `tomllib`) and nothing else: the tool has zero
runtime dependencies. Stock SteamOS 3.x ships a Python new enough for this
already.

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
does this once per run (`steam -shutdown`, write, relaunch) rather than per
game; pass `--no-restart-steam` to skip it if Steam is not currently running.

## What it touches on disk

| path | why |
|---|---|
| `<steam>/userdata/<steamid3>/config/shortcuts.vdf` | the non-Steam shortcut store; rewritten once per run |
| `<steam>/userdata/<steamid3>/config/shortcuts.vdf.bak-<timestamp>` | a backup per write, newest five kept |
| `<steam>/userdata/<steamid3>/config/grid/` | artwork: `<appid>p`, `<appid>`, `<appid>_hero`, `<appid>_logo`, `<appid>_icon` (written by the art phase, PR-4) |
| `~/.cache/moonlight-steam-sync/matches.json` | the title -> Steam/SteamGridDB match cache (PR-4) |

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
  filenames. `art --force` is the way to re-fetch anyway.
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
Use `--limit N` to bring in a handful of titles at a time instead of the
whole library at once.

The tool paces itself between calls (`request_interval_ms`), backs off on
429s and 5xxs, and stops outright after five 429s in a row rather than burn
your API key -- everything already written is kept, and re-running the same
command picks up where it left off.

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
`SGDB_API_KEY=... python3 scripts/record_fixtures.py`.

See [`AGENTS.md`](AGENTS.md) for the module map, coding rules, and the
resumability contract every durable step in this codebase has to keep.

## License

MIT, see [`LICENSE`](LICENSE).
