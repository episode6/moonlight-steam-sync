# Agent guidance for moonlight-steam-sync

This is the harness-neutral agent guidance for this repo (module map, coding
rules, the resumability contract, how to test, fixtures). `CLAUDE.md` is a
symlink to this file so Claude Code picks it up automatically; other agent
harnesses can read this file directly.

## What this tool does

Reads the game list a [Moonlight](https://moonlight-stream.org/) host
publishes (via the `moonlight` CLI or its flatpak) and adds each title as a
non-Steam shortcut in the local Steam install, with artwork pulled from
Steam's own CDN and [SteamGridDB](https://www.steamgriddb.com/) laid out
exactly the way Steam's UI and tools like SGDBoop write it. See
`~/specs/moonlight-steam-sync/initial-build.md` (sections 2 and 3) for the
full design; this file is the day-to-day operating summary.

## Hard rules

- **Python 3.11 floor.** `tomllib` (stdlib, 3.11+) is what makes a
  dependency-free TOML config possible; do not add a `tomli` fallback for
  older Pythons. Stock SteamOS 3.x ships Python new enough for this already.
- **Zero runtime dependencies.** `pyproject.toml`'s `dependencies` stays `[]`.
  `pytest`, `ruff`, and `vdf` are dev-only (`vdf` is a *test oracle* to check
  the hand-rolled binary VDF codec against, never imported by the shipped
  package).
- **stdlib only.** No third-party runtime imports, anywhere under
  `src/moonlight_steam_sync/`.
- **Nothing user-specific in the repo.** No real host names, no
  `~/.moonlight_server` paths, no MAC addresses, no API keys, no personal
  file paths. All of that lives in the user's own `config.toml` (never
  committed) or, later, in the separate `server-scripts` repo. If you need a
  concrete example for a test or the README, use the placeholder values from
  spec 3.2 (`MY-GAMING-PC`, `/home/deck/server-scripts/...`) -- they are
  already fictional stand-ins, not real values.
- **The tool never writes `config.toml`.** `ignore --all` *prints* TOML lines
  for the user to paste in; it does not edit the file. This is a deliberate
  decision (spec 6.7), not an oversight -- don't silently change it.

## Module map (spec 3.4)

```
moonlight_steam_sync/
  __main__.py      argparse, subcommands, exit codes, logging, SIGINT handling
  config.py        TOML + env + flags -> Config dataclass; key lookup order
  vdf.py           binary KeyValues loads/dumps (maps, str, i32, i64, u64) -- byte-identical round trip
  shortcuts.py     Shortcut dataclass, appid(), read/write ShortcutsFile with backup + atomic replace,
                   owned-entry detection, name<->launch-options templating
  steam.py         Steam root + userdata discovery (single user, else loginusers.vdf MostRecent -> steamid3),
                   grid paths, is_running(), shutdown()/relaunch()
  moonlight.py     find binary (native `moonlight`, else flatpak), list(host) -> [App(name, id, hidden, boxart_path)],
                   stream(host, name, extra)
  art/
    sgdb.py        API client (urllib): search, game(platformdata), grids/heroes/logos/icons
    steamstore.py  storesearch, GetApps icon hash, CDN URL builders
    resolve.py     title -> Match{steam_appid?, sgdb_id?, confidence, how}; match cache (flushed per title)
    select.py      per-slot asset choice policy; download; mime sniff -> ext
    apply.py       write grid files for an appid (skip existing), set icon field
  sync.py          the orchestration: list -> diff -> art -> write -> restart; progress lines
```

`__main__.py` and `config.py` landed in PR-1; `vdf.py`, `shortcuts.py` and
`steam.py` (the whole Steam side) in PR-2. Every subcommand in `__main__.py`
except `doctor` is still a stub that exits 1 with "not implemented" until the
PR that implements the rest of its chain lands (`moonlight.py` in PR-3,
`art/` in PR-4, `sync.py` orchestration + the resumability e2e test in PR-5,
`remove`/`status`/`list` polish in PR-6). Do not add code to a module ahead
of its PR without checking the work plan first -- the modules are split the
way they are so independent PRs can land in parallel.

### Working on the Steam side (`vdf.py`, `shortcuts.py`, `steam.py`)

- **`dumps(loads(x)) == x`, byte for byte, is the invariant.** Steam deletes
  a `shortcuts.vdf` it cannot parse, so anything that loses a field, a key's
  original spelling, an integer's width or a non-UTF-8 name is a bug even if
  it "works". `vdf.py` therefore rejects duplicate keys and trailing bytes
  rather than papering over them, and `Shortcut` preserves unknown fields and
  the key order it read.
- **`Shortcut.exe` / `.start_dir` hold the value *as stored*, quotes and
  all**, because the appid is `crc32(quoted Exe + AppName) | 0x80000000`.
  Use `Shortcut.create()` to build one from a bare path and
  `.exe_path`/`.start_dir_path` to read one back.
- **Never shell out to Steam directly.** `steam.ProcessRunner` is the seam;
  tests inject a fake. `$STEAM_ROOT` overrides root discovery, which is how
  tests (and anyone with an unusual install) point the tool at another tree.
- **`ShortcutsFile.write()` writes nothing when the bytes are unchanged** and
  is the only non-incremental step in a run (contract item 4 below). It
  rotates five timestamped backups and `os.replace`s a temp file into place.

## The resumability contract (spec 3.9)

The libraries this tool deals with are large (the biggest known host
publishes 500+ titles, so a first run is 2,500+ HTTP calls and tens of
minutes even at polite pacing). Every PR that adds a durable step -- not just
the ones that mention resumability by name -- must keep these invariants:

1. **Every durable step is idempotent and its completion is visible on
   disk**, never only in memory. A slot is done when
   `grid/<appid><suffix>.<ext>` exists and `<ext>` is one Steam actually
   displays (`steam.GRID_IMAGE_EXTENSIONS`; `steam.find_grid_file` is the
   only place that answers this, so `<appid>.json` -- the logo-position
   sidecar -- never passes for the landscape slot whose stem it shares); a
   title is resolved when it is in `matches.json`; a shortcut exists when it
   is in `shortcuts.vdf`.
   Re-running after any failure (network drop, `Ctrl-C`, a 429 storm, the
   device sleeping) does only the remaining work.
2. **The match cache is flushed after every title**, not batched to the end.
   Negative results are cached with a timestamp and are not re-queried for 7
   days unless `--retry-missing` (or `art --force`, which ignores the cache
   outright).
3. **Downloads are atomic**: write to `<file>.part`, validate magic bytes,
   then rename into place. A killed run never leaves a truncated image that
   would count as "done".
4. **The one non-incremental step -- writing `shortcuts.vdf` -- is short,
   atomic (`os.replace`), and comes last.** Everything before it is safe to
   interrupt; everything after it is just the Steam relaunch.
5. **Progress streams one line per title as it completes**, plus a final
   summary. On `SIGINT`, finish the in-flight download (or abandon its
   `.part`), flush the cache, print "resume with the same command", and exit
   130.
6. **Rate limiting is built in**: `request_interval_ms` pacing, backoff with
   jitter on 429/5xx (3 tries), and a hard stop after 5 consecutive 429s that
   exits 4 with everything so far kept.
7. **`--limit N` batches a first import** (N *new* shortcuts per run, in
   host-list order).
8. **One Steam restart per run, regardless of library size.** No per-game
   restarts, no per-game `shortcuts.vdf` rewrites.

The end-to-end resumability test (kill mid-run, rerun, assert only the
remainder happens) lives in PR-5 against a 500-title fixture, but any module
that touches disk state should have its own idempotency test well before
then.

## Testing

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest
```

CI (`.github/workflows/ci.yml`) runs `ruff check .` once and `pytest` across
Python 3.11/3.12/3.13 on ubuntu-latest -- that matrix is the SteamOS Python
spread (3.11 through the 3.13.5 shipped on SteamOS 3.8.16), not an arbitrary
choice.

## Fixtures and TODOs

None of the three real captures the design depends on (a sanitised device
`shortcuts.vdf`, `moonlight list --csv` output from each host, a SteamGridDB
API key for `scripts/record_fixtures.py`) exist in this repo yet, and PR-1
does not need them -- `config.py` has no external format to fixture against.
Starting with the PR that needs each one:

- **`shortcuts.vdf`** (PR-2, `vdf.py`/`shortcuts.py`): `tests/fixtures/shortcuts_synthetic.vdf`, a three-entry binary store built by hand from the field order, type bytes and quoting in spec 2.1 by `tests/fixtures/build_synthetic_shortcuts.py` (raw `struct`/byte literals, deliberately not using `vdf.py`, so the fixture is not produced by the code it tests). Cross-checked against `ValvePython/vdf` (dev dependency, test oracle only). **TODO:** replace with a real, sanitised `shortcuts.vdf` from a device -- `moonlight-steam-sync doctor` prints its exact path on the `steam user:` line; shut Steam down before copying it, scrub home paths to `/home/deck/...` and host names to `MY-GAMING-PC`, save it as `tests/fixtures/shortcuts_real.vdf`, and delete the one `pytest.skip` in `tests/conftest.py`'s `real_shortcuts_vdf` fixture. Nothing else changes; the tests already read whichever file exists.
- **`loginusers.vdf`** (PR-2, `steam.py`): `tests/fixtures/loginusers_synthetic.vdf`, text KeyValues shaped from spec 3.4 (a `users` block keyed by steam64, exactly one `MostRecent "1"`). **TODO:** replace with a sanitised real capture from `<steam root>/config/loginusers.vdf` -- invent steam64 ids that stay consistent with the `userdata/<steamid3>` directory names and blank out `AccountName`/`PersonaName`. Drop it in as `loginusers_real.vdf`; the tests prefer it automatically.
- **`moonlight list --csv` output** (PR-3, `moonlight.py`): a synthetic CSV
  built from the header and row shape in spec 2.1
  (`Name, ID, HDR Support, App Collection Game, Hidden, Direct Launch, Boxart URL`).
  TODO: replace with real `moonlight list --host <host> --csv` output
  captured per host, filenames scrubbed of the real host UUID.
- **SteamGridDB / Steam store JSON responses** (PR-4, `art/`): synthetic
  response bodies shaped from the endpoints in spec 2.2
  (`/search/autocomplete`, `/games/id/{id}?platformdata=steam`,
  `/grids|heroes|logos|icons/game/{id}`, `storesearch`, `GetApps`). TODO:
  replace/extend with real captures via `scripts/record_fixtures.py` (PR-6)
  once an `SGDB_API_KEY` is available in the recording environment.

Every synthetic fixture, wherever it lands, must carry a comment or file
naming exactly what real capture should replace it and how to get it -- copy
the wording pattern above rather than a bare "TODO: replace me".
`tests/fixtures/README.md` is the same list from the fixture directory's
point of view; keep the two in step. Tests must ask for a fixture *by role*
(see the helpers in `tests/conftest.py`) so that swapping a synthetic file
for a real capture is a file drop, never a test rewrite.

## Device checklist

Before anything in this tool is trusted against a real Steam install, run
through the device checklist in spec section 8
(`~/specs/moonlight-steam-sync/initial-build.md`). That happens after PR-6
lands and is a human gate before PR-7 (the `server-scripts` migration) --
agents implementing PR-1 through PR-6 do not need device access, only the
synthetic fixtures above.
