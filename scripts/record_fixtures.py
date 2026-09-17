#!/usr/bin/env python3
"""Refresh the artwork test fixtures from the real APIs (dev tool, not shipped).

The fixtures in ``tests/fixtures/art/`` start out SYNTHETIC -- invented from
the endpoint shapes in spec section 2.2, because no SteamGridDB key was
available when they were written. This script replaces them with real
captures::

    SGDB_API_KEY=xxxxxxxx python3 scripts/record_fixtures.py

    # or a different set of titles
    SGDB_API_KEY=xxxxxxxx python3 scripts/record_fixtures.py "Elden Ring" "Hades II"

What it records, per title, is exactly the call chain
:mod:`moonlight_steam_sync.art.resolve` and
:mod:`moonlight_steam_sync.art.select` would make:

* ``/search/autocomplete/{title}``
* ``/games/id/{id}?platformdata=steam`` for the chosen game
* every community query for every slot (grids/heroes/logos/icons)
* every Steam CDN URL for the slots, plus ``storesearch`` and ``GetApps``

JSON bodies are written verbatim to ``responses/``. **Image bodies are not
kept**: the manifest points at the tiny 1x1 stand-ins in ``images/``, because
the only thing the selector reads out of an image is its magic bytes, and
committing real capsules would add megabytes and a licensing question for no
test value. What *is* captured from the CDN is the thing that matters: which
URLs exist and which 404.

The key is read from the environment and never written to any file this
script produces. Check the diff before committing anyway.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from moonlight_steam_sync.art import steamstore  # noqa: E402
from moonlight_steam_sync.art.http import Fetcher, HttpError  # noqa: E402
from moonlight_steam_sync.art.resolve import pick  # noqa: E402
from moonlight_steam_sync.art.select import COMMUNITY_QUERIES, SLOTS  # noqa: E402
from moonlight_steam_sync.art.sgdb import SgdbClient  # noqa: E402
from moonlight_steam_sync.art.steamstore import SteamStoreClient  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "art"

#: The six cases spec PR-4 asks the fixture set to cover. Swap in the real
#: titles that exercise them on your library; the last one is expected to
#: match nothing.
DEFAULT_TITLES = [
    "Elden Ring",
    "Hades II™",
    "Fan Made Adventure",
    "Old Console Classic",
    "Totally Unknown Title",
]


def slugify(url: str) -> str:
    keep = [c if c.isalnum() else "-" for c in url.split("://", 1)[-1]]
    slug = "".join(keep).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:120]


class Recorder:
    """Wraps a :class:`Fetcher` and writes every response it sees."""

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        self.responses: dict[str, dict[str, Any]] = {}

    def json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        response = self.fetcher.open(url, headers=headers, accept="application/json")
        body = response.read_all()
        if response.status != 200:
            self.responses[url] = {"status": response.status, "body": None}
            return None
        name = slugify(url)
        (FIXTURE_DIR / "responses" / f"{name}.json").write_bytes(body)
        self.responses[url] = {
            "status": 200,
            "body": f"responses/{name}.json",
            "content_type": "application/json",
        }
        try:
            return json.loads(body)
        except ValueError:
            return None

    def image(self, url: str) -> bool:
        """Record whether ``url`` exists; point the body at a stand-in image."""
        response = self.fetcher.open(url, accept="image/png,image/jpeg")
        head = b""
        for chunk in response.chunks:
            head += chunk
            if len(head) >= 16:
                break
        response.close()
        if response.status != 200:
            self.responses[url] = {"status": response.status, "body": None}
            return False
        is_png = head.startswith(b"\x89PNG")
        self.responses[url] = {
            "status": 200,
            "body": "images/tiny.png" if is_png else "images/tiny.jpg",
            "content_type": "image/png" if is_png else "image/jpeg",
            "_recorded": "real URL, stand-in body (only the magic bytes are read)",
        }
        return True


def record_title(title: str, recorder: Recorder, sgdb: SgdbClient, store: SteamStoreClient) -> None:
    print(f"== {title}")
    auth = {"Authorization": f"Bearer {sgdb.api_key}"}
    results = recorder.json(sgdb.url(f"/search/autocomplete/{_quote(title)}"), auth) or {}
    games = results.get("data") or []

    chosen, how = pick(games, title)
    print(f"   autocomplete -> {how}")
    sgdb_id = int(chosen["id"]) if chosen and chosen.get("id") is not None else None
    steam_appid = None
    if sgdb_id is not None:
        game = recorder.json(sgdb.url(f"/games/id/{sgdb_id}", {"platformdata": "steam"}), auth)
        data = (game or {}).get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        entries = (data.get("external_platform_data") or {}).get("steam") or []
        if entries:
            steam_appid = int(entries[0]["id"])
    print(f"   sgdb_id={sgdb_id} steam_appid={steam_appid}")

    if steam_appid is None:
        payload = recorder.json(steamstore.store_search_url(title)) or {}
        items = payload.get("items") or []
        store_pick, how = pick(items, title, prefer_verified=False)
        if store_pick:
            steam_appid = int(store_pick["id"])
            print(f"   storesearch -> {how} appid={steam_appid}")

    if steam_appid is not None:
        for builder in (
            steamstore.portrait_urls,
            steamstore.landscape_urls,
            steamstore.hero_urls,
            steamstore.logo_urls,
        ):
            for url in builder(steam_appid):
                recorder.image(url)
        apps = recorder.json(steamstore.get_apps_url(steam_appid)) or {}
        for app in (apps.get("response") or {}).get("apps") or []:
            if app.get("icon"):
                recorder.image(steamstore.icon_url(steam_appid, str(app["icon"])))
                break

    if sgdb_id is None:
        return
    for slot in SLOTS:
        for kind, params in COMMUNITY_QUERIES[slot.key]:
            query: dict[str, Any] = {"types": "static"}
            query.update(params)
            query.setdefault("mimes", "image/png,image/jpeg")
            payload = recorder.json(sgdb.url(f"/{kind}/game/{sgdb_id}", query), auth)
            assets = (payload or {}).get("data") or []
            assets = sorted(assets, key=lambda a: a.get("score", 0), reverse=True)
            if assets and isinstance(assets[0].get("url"), str):
                recorder.image(assets[0]["url"])


def _quote(term: str) -> str:
    from urllib.parse import quote

    return quote(term, safe="")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("titles", nargs="*", default=None, help="titles to record")
    parser.add_argument(
        "--interval-ms", type=int, default=500, help="pacing between calls (default 500)"
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("SGDB_API_KEY", "").strip()
    if not api_key:
        print(
            "SGDB_API_KEY is not set. Get one at "
            "https://www.steamgriddb.com/profile/preferences/api and export it; "
            "it is never written to any file this script produces.",
            file=sys.stderr,
        )
        return 1

    (FIXTURE_DIR / "responses").mkdir(parents=True, exist_ok=True)
    fetcher = Fetcher(interval_ms=args.interval_ms)
    sgdb = SgdbClient(api_key=api_key, fetcher=fetcher)
    store = SteamStoreClient(fetcher=fetcher)
    recorder = Recorder(fetcher)

    for title in args.titles or DEFAULT_TITLES:
        try:
            record_title(title, recorder, sgdb, store)
        except HttpError as exc:
            print(f"   !! {exc}", file=sys.stderr)

    manifest_path = FIXTURE_DIR / "manifest.json"
    existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing["responses"].update(recorder.responses)
    existing["responses"] = dict(sorted(existing["responses"].items()))
    existing["_TODO"] = (
        "Partly real. Any entry not refreshed by scripts/record_fixtures.py is still "
        "synthetic -- see tests/fixtures/art/make_synthetic.py."
    )
    manifest_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    print(f"recorded {len(recorder.responses)} responses into {FIXTURE_DIR}")
    print("review the diff before committing: no key should appear in it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
