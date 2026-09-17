"""Steam store search, the community icon hash, and the CDN URL builders.

This is the "official art" half of spec 3.5, and the only half that works
without a SteamGridDB key (spec 6.12). Everything here is an unauthenticated
public endpoint verified live on 2026-09-16 (spec 2.2):

* ``https://store.steampowered.com/api/storesearch/?term=...&l=english&cc=US``
  -> ``items[{id, name}]``, used to turn a title into a Steam appid when
  SteamGridDB has nothing;
* ``https://api.steampowered.com/ICommunityService/GetApps/v1/?appids[0]=<id>``
  -> the app's ``icon`` hash, which is the only way to build the 32x32 icon
  URL;
* the ``cdn.cloudflare.steamstatic.com`` asset paths Steam ROM Manager
  rebuilds from ``platformdata=steam`` metadata.

Note that ``library_header.jpg`` does **not** exist on the CDN -- the
landscape/header slot is plain ``header.jpg``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from moonlight_steam_sync.art.http import Fetcher

STORE_SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
GET_APPS_URL = "https://api.steampowered.com/ICommunityService/GetApps/v1/"
CDN_BASE = "https://cdn.cloudflare.steamstatic.com"


def portrait_urls(appid: int) -> list[str]:
    """Portrait capsule (600x900), 2x first."""
    return [
        f"{CDN_BASE}/steam/apps/{appid}/library_600x900_2x.jpg",
        f"{CDN_BASE}/steam/apps/{appid}/library_600x900.jpg",
    ]


def landscape_urls(appid: int) -> list[str]:
    """Landscape/header capsule (460x215)."""
    return [f"{CDN_BASE}/steam/apps/{appid}/header.jpg"]


def hero_urls(appid: int) -> list[str]:
    """Hero banner."""
    return [f"{CDN_BASE}/steam/apps/{appid}/library_hero.jpg"]


def logo_urls(appid: int) -> list[str]:
    """Transparent logo drawn over the hero."""
    return [f"{CDN_BASE}/steam/apps/{appid}/logo.png"]


def icon_url(appid: int, icon_hash: str) -> str:
    """The 32x32 client icon, addressed by the hash :func:`app_icon_hash` returns."""
    return f"{CDN_BASE}/steamcommunity/public/images/apps/{appid}/{icon_hash}.jpg"


def store_search_url(term: str) -> str:
    return f"{STORE_SEARCH_URL}?{urlencode({'term': term, 'l': 'english', 'cc': 'US'})}"


def get_apps_url(appid: int) -> str:
    return f"{GET_APPS_URL}?{urlencode({'appids[0]': appid})}"


@dataclass
class SteamStoreClient:
    """The two JSON endpoints; the CDN builders above need no client."""

    fetcher: Fetcher

    def search(self, term: str) -> list[dict[str, Any]]:
        """``storesearch`` -> ``items``, best-first, as Steam ranks them."""
        payload = self.fetcher.get_json(store_search_url(term))
        if not isinstance(payload, Mapping):
            return []
        items = payload.get("items") or []
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    def app_icon_hash(self, appid: int) -> str | None:
        """The ``icon`` hash for ``appid``, or ``None`` when Steam has none."""
        payload = self.fetcher.get_json(get_apps_url(appid))
        if not isinstance(payload, Mapping):
            return None
        apps = (payload.get("response") or {}).get("apps") or []
        if not isinstance(apps, list):
            return None
        for app in apps:
            if not isinstance(app, Mapping):
                continue
            icon = app.get("icon")
            if icon:
                return str(icon)
        return None
