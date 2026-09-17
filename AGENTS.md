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
    http.py        urllib transport seam, User-Agent, timeouts, pacing, backoff, the 429 hard stop
    sgdb.py        API client (urllib): search, game(platformdata), grids/heroes/logos/icons
    steamstore.py  storesearch, GetApps icon hash, CDN URL builders
    resolve.py     title -> Match{steam_appid?, sgdb_id?, how}; match cache (flushed per title)
    select.py      per-slot asset choice policy; download; mime sniff -> ext
    apply.py       write grid files for an appid (skip existing), set icon field
    cli.py         the `art` and `status` subcommands
  sync.py          the orchestration: list -> diff -> art -> write -> restart; progress lines
```

`art/http.py` and `art/cli.py` are two small additions to the spec 3.4 map:
one place for the shared urllib/pacing/backoff plumbing so `sgdb.py` and
`steamstore.py` stay thin, and one place for the argparse glue so
`__main__.py` stays a dispatcher.

`__main__.py` and `config.py` landed in PR-1; `vdf.py`, `shortcuts.py` and
`steam.py` (the whole Steam side) in PR-2; `moonlight.py` (binary discovery,
`list_apps()`, `stream()`, wired into the `launch` subcommand) in PR-3; `art/`
(the artwork engine plus the `art` and `status` subcommands) in PR-4;
`sync.py` (the `sync`, `list`, `ignore` and `remove` subcommands, the one
shutdown -> write -> relaunch path, and the resumability e2e tests) in PR-5;
and the release pipeline (`release.yml`, `install.sh`, `--version` from
package metadata, `CHANGELOG.md`) in PR-6. Every subcommand in spec 3.3 is
now real and the tool is installable from a release. What remains is the
device checklist (spec section 8, a human gate) and then PR-7 in
`server-scripts`. Do not add code to a module ahead of its PR without
checking the work plan first -- the modules are split the way they are so
independent PRs can land in parallel.

### Working on the orchestration (`sync.py`)

- **Never write `shortcuts.vdf` with Steam up.** Steam holds the file in
  memory and rewrites it on exit, so the edit is silently lost (spec 2.1).
  `sync.commit_shortcuts()` is the *only* path from an in-memory
  `ShortcutsFile` to disk: it checks `steam.is_running()`, refuses with
  `SteamRunningError` (exit 2) when `restart_steam` is false, and otherwise
  does `steam -shutdown` -> wait -> write -> `steam -silent` with `SIGINT`
  deferred across the window. `art` commits through it too
  (`sync.steam_aware_provider`), and the fallback writer in `art/apply.py`
  refuses rather than writes when Steam is running. Do not add a second
  writer.
- **Owned = exe match, not name match.** `ShortcutsFile.owned(config.exe)`
  compares the unquoted `Exe` by realpath (spec 3.3). That is what adopts the
  SteamTinkerLaunch-era entries and what keeps foreign shortcuts (an
  emulator, a browser) out of `remove --all`. The Moonlight name behind an
  owned entry comes from `Shortcut.moonlight_name()` (launch options first,
  `AppName` minus the suffix as the fallback). Nothing in `sync.py` should
  ever look a shortcut up by `AppName`.
- **Progress lives on disk, never in memory.** The plan is computed from
  `shortcuts.vdf`, the grid directory and `matches.json` every run; there is
  no state file and no in-memory "done" set. New entries are appended to the
  in-memory `ShortcutsFile` *before* the art phase (so icon patches can land
  on them) but the file is only written afterwards, and not at all when the
  art phase stopped early (`RunSummary.stopped_early`): a crash, a 429
  storm or a Ctrl-C mid-art leaves `shortcuts.vdf` byte-identical and the
  same command finishes the job. `tests/test_sync_e2e.py` proves this for a
  500-title library at every phase boundary; keep it passing.
- **One restart per run, and none when nothing changed.** `commit_shortcuts`
  does nothing at all when the serialised file is unchanged and no art was
  newly written this run (`RunSummary.written`, which excludes `kept`
  slots). A second `sync` over a finished library makes zero HTTP calls, no
  write and no restart.
- **The `icon` field follows the file on disk.** `art.apply.patch_icon()`
  stores the bare absolute path (the way Steam's own UI writes it) and only
  repoints a field that is empty or dangling; a field that names an existing
  file -- the same one, or one the user chose -- is left alone.
  `sync.patch_icons_from_disk()` applies the same rule with `--no-art`.
- **`--dry-run` resolves titles.** The only way to print a CDN URL per slot
  is to know the Steam appid, so a dry run does make the lookup calls and
  writes `matches.json` (the real run then reuses every resolution). It
  downloads nothing and touches nothing under the Steam directory.

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

### The artwork seam onto the shortcut layer

`art/` never parses `shortcuts.vdf` and never discovers the Steam root. It
asks for exactly four things per title, through
`art.apply.ArtTarget(name, appid, grid_dir, boxart_path)`, and hands exactly
one thing back, through `art.apply.TargetProvider`:

```python
class TargetProvider(Protocol):
    def targets(self) -> Sequence[ArtTarget]: ...
    def set_icon(self, appid: int, icon_path: Path) -> None: ...
    def commit(self) -> None: ...
```

`appid` is the shortcut's own 32-bit id (`crc32(Exe + AppName) |
0x80000000`), which is knowable *before* the shortcut exists -- that is what
lets the art phase run first (spec 3.6). `set_icon` must only *record* the
icon patch: spec 3.6 wants one atomic `shortcuts.vdf` write per run, which is
what `commit()` is for. The real implementation is
`art.apply.SteamShortcutProvider`, built on PR-2's `steam.pick_user()` /
`SteamUser.grid_dir` and `ShortcutsFile.owned(exe)`; tests inject a fake
through the `provider_factory` seam, and `art` / `status` exit 1 with the
Steam-side error message when no install or user can be found.

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
remainder happens) is `tests/test_sync_e2e.py` against a 500-title fixture:
a fake HTTP layer dies after call N (parametrised across every phase
boundary of a title: mid-resolve, mid-download, mid-body with a `.part`
open, on the last call), or raises `KeyboardInterrupt`, or answers 429 from
call N on; the test then asserts `shortcuts.vdf` was never created, exactly
N-worth of cache entries and grid files exist, and the rerun's call list
equals the list *derived from what is on disk* -- no lookups for a cached
title, no download for a filled slot. Any module that touches disk state
should have its own idempotency test as well. `art/` carries its share: a
second pass over the same titles makes zero HTTP calls, an existing slot
file is never re-downloaded, a connection that dies mid-download leaves only
a `.part` and costs just that slot, and `matches.json` is on disk after
every title (`tests/test_art_apply.py`).

### Artwork gotchas

- **Never write `.webp` into `grid/`.** Steam cannot read it. The asset
  queries pin `types=static` and `mimes=image/png,image/jpeg`, and the
  downloader sniffs magic bytes, so a mislabelled `.png` that is really WebP
  is dropped and the next source is tried.
- **Grid filenames use the *shortcut's* appid, never the Steam store appid**
  the art came from (`<appid>p`, `<appid>`, `<appid>_hero`, `<appid>_logo`,
  `<appid>_icon`).
- **A slot that already has a file is never touched without `--force`.** The
  Steam UI writes the same filenames, so that rule is what keeps hand-picked
  art safe -- and it doubles as the resume mechanism. When `--force` *does*
  refill a slot, the old file goes even if the new image has a different
  extension: two files for one slot would leave Steam's choice undefined and
  let the next run report the stale one as "kept".
- **A transient failure is never cached as a miss.** A 5xx, a timeout, a
  dropped connection or a 429 that exhausted its retries leaves no negative
  entry; only "the source genuinely has no image for this slot" starts the
  7-day window. That holds for a failed *title* lookup too:
  `MatchCache.record_missing_slot` refuses to invent an entry for a title
  that never resolved, so the slot loop cannot cache a miss through the back
  door.
- **Every network failure is an `HttpError`, including one halfway through a
  response body.** A connection that dies while a download is streaming used
  to escape as a bare `OSError` past every handler; `Fetcher` now translates
  it into `NetworkError` and counts it toward the same five-in-a-row hard
  stop, so a flaky link costs a slot (or, five times over, exits 4) instead
  of a traceback.

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
- **`moonlight list --csv` output** (PR-3, `moonlight.py`): a synthetic CSV,
  `tests/fixtures/moonlight_list_sample.csv`, built to match the exact byte
  shape moonlight-qt's source emits (header separator `", "`, lowercase
  `true`/`false`, percent-encoded `Boxart URL`) -- see
  `tests/fixtures/README.md` for the citations and exactly what to swap in
  once real captures are available. TODO: replace with real `moonlight list
  <host> --csv` output captured per host, with the host UUID in the `Boxart
  URL` cache path scrubbed. Still unverified against a real binary: whether
  `list --csv` blocks long enough for box art to finish caching on a first
  run (spec 2.3's own "[verify]" note) -- `moonlight.list_apps()`'s
  `LIST_TIMEOUT_S = 60` is a generous guess, not a measurement.
- **SteamGridDB / Steam store JSON responses** (PR-4, `art/`): synthetic
  response bodies shaped from the endpoints in spec 2.2
  (`/search/autocomplete`, `/games/id/{id}?platformdata=steam`,
  `/grids|heroes|logos|icons/game/{id}`, `storesearch`, `GetApps`), living in
  `tests/fixtures/art/`. `manifest.json` maps a URL to a recorded response;
  any URL it does not list answers 404, which is what the Steam CDN does for
  an asset a game does not have. Six titles cover exact-verified match, a
  title with a trademark glyph, a non-Steam game with community art only, a
  Steam game with no logo on the CDN, a title with zero results, and a 429
  storm; `tests/fixtures/art/README.md` says which is which.
  **TODO: replace with real captures** by running
  `SGDB_API_KEY=xxxxxxxx python3 scripts/record_fixtures.py` from the repo
  root once a key is available. The tests resolve responses by URL through
  the manifest and never read fixture literals, so that is a file
  replacement, not a test rewrite. `tests/fixtures/art/make_synthetic.py`
  regenerates the synthetic set and documents what each title is for.
  The 1x1 images in `tests/fixtures/art/images/` are stand-ins on purpose and
  do **not** need replacing: only their magic bytes are read. PR-5 added a
  seventh title, `Hollow Knight`, so the adoptable entry in the PR-2
  `shortcuts.vdf` fixture resolves (all official, like Elden Ring).
- **A 500-title `moonlight list --csv` capture** (PR-5, `sync.py`):
  `tests/fixtures/moonlight_list_large_synthetic.csv`, 500 rows named
  `Synthetic Title 001`...`500`, generated by
  `tests/fixtures/build_synthetic_moonlight_list.py` in the exact byte shape
  moonlight-qt emits (same rules as the sample CSV above). It feeds the
  resumability test through the fake `moonlight` script `tests/fakes.py`
  puts on `PATH`; the artwork answers for it come from
  `tests/art_fixtures.BulkTransport`, a pattern-based fake server (one exact
  SteamGridDB match with a Steam release per title, every CDN asset present,
  so a title costs exactly eight calls) rather than 500 titles of
  recordings. **TODO:** replace the CSV with a real capture from the user's
  largest host -- `moonlight list <host> --csv > moonlight_list_large_real.csv`,
  scrub the host UUID in the `Boxart URL` paths and any home directory, drop
  it in as `tests/fixtures/moonlight_list_large_real.csv`. The tests ask for
  the large library by role (`large_library_csv` in `tests/conftest.py`) and
  `BulkTransport` keys on the CSV's names, so nothing else changes. The ids
  and images `BulkTransport` serves stay synthetic on purpose.

Every synthetic fixture, wherever it lands, must carry a comment or file
naming exactly what real capture should replace it and how to get it -- copy
the wording pattern above rather than a bare "TODO: replace me".
`tests/fixtures/README.md` is the same list from the fixture directory's
point of view; keep the two in step. Tests must ask for a fixture *by role*
(see the helpers in `tests/conftest.py`) so that swapping a synthetic file
for a real capture is a file drop, never a test rewrite.

The fakes for the outside world live in `tests/fakes.py` (`FakeRunner` for
Steam's process, `make_steam_root()` + `$STEAM_ROOT` for the Steam tree,
`install_fake_moonlight()` for the CLI) and `tests/art_fixtures.py`
(`FakeTransport` over the recorded manifest, `BulkTransport` by pattern).
New end-to-end tests should build on those rather than monkeypatching
`subprocess` or `urllib` directly.

## Cutting a release

The release artifact (spec 3.1) is a single executable zipapp,
`moonlight-steam-sync.pyz`, built by `.github/workflows/release.yml` and
attached to a GitHub release whenever a `v*` tag is pushed. That workflow
also has a `pull_request` and `workflow_dispatch` trigger on its build+smoke
job -- the part that builds the zipapp and smoke-runs `--version` and
`doctor` against it on Python 3.11/3.12/3.13 -- specifically so it is
exercised by ordinary CI and does not sit untested until the first tag. Only
the final "create the release and attach the asset" job is tag-only.

Bumping the version and cutting a release is a small number of steps, done
from a clean checkout of `main` after everything intended for the release
has merged:

```sh
git checkout main && git pull

# 1. Bump the single source of truth for the version.
#    pyproject.toml's `version` is `dynamic` and reads this attribute
#    (`[tool.setuptools.dynamic]`), so this is the only file to edit.
$EDITOR src/moonlight_steam_sync/__init__.py   # __version__ = "X.Y.Z"

# 2. Move the CHANGELOG's prepared entry out of "not yet released" and
#    open a new empty "Unreleased" section above it.
$EDITOR CHANGELOG.md

# 3. Commit the bump.
git add src/moonlight_steam_sync/__init__.py CHANGELOG.md
git commit -m "Release vX.Y.Z"

# 4. Tag and push. The tag push is what triggers release.yml's
#    github-release job.
git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin main
git push origin vX.Y.Z
```

After the push, watch the `Release` workflow run to completion
(`gh run watch` or the Actions tab) and confirm the release page has
`moonlight-steam-sync.pyz` and `moonlight-steam-sync.pyz.sha256` attached,
then sanity-check the install path end to end:

```sh
curl -fsSL https://raw.githubusercontent.com/episode6/moonlight-steam-sync/main/install.sh | sh
moonlight-steam-sync --version
```

**`v0.1.0` is deliberately not cut yet.** It sits immediately on the other
side of the human device checklist below -- a release is an irreversible
public publish, and the repo owner holds it back until the checklist has
passed on real hardware. PR-6 lands everything this section describes
without running these steps.

## Device checklist

Before anything in this tool is trusted against a real Steam install, run
through the device checklist in spec section 8
(`~/specs/moonlight-steam-sync/initial-build.md`). That happens after PR-6
lands and is a human gate before PR-7 (the `server-scripts` migration) --
agents implementing PR-1 through PR-6 do not need device access, only the
synthetic fixtures above.
