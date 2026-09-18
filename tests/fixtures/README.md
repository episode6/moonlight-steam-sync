# Test fixtures

Everything in here is **synthetic** unless its name says otherwise. Each
file is built from a format documented in the design spec (section 2 and
3.4), not captured from a device, and each one carries a TODO naming the real
capture that should replace it. `AGENTS.md` ("Fixtures and TODOs") holds the
same list.

The tests are written to read *whichever* fixtures are present, so swapping a
synthetic file for a real capture is a file drop plus (for the real
`shortcuts.vdf`) deleting one `pytest.skip` guard -- never a test rewrite.

| file | what it stands in for | replace with |
|---|---|---|
| `shortcuts_synthetic.vdf` | Steam's binary shortcut store, 3 entries (spec 2.1) | `shortcuts_real.vdf` -- a sanitised `userdata/<steamid3>/config/shortcuts.vdf` from a device |
| `build_synthetic_shortcuts.py` | the raw-bytes generator for the above | nothing; it stays, and `test_vdf.py` asserts the committed fixture still matches its output |
| `loginusers_synthetic.vdf` | Steam's text-KeyValues `config/loginusers.vdf` (spec 3.4) | a sanitised real `loginusers.vdf` |
| `moonlight_list_sample.txt` | `moonlight list <host>` output (spec 2.1/2.3) | real `moonlight list <host>` output |
| `moonlight_list_large_synthetic.txt` | the same, from a 500-title host (spec 7; the resumability test in `tests/test_sync_e2e.py`) | `moonlight_list_large_real.txt` -- a real capture from the biggest host |
| `build_synthetic_moonlight_list.py` | the generator for the above | nothing; it stays and documents the capture |

## TODO: `shortcuts_real.vdf`

**Not available yet.** Capture it like this:

1. In Desktop Mode on the device, run `moonlight-steam-sync doctor`; the
   `steam user:` line prints the exact path to `shortcuts.vdf`.
2. Shut Steam down first (`steam -shutdown`, then wait for `pgrep -x steam`
   to come back empty) -- Steam holds this file in memory and rewrites it on
   exit, so a copy taken while it runs is stale (spec 2.1).
3. Sanitise: the file holds file paths and game names only. Rewrite home
   paths to `/home/deck/...` and any host name to `MY-GAMING-PC`.
4. Save it here as `shortcuts_real.vdf` and delete the `pytest.skip` in
   `tests/test_shortcuts.py::test_real_device_fixture_round_trips`.

Why it matters: the synthetic file encodes second-hand knowledge of Steam's
field order, key spelling and quoting. Only a real capture proves Steam's own
writer agrees -- and the round-trip assertion (`dumps(loads(x)) == x`) is
exactly the test that would catch a disagreement.

## TODO: `loginusers_real.vdf`

**Not available yet.** Same Steam directory, `config/loginusers.vdf`.
Sanitise by inventing steam64 ids (keep `steamid3 = steam64 - 76561197960265728`
consistent with the `userdata/` directory names) and replacing
`AccountName`/`PersonaName`. Drop it in as `loginusers_real.vdf`; the
tokenizer test picks it up automatically.

## `moonlight_list_sample.txt`

**SYNTHETIC -- TODO: replace with real data.** Hand-built to match the shape
moonlight-qt's plain `list <host>` command emits, per its source
(`app/cli/listapps.cpp`): one app name per line (`"%s\n"`), nothing else on
stdout, no header. `moonlight.py` keeps every non-blank line verbatim.

The tool deliberately does not use `--csv`: that mode loads box art for
every app before printing, a burst of per-title fetches that has crashed an
Apollo host on a large library. The CSV-only columns (id, the
`Hidden`/`App Collection Game` flags, the cached box-art path) are therefore
not part of the fixture or the parser.

TODO: replace this file with real `moonlight list <host>` output captured
from each of the user's Moonlight hosts (once available). Capturing it is a
one-line run: `moonlight list <host> > moonlight_list_<host>.txt`. It holds
only app names, so there is nothing host-specific to scrub.

## `moonlight_list_large_synthetic.txt`

**SYNTHETIC -- TODO: replace with real data.** 500 lines, `Synthetic Title
001` through `500`, in the same shape as the sample above
(`build_synthetic_moonlight_list.py` writes it through the same
`moonlight_list()` helper the end-to-end tests use). It exists so the
resumability test (spec PR-5 (b), 3.9) runs against a library the size of
the user's real one: a fake HTTP layer dies after N calls, and the rerun
must do exactly the remaining work.

TODO: replace with `moonlight list <host> > moonlight_list_large_real.txt`
from the largest host. `tests/conftest.py`'s `large_library_list` fixture
prefers the real file when it exists; the fake art server for the test keys
on the list's names, so real names work unchanged.

## Fixtures owned by other PRs

The SteamGridDB / Steam-store JSON bodies (PR-4) land alongside these with the
same rules. They are listed in `AGENTS.md` rather than here until they exist.
