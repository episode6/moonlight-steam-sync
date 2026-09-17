"""The HTTP layer: pacing, backoff, the hard stops, structured errors (spec 3.9)."""

from __future__ import annotations

import pytest

from moonlight_steam_sync import __version__
from moonlight_steam_sync.art.http import (
    MAX_CONSECUTIVE_NETWORK_ERRORS,
    MAX_CONSECUTIVE_RATE_LIMITS,
    Fetcher,
    HttpStatusError,
    NetworkError,
    NetworkHardStop,
    RateLimitHardStop,
    StreamResponse,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make(transport, *, interval_ms: int = 0, max_tries: int = 3) -> tuple[Fetcher, Clock]:
    clock = Clock()
    fetcher = Fetcher(
        transport=transport,
        interval_ms=interval_ms,
        max_tries=max_tries,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        jitter=lambda: 0.0,
    )
    return fetcher, clock


def ok(url: str, body: bytes = b"{}", status: int = 200, headers=None) -> StreamResponse:
    return StreamResponse(status, headers or {}, iter([body]), url)


def test_sends_user_agent_and_timeout() -> None:
    seen: dict[str, object] = {}

    def transport(url, headers, timeout):
        seen["headers"] = dict(headers)
        seen["timeout"] = timeout
        return ok(url)

    fetcher, _ = make(transport)
    fetcher.get_json("https://example.invalid/x")
    assert seen["headers"]["User-Agent"] == f"moonlight-steam-sync/{__version__}"
    assert seen["timeout"] == 10.0


def test_paces_between_requests() -> None:
    fetcher, clock = make(lambda url, headers, timeout: ok(url), interval_ms=250)
    fetcher.get_json("https://example.invalid/a")
    fetcher.get_json("https://example.invalid/b")
    fetcher.get_json("https://example.invalid/c")
    # No wait before the first call; 250 ms before each of the others.
    assert clock.slept == [0.25, 0.25]


def test_retries_5xx_then_gives_up_with_a_structured_error() -> None:
    calls: list[str] = []

    def transport(url, headers, timeout):
        calls.append(url)
        return ok(url, status=503)

    fetcher, clock = make(transport)
    with pytest.raises(HttpStatusError) as excinfo:
        fetcher.get_json("https://example.invalid/x")
    assert excinfo.value.status == 503
    assert len(calls) == 3  # MAX_TRIES
    assert clock.slept == [0.5, 1.0]  # backoff doubles, jitter pinned to 0


def test_429_backs_off_then_hard_stops_after_five_consecutive() -> None:
    calls: list[str] = []

    def transport(url, headers, timeout):
        calls.append(url)
        return ok(url, status=429, headers={"Retry-After": "1"})

    fetcher, _ = make(transport)
    with pytest.raises(HttpStatusError):
        fetcher.get_json("https://example.invalid/a")
    assert len(calls) == 3
    with pytest.raises(RateLimitHardStop):
        fetcher.get_json("https://example.invalid/b")
    assert len(calls) == MAX_CONSECUTIVE_RATE_LIMITS


def test_a_good_response_resets_the_429_counter() -> None:
    statuses = iter([429, 429, 200, 429, 429, 429])

    def transport(url, headers, timeout):
        return ok(url, status=next(statuses))

    fetcher, _ = make(transport)
    # 429, 429, then 200 on the third try: the counter goes back to zero.
    fetcher.get_json("https://example.invalid/a")
    # Three more 429s would be the 6th overall, but only the 3rd consecutive.
    with pytest.raises(HttpStatusError):
        fetcher.get_json("https://example.invalid/b")


def test_retry_after_raises_the_backoff_floor() -> None:
    def transport(url, headers, timeout):
        return ok(url, status=429, headers={"Retry-After": "30"})

    fetcher, clock = make(transport)
    with pytest.raises(HttpStatusError):
        fetcher.get_json("https://example.invalid/x")
    assert clock.slept == [30.0, 30.0]


def test_network_failures_hard_stop_when_they_keep_coming() -> None:
    def transport(url, headers, timeout):
        raise NetworkError("down")

    fetcher, _ = make(transport)
    with pytest.raises(NetworkError):
        fetcher.get_json("https://example.invalid/a")
    with pytest.raises(NetworkHardStop):
        fetcher.get_json("https://example.invalid/b")
    assert MAX_CONSECUTIVE_NETWORK_ERRORS == 5


def test_404_is_a_response_not_an_error() -> None:
    fetcher, _ = make(lambda url, headers, timeout: ok(url, status=404))
    response = fetcher.open("https://example.invalid/missing.jpg")
    assert response.status == 404


def test_malformed_json_is_reported_not_swallowed() -> None:
    fetcher, _ = make(lambda url, headers, timeout: ok(url, body=b"<html>nope</html>"))
    with pytest.raises(Exception, match="malformed JSON"):
        fetcher.get_json("https://example.invalid/x")
