"""Fixture-backed fake HTTP transport for the artwork tests.

The whole artwork stack reaches the network through one seam
(:data:`moonlight_steam_sync.art.http.Transport`), so the tests replace that
seam with :class:`FakeTransport`, which serves the recorded responses in
``tests/fixtures/art/manifest.json``.

TODO(real-data): those recordings are SYNTHETIC -- see
``tests/fixtures/art/make_synthetic.py`` for what each of the seven titles is
meant to exercise and how it was built from spec section 2.2. Replace them
with real captures by running ``SGDB_API_KEY=... python3
scripts/record_fixtures.py``. **Nothing in this module or in the tests reads
the fixture literals directly**: they look responses up by URL through the
manifest, so swapping in a real capture is a file replacement, not a test
rewrite.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from moonlight_steam_sync.art.http import Fetcher, NetworkError, StreamResponse

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "art"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"


def load_manifest() -> dict[str, dict]:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return payload["responses"]


@dataclass
class FakeTransport:
    """Serves ``manifest.json`` and records every URL it is asked for.

    A URL the manifest does not know answers 404 -- which is exactly what the
    Steam CDN does for an asset a game does not have, and what makes the
    "no logo on the CDN" fixture work without a special case.
    """

    responses: Mapping[str, dict] = field(default_factory=load_manifest)
    #: Every URL requested, in order.
    calls: list[str] = field(default_factory=list)
    #: URL -> exception to raise instead of answering (transport failures).
    fail_with: dict[str, Exception] = field(default_factory=dict)
    #: URL -> number of chunks to yield before raising ``chunk_error``.
    truncate_after: dict[str, int] = field(default_factory=dict)
    chunk_error: type[BaseException] = ConnectionResetError
    chunk_size: int = 8

    def __call__(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> StreamResponse:
        self.calls.append(url)
        failure = self.fail_with.get(url)
        if failure is not None:
            raise failure
        spec = self.responses.get(url)
        if spec is None:
            return StreamResponse(404, {}, iter([b""]), url)
        status = int(spec.get("status", 200))
        response_headers = dict(spec.get("headers") or {})
        body_path = spec.get("body")
        body = (FIXTURE_DIR / body_path).read_bytes() if body_path else b""
        content_type = spec.get("content_type")
        if content_type:
            response_headers.setdefault("Content-Type", content_type)
        limit = self.truncate_after.get(url)
        chunks = self._chunks(body, limit)
        return StreamResponse(status, response_headers, chunks, url)

    def _chunks(self, body: bytes, limit: int | None) -> Iterator[bytes]:
        if limit is None:
            yield body
            return
        emitted = 0
        for start in range(0, len(body), self.chunk_size):
            if emitted >= limit:
                raise self.chunk_error("connection reset mid-download")
            yield body[start : start + self.chunk_size]
            emitted += 1
        if emitted >= limit:
            raise self.chunk_error("connection reset mid-download")


def make_fetcher(transport: FakeTransport | None = None, **kwargs) -> Fetcher:
    """A :class:`Fetcher` that never sleeps and never touches the network."""
    transport = transport or FakeTransport()
    kwargs.setdefault("interval_ms", 0)
    clock = _Clock()
    return Fetcher(
        transport=transport,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        jitter=lambda: 0.0,
        **kwargs,
    )


class _Clock:
    """A fake clock: ``sleep`` advances it instead of blocking."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def network_error(url: str) -> NetworkError:
    return NetworkError(f"{url}: synthetic transport failure")


# ---------------------------------------------------------------------------
# a whole library's worth of answers, by pattern
# ---------------------------------------------------------------------------

#: The URLs one fully-official title costs, in the order the art phase
#: makes them: resolve (2), the four CDN downloads, the icon hash, the icon.
CALLS_PER_TITLE = 8


class SimulatedCrash(BaseException):
    """Stands in for ``kill -9`` / a power cut: not an ``Exception``, so no
    handler in the artwork stack can catch it and the run just dies."""


@dataclass
class BulkTransport:
    """Answers for every title in a library from URL patterns, no recordings.

    Built for the 500-title resumability test (spec PR-5 (b), 3.9): every
    title resolves to an exact SteamGridDB match with a Steam release, and
    the Steam CDN has all five assets, so a title costs exactly
    :data:`CALLS_PER_TITLE` calls (:meth:`title_calls` lists them). Ids are
    derived from the title's position in ``names`` so a real capture's
    names work as well as the synthetic "Synthetic Title 001" ones.

    TODO(real-data): this is a *synthetic* server, and so are the ids and
    the 1x1 images it serves. It is deliberately not a recording, because
    500 titles of recordings would be megabytes for no extra proof; the
    per-response shapes it emits are the same ones ``manifest.json`` (and a
    real ``scripts/record_fixtures.py`` capture) use.

    Failure knobs, all counted in calls *attempted* (the first call is 1):

    * ``crash_after=N`` -- call N+1 raises ``crash_with`` before answering.
    * ``truncate_call=N`` -- call N answers, but its body raises
      ``crash_with`` after one chunk (a download dies with a ``.part`` open).
    * ``rate_limit_after=N`` -- every call after N answers 429.
    """

    names: Sequence[str]
    calls: list[str] = field(default_factory=list)
    crash_after: int | None = None
    truncate_call: int | None = None
    rate_limit_after: int | None = None
    crash_with: BaseException = field(default_factory=lambda: SimulatedCrash("boom"))

    def __post_init__(self) -> None:
        self._index = {name: i + 1 for i, name in enumerate(self.names)}
        self._by_sgdb = {self.sgdb_id(i): i for i in self._index.values()}
        self._by_steam = {self.steam_appid(i): i for i in self._index.values()}
        self._png = (FIXTURE_DIR / "images" / "tiny.png").read_bytes()
        self._jpg = (FIXTURE_DIR / "images" / "tiny.jpg").read_bytes()

    # -- the ids and URLs one title maps to -----------------------------

    @staticmethod
    def sgdb_id(index: int) -> int:
        return 100000 + index

    @staticmethod
    def steam_appid(index: int) -> int:
        return 500000 + index

    @staticmethod
    def icon_hash(index: int) -> str:
        return f"h{index:04d}"

    def title_calls(self, name: str) -> list[str]:
        """Every URL a fresh, empty-slot run makes for ``name``, in order."""
        from urllib.parse import quote

        from moonlight_steam_sync.art import steamstore
        from moonlight_steam_sync.art.sgdb import BASE_URL

        i = self._index[name]
        appid = self.steam_appid(i)
        return [
            f"{BASE_URL}/search/autocomplete/{quote(name, safe='')}",
            f"{BASE_URL}/games/id/{self.sgdb_id(i)}?platformdata=steam",
            steamstore.portrait_urls(appid)[0],
            steamstore.landscape_urls(appid)[0],
            steamstore.hero_urls(appid)[0],
            steamstore.logo_urls(appid)[0],
            steamstore.get_apps_url(appid),
            steamstore.icon_url(appid, self.icon_hash(i)),
        ]

    # -- serving ---------------------------------------------------------

    def __call__(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> StreamResponse:
        self.calls.append(url)
        n = len(self.calls)
        if self.crash_after is not None and n > self.crash_after:
            raise self.crash_with
        if self.rate_limit_after is not None and n > self.rate_limit_after:
            return StreamResponse(429, {"Retry-After": "1"}, iter([b""]), url)
        status, body = self._answer(url)
        if status == 200 and self.truncate_call == n:
            return StreamResponse(200, {}, self._dying_body(body), url)
        return StreamResponse(status, {}, iter([body]), url)

    def _dying_body(self, body: bytes) -> Iterator[bytes]:
        yield body[:4]
        raise self.crash_with

    def _answer(self, url: str) -> tuple[int, bytes]:
        from urllib.parse import unquote

        path = url.split("://", 1)[-1]
        if "/api/v2/search/autocomplete/" in path:
            name = unquote(path.rsplit("/", 1)[-1])
            i = self._index.get(name)
            if i is None:
                return 200, json.dumps({"success": True, "data": []}).encode()
            return 200, json.dumps(
                {"success": True, "data": [self._game(i)]}
            ).encode()
        if "/api/v2/games/id/" in path:
            sgdb_id = int(path.split("/games/id/", 1)[1].split("?", 1)[0])
            i = self._by_sgdb.get(sgdb_id)
            if i is None:
                return 404, b""
            return 200, json.dumps({"success": True, "data": self._game(i)}).encode()
        if "ICommunityService/GetApps" in path:
            appid = int(unquote(path.split("appids", 1)[1]).split("=", 1)[1])
            i = self._by_steam.get(appid)
            if i is None:
                return 404, b""
            payload = {"response": {"apps": [{"appid": appid, "icon": self.icon_hash(i)}]}}
            return 200, json.dumps(payload).encode()
        if "/steam/apps/" in path:
            tail = path.rsplit("/", 1)[-1]
            if tail in ("library_600x900_2x.jpg", "header.jpg", "library_hero.jpg"):
                return 200, self._jpg
            if tail == "logo.png":
                return 200, self._png
            return 404, b""
        if "/steamcommunity/public/images/apps/" in path:
            return 200, self._jpg
        return 404, b""

    def _game(self, i: int) -> dict:
        return {
            "id": self.sgdb_id(i),
            "name": self.names[i - 1],
            "types": ["steam"],
            "verified": True,
            "release_date": 1600000000,
            "external_platform_data": {
                "steam": [{"id": str(self.steam_appid(i)), "metadata": {}}]
            },
        }
