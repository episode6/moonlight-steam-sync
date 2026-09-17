"""Fixture-backed fake HTTP transport for the artwork tests.

The whole artwork stack reaches the network through one seam
(:data:`moonlight_steam_sync.art.http.Transport`), so the tests replace that
seam with :class:`FakeTransport`, which serves the recorded responses in
``tests/fixtures/art/manifest.json``.

TODO(real-data): those recordings are SYNTHETIC -- see
``tests/fixtures/art/make_synthetic.py`` for what each of the six titles is
meant to exercise and how it was built from spec section 2.2. Replace them
with real captures by running ``SGDB_API_KEY=... python3
scripts/record_fixtures.py``. **Nothing in this module or in the tests reads
the fixture literals directly**: they look responses up by URL through the
manifest, so swapping in a real capture is a file replacement, not a test
rewrite.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
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
