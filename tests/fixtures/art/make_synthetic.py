#!/usr/bin/env python3
"""Regenerate the SYNTHETIC artwork fixtures in this directory.

TODO(real-data): every file this script writes is invented. It is shaped from
the endpoint documentation in spec section 2.2 (SteamGridDB API v2 response
envelopes, asset object fields, style names) and the Steam CDN/store URLs
verified live on 2026-09-16, but no byte of it came off the wire. Replace it
with real captures by running::

    SGDB_API_KEY=... python3 scripts/record_fixtures.py

which hits the real SteamGridDB, Steam store and Steam CDN endpoints for the
same six titles and rewrites ``manifest.json`` plus ``responses/``. The tests
read the manifest, not these literals, so a real capture is a file
replacement and not a test rewrite. Keep this script around afterwards: it
documents what each title is *for*, and it is how the fixture set gets
extended when a new match case shows up on a device.

The six titles exercise the cases spec PR-4 asks for:

1. ``Elden Ring``          -- exact verified SteamGridDB match, every slot official.
2. ``Hades II(tm)``        -- a title with a trademark glyph; also the 2x portrait
                              missing so the 1x official URL is used.
3. ``Fan Made Adventure``  -- a non-Steam game: community art only, score sorting,
                              a second-choice query, and an asset whose bytes are
                              WebP despite a ``.png`` URL (must be rejected).
4. ``Old Console Classic`` -- a Steam game with no ``logo.png`` on the CDN, so the
                              logo slot falls through to community art.
5. ``Totally Unknown Title``-- zero results anywhere: no match, negative cache, and
                              the Moonlight box art as the portrait of last resort.
6. ``Rate Limited Game``   -- SteamGridDB and the store both answer 429 forever, so
                              the run hits the five-consecutive-429 hard stop.

Run from the repo root::

    python3 tests/fixtures/art/make_synthetic.py
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from moonlight_steam_sync.art import steamstore  # noqa: E402
from moonlight_steam_sync.art.sgdb import PNG_ONLY_MIMES, STATIC_IMAGE_MIMES  # noqa: E402

SGDB = "https://www.steamgriddb.com/api/v2"

TODO = (
    "SYNTHETIC FIXTURE -- invented, not captured. Replace with a real capture: "
    "SGDB_API_KEY=... python3 scripts/record_fixtures.py"
)


def sgdb_url(path: str, params: dict[str, str] | None = None) -> str:
    from urllib.parse import urlencode

    url = f"{SGDB}{path}"
    if params:
        url = f"{url}?{urlencode(params, safe='/,')}"
    return url


def search_url(term: str) -> str:
    from urllib.parse import quote

    return sgdb_url(f"/search/autocomplete/{quote(term, safe='')}")


def game_url(sgdb_id: int) -> str:
    return sgdb_url(f"/games/id/{sgdb_id}", {"platformdata": "steam"})


def asset_url(kind: str, sgdb_id: int, **params: str) -> str:
    query = {"types": "static"}
    query.update(params)
    query.setdefault("mimes", STATIC_IMAGE_MIMES)
    return sgdb_url(f"/{kind}/game/{sgdb_id}", query)


def envelope(data: object) -> dict[str, object]:
    return {"_TODO": TODO, "success": True, "data": data}


def game(sgdb_id: int, name: str, steam_appid: int | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": sgdb_id,
        "name": name,
        "types": ["steam"] if steam_appid else ["other"],
        "verified": True,
        "release_date": 1600000000,
    }
    if steam_appid is not None:
        payload["external_platform_data"] = {
            "steam": [{"id": str(steam_appid), "metadata": {"clienticon": "deadbeef"}}]
        }
    return payload


def asset(asset_id: int, url: str, *, style: str, score: int, mime: str) -> dict[str, object]:
    return {
        "id": asset_id,
        "score": score,
        "style": style,
        "width": 600,
        "height": 900,
        "mime": mime,
        "nsfw": False,
        "humor": False,
        "epilepsy": False,
        "language": "en",
        "url": url,
        "thumb": url.replace("/full/", "/thumb/"),
        "upvotes": score,
        "downvotes": 0,
        "author": {"name": "synthetic-uploader"},
    }


# --------------------------------------------------------------------------
# tiny image bodies. Only the magic bytes matter to the selector, so these are
# the smallest things that carry the right ones.
# --------------------------------------------------------------------------


def tiny_png() -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    body = zlib.compress(b"\x00\xff\xff\xff")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", body)
        + chunk(b"IEND", b"")
    )


def tiny_jpeg() -> bytes:
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00\x43\x00" + bytes(64) + b"\xff\xd9"
    )


def tiny_webp() -> bytes:
    payload = b"VP8 " + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
    return b"RIFF" + struct.pack("<I", len(payload) + 4) + b"WEBP" + payload


def main() -> int:
    responses_dir = HERE / "responses"
    images_dir = HERE / "images"
    responses_dir.mkdir(exist_ok=True)
    images_dir.mkdir(exist_ok=True)

    (images_dir / "tiny.png").write_bytes(tiny_png())
    (images_dir / "tiny.jpg").write_bytes(tiny_jpeg())
    (images_dir / "tiny.webp").write_bytes(tiny_webp())
    (images_dir / "README.txt").write_text(
        "SYNTHETIC 1x1 images. Only the magic bytes are load-bearing: the\n"
        "selector sniffs PNG/JPEG and rejects anything else (tiny.webp is here\n"
        "to prove WebP never reaches grid/). Real captures do not need to\n"
        "replace these.\n",
        encoding="utf-8",
    )

    responses: dict[str, dict[str, object]] = {}

    def json_response(url: str, name: str, payload: object, status: int = 200) -> None:
        path = responses_dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        responses[url] = {
            "status": status,
            "body": f"responses/{name}.json",
            "content_type": "application/json",
        }

    def image_response(url: str, image: str) -> None:
        responses[url] = {
            "status": 200,
            "body": f"images/{image}",
            "content_type": "image/png" if image.endswith(".png") else "image/jpeg",
        }

    def rate_limited(url: str) -> None:
        responses[url] = {"status": 429, "body": None, "headers": {"Retry-After": "1"}}

    # -- 1. Elden Ring: exact verified match, all five slots official --------
    json_response(
        search_url("Elden Ring"),
        "sgdb-search-elden-ring",
        envelope(
            [
                game(9999, "Elden Ring Nightreign", 2622380) | {"verified": False},
                game(5297, "Elden Ring", 1245620),
            ]
        ),
    )
    json_response(game_url(5297), "sgdb-game-5297", envelope(game(5297, "Elden Ring", 1245620)))
    image_response(steamstore.portrait_urls(1245620)[0], "tiny.jpg")
    image_response(steamstore.landscape_urls(1245620)[0], "tiny.jpg")
    image_response(steamstore.hero_urls(1245620)[0], "tiny.jpg")
    image_response(steamstore.logo_urls(1245620)[0], "tiny.png")
    json_response(
        steamstore.get_apps_url(1245620),
        "steam-getapps-1245620",
        {
            "_TODO": TODO,
            "response": {
                "apps": [
                    {"appid": 1245620, "name": "ELDEN RING", "icon": "1c1c1c1c1c1c1c1c1c1c"}
                ]
            },
        },
    )
    image_response(steamstore.icon_url(1245620, "1c1c1c1c1c1c1c1c1c1c"), "tiny.jpg")

    # -- 2. Hades II(tm): trademark glyph; 2x portrait missing --------------
    json_response(
        search_url("Hades II™"),
        "sgdb-search-hades-ii",
        envelope([game(5468, "Hades II", 1145350)]),
    )
    json_response(game_url(5468), "sgdb-game-5468", envelope(game(5468, "Hades II", 1145350)))
    # portrait_urls(...)[0] (the 2x) is deliberately absent -> 404 -> 1x wins.
    image_response(steamstore.portrait_urls(1145350)[1], "tiny.jpg")
    image_response(steamstore.landscape_urls(1145350)[0], "tiny.jpg")
    image_response(steamstore.hero_urls(1145350)[0], "tiny.jpg")
    image_response(steamstore.logo_urls(1145350)[0], "tiny.png")
    json_response(
        steamstore.get_apps_url(1145350),
        "steam-getapps-1145350",
        {
            "_TODO": TODO,
            "response": {"apps": [{"appid": 1145350, "name": "Hades II", "icon": "2d2d2d2d2d2d"}]},
        },
    )
    image_response(steamstore.icon_url(1145350, "2d2d2d2d2d2d"), "tiny.jpg")

    # -- 3. Fan Made Adventure: community only ------------------------------
    json_response(
        search_url("Fan Made Adventure"),
        "sgdb-search-fan-made-adventure",
        envelope([game(7001, "Fan Made Adventure", None) | {"verified": False}]),
    )
    json_response(
        game_url(7001), "sgdb-game-7001", envelope(game(7001, "Fan Made Adventure", None))
    )
    community = "https://cdn2.steamgriddb.com/file/sgdb-cdn/grid/full"
    json_response(
        asset_url("grids", 7001, dimensions="600x900", styles="alternate"),
        "sgdb-grids-7001-600x900",
        envelope(
            [
                # Deliberately NOT in score order: the client sorts.
                asset(101, f"{community}/fan-portrait-low.png", style="alternate", score=42,
                      mime="image/png"),
                asset(102, f"{community}/fan-portrait-best.png", style="alternate", score=97,
                      mime="image/png"),
            ]
        ),
    )
    image_response(f"{community}/fan-portrait-best.png", "tiny.png")
    json_response(
        asset_url("grids", 7001, dimensions="920x430", styles="alternate"),
        "sgdb-grids-7001-920x430-empty",
        envelope([]),
    )
    json_response(
        asset_url("grids", 7001, dimensions="460x215", styles="alternate"),
        "sgdb-grids-7001-460x215",
        envelope(
            [asset(103, f"{community}/fan-landscape.png", style="alternate", score=70,
                   mime="image/png")]
        ),
    )
    image_response(f"{community}/fan-landscape.png", "tiny.png")
    json_response(
        asset_url("heroes", 7001, dimensions="1920x620", styles="alternate"),
        "sgdb-heroes-7001-1920x620",
        envelope(
            [asset(104, f"{community}/fan-hero.jpg", style="alternate", score=55,
                   mime="image/jpeg")]
        ),
    )
    image_response(f"{community}/fan-hero.jpg", "tiny.jpg")
    json_response(
        asset_url("logos", 7001, styles="official"),
        "sgdb-logos-7001-official",
        envelope(
            # A .png URL whose bytes are actually WebP: the magic-byte sniff
            # must reject it and fall through to the `white` query.
            [asset(105, f"{community}/fan-logo-official.png", style="official", score=90,
                   mime="image/png")]
        ),
    )
    responses[f"{community}/fan-logo-official.png"] = {
        "status": 200,
        "body": "images/tiny.webp",
        "content_type": "image/png",
    }
    json_response(
        asset_url("logos", 7001, styles="white"),
        "sgdb-logos-7001-white",
        envelope(
            [asset(106, f"{community}/fan-logo-white.png", style="white", score=60,
                   mime="image/png")]
        ),
    )
    image_response(f"{community}/fan-logo-white.png", "tiny.png")
    json_response(
        asset_url("icons", 7001, styles="official", mimes=PNG_ONLY_MIMES),
        "sgdb-icons-7001-official",
        envelope(
            [asset(107, f"{community}/fan-icon.png", style="official", score=80,
                   mime="image/png")]
        ),
    )
    image_response(f"{community}/fan-icon.png", "tiny.png")

    # -- 4. Old Console Classic: Steam game, no logo on the CDN -------------
    json_response(
        search_url("Old Console Classic"),
        "sgdb-search-old-console-classic",
        envelope([game(8100, "Old Console Classic", 400910)]),
    )
    json_response(
        game_url(8100), "sgdb-game-8100", envelope(game(8100, "Old Console Classic", 400910))
    )
    image_response(steamstore.portrait_urls(400910)[0], "tiny.jpg")
    image_response(steamstore.landscape_urls(400910)[0], "tiny.jpg")
    image_response(steamstore.hero_urls(400910)[0], "tiny.jpg")
    # steamstore.logo_urls(400910)[0] is absent -> 404 -> community logo wins.
    json_response(
        asset_url("logos", 8100, styles="official"),
        "sgdb-logos-8100-official",
        envelope(
            [asset(108, f"{community}/occ-logo.png", style="official", score=75,
                   mime="image/png")]
        ),
    )
    image_response(f"{community}/occ-logo.png", "tiny.png")
    json_response(
        steamstore.get_apps_url(400910),
        "steam-getapps-400910",
        {
            "_TODO": TODO,
            "response": {
                "apps": [{"appid": 400910, "name": "Old Console Classic", "icon": "3e3e3e3e"}]
            },
        },
    )
    image_response(steamstore.icon_url(400910, "3e3e3e3e"), "tiny.jpg")

    # -- 5. Totally Unknown Title: zero results anywhere --------------------
    json_response(
        search_url("Totally Unknown Title"), "sgdb-search-unknown", envelope([])
    )
    json_response(
        steamstore.store_search_url("Totally Unknown Title"),
        "steam-storesearch-unknown",
        {"_TODO": TODO, "total": 0, "items": []},
    )

    # -- 6. Rate Limited Game: 429 forever ----------------------------------
    rate_limited(search_url("Rate Limited Game"))
    rate_limited(steamstore.store_search_url("Rate Limited Game"))

    manifest = {
        "_TODO": TODO,
        "_generated_by": "tests/fixtures/art/make_synthetic.py",
        "_note": (
            "url -> {status, body (a path in this directory, or null), content_type, "
            "headers}. Any URL not listed answers 404, which is what the Steam CDN "
            "does for an asset a game does not have."
        ),
        "responses": dict(sorted(responses.items())),
    }
    (HERE / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(responses)} responses to {HERE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
