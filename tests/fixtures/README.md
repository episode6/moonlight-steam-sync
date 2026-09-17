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
| `moonlight_list_sample.csv` | `moonlight list --csv` output (spec 2.1/2.3) | real `moonlight list <host> --csv` output, host UUID scrubbed |

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

## `moonlight_list_sample.csv`

**SYNTHETIC -- TODO: replace with real data.** Hand-built to match the exact
byte shape moonlight-qt's `--csv` flag emits, per its source
(`app/cli/listapps.cpp`'s `printAppCSV`/`printAppsCSV` and
`app/backend/boxartmanager.cpp`), not just the field names:

- Header is `Name, ID, HDR Support, App Collection Game, Hidden, Direct
  Launch, Boxart URL` with a literal `", "` (comma-space) separator, so
  every field but `Name` carries a leading space in the raw text --
  `moonlight.py` parses with `skipinitialspace=True` and validates the
  header once, rather than indexing columns positionally.
- Booleans are lowercase `true`/`false` (confirmed against the source, not
  guessed).
- `Boxart URL` is `QUrl::fromLocalFile(...).toDisplayString()`, which
  percent-encodes the path -- the real cache path always contains spaces
  (`.../cache/Moonlight Game Streaming Project/Moonlight/boxart/<uuid>/<id>.png`),
  so the fixture's `file://` rows use `%20` and `moonlight.py`'s
  `_parse_boxart` percent-decodes rather than just stripping the scheme.

One `file://` boxart path and one `qrc:/res/no_app_image.png` (not cached)
row, one `Hidden=true` row and one `App Collection Game=true` row exercise
the filtering in `moonlight.list_apps()`.

TODO: replace this file with real `moonlight list --host <host> --csv`
output captured from each of the user's Moonlight hosts (once available),
with the host UUID in the `Boxart URL` column's cache path scrubbed to
something like `<host-uuid>`. Capturing it is a one-line run: `moonlight
list <host> --csv > moonlight_list_<host>.csv`, then hand-edit out any
real host name, UUID, or absolute home directory before committing.

## Fixtures owned by other PRs

The SteamGridDB / Steam-store JSON bodies (PR-4) land alongside these with the
same rules. They are listed in `AGENTS.md` rather than here until they exist.
