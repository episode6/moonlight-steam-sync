# Artwork fixtures

> **TODO(real-data): everything in this directory is SYNTHETIC.** No byte of
> it came off the wire. It is shaped from the endpoint documentation in spec
> section 2.2 (SteamGridDB API v2 envelopes and asset fields, the style
> names, the Steam CDN and `storesearch`/`GetApps` URLs verified live on
> 2026-09-16), but the values are invented, because no SteamGridDB API key
> was available when the artwork PR was written.
>
> **How to replace it with real data:** get a key from
> <https://www.steamgriddb.com/profile/preferences/api> and run
>
> ```sh
> SGDB_API_KEY=xxxxxxxx python3 scripts/record_fixtures.py
> ```
>
> from the repo root. That re-queries the same call chain the tool makes and
> rewrites `manifest.json` and `responses/`. The tests look responses up **by
> URL through the manifest** and never read a fixture's literals, so this is
> a file replacement, not a test rewrite. The key is never written into
> anything the script produces; check the diff before committing anyway.

## Layout

| path | what it is |
|---|---|
| `manifest.json` | `url -> {status, body, content_type, headers}`. A URL that is not listed answers **404**, which is what the Steam CDN does for an asset a game does not have -- that is how the "no logo on the CDN" case works without a special case. |
| `responses/*.json` | one recorded JSON body each. |
| `images/tiny.{png,jpg,webp}` | 1x1 stand-in bodies. Only the magic bytes are load-bearing: the selector sniffs PNG/JPEG and rejects everything else, and `tiny.webp` exists to prove WebP never reaches `grid/`. Real captures do **not** need to replace these, and `record_fixtures.py` deliberately keeps pointing at them. |
| `make_synthetic.py` | regenerates the synthetic set, and documents what each title is for. |

## The six titles and what each one is for

| title | case |
|---|---|
| `Elden Ring` | exact **verified** SteamGridDB match; every slot comes from the Steam CDN, so not one community call is made. |
| `Hades II™` | a title carrying a trademark glyph (normalisation), and a missing `library_600x900_2x.jpg` so the 1x official portrait URL is used. |
| `Fan Made Adventure` | a **non-Steam** game: community art only, score sorting (the fixture lists the low-score asset first), a second-choice query (`920x430` empty -> `460x215`), and an asset whose bytes are WebP behind a `.png` URL, which must be rejected. |
| `Old Console Classic` | a Steam game with **no `logo.png` on the CDN**, so the logo slot falls through to community art while the rest stay official. |
| `Totally Unknown Title` | **zero results** anywhere: no match, a negative cache entry, and the Moonlight box art as the portrait of last resort. |
| `Rate Limited Game` | SteamGridDB and the store both answer **429** forever, so the run hits the five-consecutive-429 hard stop and exits 4 with everything so far kept. |

When a real device run turns up a match the heuristics get wrong, add it here
as a seventh case rather than special-casing it in the code.
