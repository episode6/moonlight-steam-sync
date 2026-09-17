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
restart) is in place too. Every other subcommand below is still a stub that
prints "not implemented" and exits 1 until its PR lands. See the work plan in
[`AGENTS.md`](AGENTS.md) for what's coming and in what order.

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

A large Moonlight library (500+ titles) means a first `sync` can be a couple
thousand HTTP calls; at the default pacing that is on the order of 10-20
minutes, and it is safe to interrupt (`Ctrl-C`) and resume with the same
command -- nothing already written is redone. Use `--limit N` to bring in a
handful of titles at a time instead of the whole library at once.

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```

See [`AGENTS.md`](AGENTS.md) for the module map, coding rules, and the
resumability contract every durable step in this codebase has to keep.

## License

MIT, see [`LICENSE`](LICENSE).
