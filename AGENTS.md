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

A second spec, `~/specs/moonlight-steam-sync/decky-plugin.md`, is in
progress on top of that build: a Decky Loader plugin that runs `sync` from
Game Mode and stops creating visible shortcuts for games the Deck's own
Steam account already owns. Its CLI half lands as a stack of PRs behind new,
additive flags (`--json`, `--owned-apps`, `host`, `search`, `match`,
`--commit`, ...); until it lands in full, every command's behaviour with
none of those flags passed stays byte-identical to the release before it
started (spec 3.11).

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
- **`moonlight list <host>` is always the plain form, never `--csv`.**
  moonlight-qt's CSV mode calls `loadBoxArt()` for every app before it
  prints, which fires one box-art fetch per title at the host in a burst;
  on a large library that burst has crashed an Apollo host outright. The
  plain form only asks for the app list. The CSV-only columns (per-app id,
  the `Hidden`/`App Collection Game` flags, the cached box-art path) are
  gone with it: an app hidden in the Moonlight client is listed like any
  other and `ignore` is the way to keep it out, and there is no host
  box-art source for the portrait slot any more.
- **The tool never writes `config.toml`.** `ignore --all` *prints* TOML lines
  for the user to paste in; it does not edit the file. This is a deliberate
  decision (spec 6.7), not an oversight -- don't silently change it. The
  Decky plugin's own state goes elsewhere for the same reason: its ignores
  arrive as `--ignore-file` (a JSON list, unioned with `ignore`) and its
  match fixes as pins in `matches.json` (`match`), which *prints* the
  equivalent `[overrides]` line instead of writing it.

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
  moonlight.py     find binary (native `moonlight`, else flatpak), list(host) -> [App(name)],
                   stream(host, name, extra), run_client(extra)
  hosts.py         active-host state file (read/write/clear_active_host); per-host `moonlight list`
                   cache (slug, HostCache, list_cached_hosts); `host show|set|clear` (cmd_host)
  reporting.py     Reporter: the `--json` event stream shared by sync.py and art/cli.py; the title
                   kinds (KIND_*, STREAM_HOWS, is_stream_match) both need
  art/
    http.py        urllib transport seam, User-Agent, timeouts, pacing, backoff, the 429 hard stop
    sgdb.py        API client (urllib): search, game(platformdata), grids/heroes/logos/icons
    steamstore.py  storesearch, GetApps icon hash, CDN URL builders
    resolve.py     title -> Match{steam_appid?, sgdb_id?, how}; match cache (flushed per title),
                   pins (MatchCache.pin/unpin) and the stale_art flag
    select.py      per-slot asset choice policy; download; mime sniff -> ext
    apply.py       write grid files for an appid (skip existing), set icon field
    cli.py         the `art`, `status`, `search` and `match` subcommands
  sync.py          the orchestration: list -> (resolve) -> plan (kinds, replacements, duplicates,
                   parking, the client entry) -> art -> write -> restart; progress lines
```

`art/http.py` and `art/cli.py` are two small additions to the spec 3.4 map:
one place for the shared urllib/pacing/backoff plumbing so `sgdb.py` and
`steamstore.py` stay thin, and one place for the argparse glue so
`__main__.py` stays a dispatcher. `hosts.py` and `reporting.py` (added for
the Decky-plugin CLI work, see
`~/specs/moonlight-steam-sync/decky-plugin.md`) follow the same rule:
`hosts.py` imports nothing from `config` or `sync` so both can import it
back, and `reporting.py` holds `Reporter` (the `--json` event stream, spec
3.4.6) because `sync.py` imports `art/cli.py`, so `art/cli.py` cannot import
`sync.py`'s copy back -- one shared module instead of two Reporters
drifting apart.

`__main__.py` and `config.py` landed in PR-1; `vdf.py`, `shortcuts.py` and
`steam.py` (the whole Steam side) in PR-2; `moonlight.py` (binary discovery,
`list_apps()`, `stream()`, wired into the `launch` subcommand) in PR-3; `art/`
(the artwork engine plus the `art` and `status` subcommands) in PR-4;
`sync.py` (the `sync`, `list`, `ignore` and `remove` subcommands, the one
shutdown -> write -> relaunch path, and the resumability e2e tests) in PR-5;
and the release pipeline (`release.yml`, `install.sh`, `--version` from
package metadata, `CHANGELOG.md`) in PR-6. Every subcommand in spec 3.3 is
now real, `v0.1.0` is released, and PR-7 (the `server-scripts` switch-over)
has landed too. What remains is the device checklist (spec section 8, a
human gate; the installer and Python-version findings from its first run
are already folded in). Do not add code to a module ahead of its PR without
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
  (`sync.steam_aware_provider`), so does `match`'s grid-and-icon reset
  (`art/cli.cmd_match`), and the fallback writer in `art/apply.py`
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

### Owned apps, hidden entries and parking (decky spec 3.2, 3.3, 3.4.2, 3.11, 3.12)

- **Without the new flags, v0.2.0's bytes.** With none of `--json`,
  `--owned-apps`, `--ignore-file`, `--client-shortcut`, `--commit`,
  `--park-unpublished`, `--cached`, `status --host`, no new subcommand, no
  `active-host` state file and no `pinned`/`stale_art` entry in
  `matches.json`, every command produces the same stdout, stderr, exit
  code and the same bytes under the Steam root and in `matches.json` as
  v0.2.0 (`1488a96`). Exactly three things differ and nothing else: the
  three extra `doctor` lines; the per-host list cache `sync`, `list` and
  `ignore --all` write beside `matches.json`; and `list`'s `same-game-as`
  line, which needs a second host's cache file to exist at all. The
  frozen `tests/test_sync_e2e.py` is the proof. Concretely: no title is
  resolved ahead of the plan unless `--owned-apps` is passed, but
  `build_plan` always gets `matches` -- a plain `sync` and `list
  --owned-apps` read them from the cache (a read, never a write or a
  lookup) -- so a `stale_art` entry is a `rematched` replacement in every
  `sync`, whatever the entry's `how` (decky spec 3.4.4); every new human
  line is printed only when the plan holds the change it describes;
  `IsHidden` is only ever flipped by `--owned-apps` (a visible `stream`
  entry is re-hidden, `Plan.to_hide`) or `--park-unpublished` (parking
  and unparking); and `list` labels a hidden entry `parked`, and a
  duplicate `ignored`, only under `--owned-apps`.
- **A hidden entry is still an owned entry.** `remove --all`, adoption and
  the plan treat it exactly like a visible one; nothing looks a shortcut up
  by `AppName`. A `stream` entry's `AppName` is the owned game's display
  name (no suffix) so Steam Input offers it the retail game's layouts, and
  its launch options carry the Moonlight name, which is the only way back
  to it -- `--owned-apps` refuses a `launch_options` template without
  `{name}` for that reason.
- **A rename is a replacement, never an in-place edit of `AppName`** (the
  appid would silently stop matching the grid files). `apply_replacement`
  swaps the entry in place (`ShortcutsFile.replace`, same key) and moves
  the art *before* the art phase: `kind-changed` / `name-changed` rename
  the five grid files to the new appid with `os.replace` (zero downloads),
  `rematched` (the title's `stale_art` flag) deletes them instead. Every
  move is idempotent -- a source that is gone with a target that exists is
  done, a target is never overwritten -- so a crash between two
  replacements is finished by the rerun (`tests/test_sync_e2e_replacements.py`).
  The flag is cleared only once the art phase has actually run for the
  title; never after an early stop and never under `--no-art`.
- **`IsHidden` is the one field edited in place**: parking, unparking and a
  `shortcut -> stream` flip that keeps the `AppName` all toggle it without
  a replacement, and `--limit` never counts a flip (it counts additions and
  replacements together, host-list order, never splitting a replacement).
  The re-hide is not parking, though: parking is for a name the active
  host does *not* publish (decky spec 3.12), and the plugin reads the
  `plan` event's `to_park` as "titles the other host has parked", so a
  visible `stream` entry goes in `Plan.to_hide` (`N to hide` / `hidden N`
  in the human lines, a `hide` line under `--dry-run`) and never in
  `to_park`. The `plan` event's keys are the spec's (3.4.6); there is no
  `to_hide` key.
- **Entries never record a host.** Which host a tile streams from is
  decided at launch time by the active-host rule; the tool's only
  host-scoped state is the read-only list cache under `<cache dir>/hosts/`.
  Parking keeps the entry, its appid, its art, its pin and its layout, so
  switching back is one write that flips bits and no downloads.
- **Duplicates: first in host order wins.** Two titles resolving to one
  owned game collide on `AppName`; the later one is kind `duplicate`, gets
  no entry (an entry it had is removed with its art, unless that entry *is*
  the winner's hidden entry, which stays), and a `--none` pin gives it a
  tile again.
- **The client entry is identified by launch options + the tool's exe,
  never by name.** `art_targets()` and `art` skip it, `status` reports it
  (`client: true`) whoever `config.exe` is, `remove --client` deletes it,
  `remove --all` only when `exe` is the tool.

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
`art.apply.ArtTarget(name, appid, grid_dir)`, and hands exactly
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
   outright -- except for pins, below).
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
- **Pins are the one cache state `--force` keeps.** `match` writes a
  `how: "pinned"` entry into `matches.json`, and `Resolver.resolve()`
  returns it *before* it looks at `force` or `retry_missing`, so neither
  `art --force` nor `--retry-missing` ever re-searches or overwrites a
  pinned title -- not even a `--none` pin (which makes no lookups at all).
  Only `match --unpin` or another `match` changes one. An `[overrides]`
  entry in `config.toml` still wins over a pin (config beats cache), and
  `CACHE_VERSION` stays 1: `stale_art` is written only while true, so a
  cache with no pinned or stale entry is byte-identical to one written
  before pins existed. `match`'s immediate path (delete the title's grid
  files, clear its `icon`) goes through `sync.commit_shortcuts()` like
  `remove`; `--defer-art` only marks the entry `stale_art`, which the
  next `sync` -- plain or `--owned-apps`; the plan reads the cache either
  way -- acts on as a `rematched` replacement (decky spec 3.4.2, 3.4.4)
  and `art --force` clears (it refilled every slot).
- **`stale_art` belongs to the grid files, not to the match.** It says
  "the art on disk came from an earlier match", so `MatchCache.put()`
  carries it forward onto whatever resolution replaces a flagged entry;
  only `pin()` (a fresh choice by the user) and `clear_stale_art()` (the
  art was re-fetched) drop it. That is what makes `match --unpin
  --defer-art` work: with no entry left to flag it leaves a `how:
  "unpinned"` placeholder (ids null, flag set) that `Resolver.resolve()`
  treats as a cache miss and `match_json()` reports as `null`, and the
  next run's search inherits the flag. Never treat `unpinned` as a
  resolution anywhere (`Match.unpinned`), and never write a `stale_art`
  entry over a `put()` without thinking about which of the two rules
  applies.
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

CI (`.github/workflows/ci.yml`) runs `ruff check .` once and `pytest` on
Python 3.11 and 3.13.5 on ubuntu-latest. That matrix is not arbitrary: 3.11
is the floor `pyproject.toml` declares (the `tomllib` line), and 3.13.5 is
the exact Python a stock SteamOS install ships (verified on a fresh install
on 2026-09-17), i.e. the one the tool actually has to run on. When SteamOS
moves to a new Python, change the pinned version in both `ci.yml` and
`release.yml` rather than adding legs.

## Fixtures and TODOs

None of the three real captures the design depends on (a sanitised device
`shortcuts.vdf`, `moonlight list` output from each host, a SteamGridDB
API key for `scripts/record_fixtures.py`) exist in this repo yet, and PR-1
does not need them -- `config.py` has no external format to fixture against.
Starting with the PR that needs each one:

- **`shortcuts.vdf`** (PR-2, `vdf.py`/`shortcuts.py`): `tests/fixtures/shortcuts_synthetic.vdf`, a three-entry binary store built by hand from the field order, type bytes and quoting in spec 2.1 by `tests/fixtures/build_synthetic_shortcuts.py` (raw `struct`/byte literals, deliberately not using `vdf.py`, so the fixture is not produced by the code it tests). Cross-checked against `ValvePython/vdf` (dev dependency, test oracle only). **TODO:** replace with a real, sanitised `shortcuts.vdf` from a device -- `moonlight-steam-sync doctor` prints its exact path on the `steam user:` line; shut Steam down before copying it, scrub home paths to `/home/deck/...` and host names to `MY-GAMING-PC`, save it as `tests/fixtures/shortcuts_real.vdf`, and delete the one `pytest.skip` in `tests/conftest.py`'s `real_shortcuts_vdf` fixture. Nothing else changes; the tests already read whichever file exists.
- **`loginusers.vdf`** (PR-2, `steam.py`): `tests/fixtures/loginusers_synthetic.vdf`, text KeyValues shaped from spec 3.4 (a `users` block keyed by steam64, exactly one `MostRecent "1"`). **TODO:** replace with a sanitised real capture from `<steam root>/config/loginusers.vdf` -- invent steam64 ids that stay consistent with the `userdata/<steamid3>` directory names and blank out `AccountName`/`PersonaName`. Drop it in as `loginusers_real.vdf`; the tests prefer it automatically.
- **`moonlight list` output** (PR-3, `moonlight.py`): a synthetic list,
  `tests/fixtures/moonlight_list_sample.txt`, in the shape moonlight-qt's
  plain `list <host>` emits (one app name per line, nothing else) -- see
  `tests/fixtures/README.md` for the citation and exactly what to swap in
  once real captures are available. TODO: replace with real `moonlight list
  <host>` output captured per host (nothing in it needs scrubbing; it is
  only names). `moonlight.list_apps()`'s `LIST_TIMEOUT_S = 60` is a
  generous guess, not a measurement.
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
- **A 500-title `moonlight list` capture** (PR-5, `sync.py`):
  `tests/fixtures/moonlight_list_large_synthetic.txt`, 500 lines named
  `Synthetic Title 001`...`500`, generated by
  `tests/fixtures/build_synthetic_moonlight_list.py` in the shape moonlight-qt
  emits (same rule as the sample list above). It feeds the resumability
  test through the fake `moonlight` script `tests/fakes.py` puts on `PATH`;
  the artwork answers for it come from `tests/art_fixtures.BulkTransport`, a
  pattern-based fake server (one exact SteamGridDB match with a Steam
  release per title, every CDN asset present, so a title costs exactly eight
  calls) rather than 500 titles of recordings. **TODO:** replace the list
  with a real capture from the user's largest host -- `moonlight list <host>
  > moonlight_list_large_real.txt`, drop it in as
  `tests/fixtures/moonlight_list_large_real.txt`. The tests ask for the
  large library by role (`large_library_list` in `tests/conftest.py`) and
  `BulkTransport` keys on the list's names, so nothing else changes. The ids
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
`doctor` against it on Python 3.11 and 3.13.5 -- specifically so it is
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

# 4. Push the bump, then create the release with the gh CLI. `gh release
#    create` makes the tag and the release page in one step; the tag
#    reaching GitHub is what triggers release.yml's github-release job,
#    which builds the zipapp and uploads it into the release that now
#    already exists (that step is idempotent -- see the comment on it).
git push origin main
gh release create vX.Y.Z --target main --title vX.Y.Z --generate-notes
```

Pushing a plain `git tag` works too and takes the same path; the workflow
creates the release itself in that case. Either way the assets are attached
by CI, never by hand.

After the push, watch the `Release` workflow run to completion
(`gh run watch` or the Actions tab) and confirm the release page has
`moonlight-steam-sync.pyz` and `moonlight-steam-sync.pyz.sha256` attached,
then sanity-check the install path end to end:

```sh
curl -fsSL https://raw.githubusercontent.com/episode6/moonlight-steam-sync/main/install.sh | sh
moonlight-steam-sync --version
```

**`v0.1.0` was cut on 2026-09-17**, ahead of the device checklist rather
than after it: the whole point of the release is that installing on a
SteamOS box becomes one `curl` instead of a git checkout and a
`PYTHONPATH`, which is what makes the checklist practical to run.

## Device checklist

Before anything in this tool is trusted against a real Steam install, run
through the device checklist in spec section 8
(`~/specs/moonlight-steam-sync/initial-build.md`). It was meant as a human
gate before PR-7 (the `server-scripts` migration); in practice the release
and PR-7 went first so the checklist could run against an installed tool --
agents implementing PR-1 through PR-6 did not need device access, only the
synthetic fixtures above.
