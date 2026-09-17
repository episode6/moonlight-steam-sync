"""HTTP plumbing shared by the artwork clients.

stdlib only (``urllib``), with everything the resumability contract in spec
3.9 asks for built in:

* ``User-Agent: moonlight-steam-sync/<version>`` and a 10 s timeout on every
  call;
* ``request_interval_ms`` pacing *between* calls, so a 500-title run stays
  polite without the callers having to think about it;
* backoff with jitter on 429/5xx, 3 tries per request;
* a **hard stop after 5 consecutive 429s** (:class:`RateLimitHardStop`) and
  after 5 consecutive transport failures (:class:`NetworkHardStop`), both of
  which end the run with exit code 4 and everything done so far kept;
* structured errors, so a caller can tell "this slot has no image" (a 404)
  from "the run must stop" (a hard stop).

Everything reaches the network through a single :data:`Transport` seam: a
callable ``(url, headers, timeout) -> StreamResponse``. Tests inject a fake
that serves recorded fixtures and records the URLs that were asked for; the
shipped default is :func:`urllib_transport`.

Responses stream in chunks rather than arriving as one ``bytes``: that is what
lets a download write ``<file>.part`` incrementally, so an interrupted
download leaves a ``.part`` and never a truncated file that would count as
done (spec 3.9 item 3). A transport failure *while the body is streaming* --
the likeliest failure on a Deck over Wi-Fi in a 2,500-call run -- is
translated into :class:`NetworkError` and counted by the same
consecutive-failure counter as a failure on the request itself, so it ends a
run with exit code 4 rather than a bare ``OSError`` traceback.
"""

from __future__ import annotations

import http.client
import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from moonlight_steam_sync import __version__

USER_AGENT = f"moonlight-steam-sync/{__version__}"
DEFAULT_TIMEOUT = 10.0

#: Attempts per request before giving up on it (spec 3.9 item 6).
MAX_TRIES = 3
#: Consecutive 429s across the whole run before stopping rather than burning
#: the key (spec 3.9 item 6).
MAX_CONSECUTIVE_RATE_LIMITS = 5
#: Consecutive transport failures before calling the network dead (exit 4).
MAX_CONSECUTIVE_NETWORK_ERRORS = 5
#: First backoff step in seconds; doubled per attempt, plus jitter.
BACKOFF_BASE_SECONDS = 0.5
#: Never wait longer than this for a ``Retry-After`` we were handed.
MAX_RETRY_AFTER_SECONDS = 60.0

_CHUNK_SIZE = 64 * 1024


class HttpError(Exception):
    """Base for everything this module raises."""


class NetworkError(HttpError):
    """The transport failed (DNS, refused, timeout, TLS...)."""


class HttpStatusError(HttpError):
    """A response arrived, but not one we can use."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


class HardStop(HttpError):
    """The run must end now, keeping everything already written (exit 4)."""


class RateLimitHardStop(HardStop):
    """Five consecutive 429s: stop rather than keep hammering the API."""


class NetworkHardStop(HardStop):
    """Five consecutive transport failures: the network is gone."""


#: What a dying connection raises *while the body is being read*, as opposed
#: to from the request itself. ``http.client`` raises ``IncompleteRead``;
#: everything else (``socket.timeout``, ``ConnectionResetError``) is an
#: ``OSError``.
BODY_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    NetworkError,
    OSError,
    http.client.HTTPException,
)


@dataclass
class StreamResponse:
    """A response whose body is consumed as an iterator of chunks."""

    status: int
    headers: Mapping[str, str]
    chunks: Iterator[bytes]
    url: str
    #: Fired once when the exchange finishes cleanly -- the body was read to
    #: the end, or the response was closed without an error. :class:`Fetcher`
    #: hangs the "the network is alive again" reset off it.
    on_complete: Callable[[], None] | None = field(default=None, repr=False)

    def read_all(self) -> bytes:
        return b"".join(self.chunks)

    def complete(self) -> None:
        """Fire :attr:`on_complete`, at most once."""
        callback, self.on_complete = self.on_complete, None
        if callback is not None:
            callback()

    def close(self) -> None:
        close = getattr(self.chunks, "close", None)
        if close is not None:
            close()
        # Headers arrived and we chose not to read the body (a 404, a 429 we
        # are about to retry): the exchange itself succeeded.
        self.complete()


class Transport(Protocol):
    """The single seam through which this package reaches the network."""

    def __call__(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> StreamResponse: ...


def _iter_body(response: Any) -> Iterator[bytes]:
    try:
        while True:
            try:
                chunk = response.read(_CHUNK_SIZE)
            except (OSError, http.client.HTTPException) as exc:
                # The connection died mid-body. Callers up the stack handle
                # HttpError; a raw OSError would escape every one of them.
                raise NetworkError(f"{response.url}: {exc}") from exc
            if not chunk:
                return
            yield chunk
    finally:
        response.close()


def urllib_transport(
    url: str, headers: Mapping[str, str], timeout: float
) -> StreamResponse:
    """The shipped :data:`Transport`: one ``urllib`` GET."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        response = urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
    except urllib.error.HTTPError as exc:
        # An error status is a *successful* exchange as far as pacing and the
        # network-dead counter are concerned; hand it back as a response.
        body = exc.read()
        exc.close()
        return StreamResponse(exc.code, dict(exc.headers or {}), iter([body]), url)
    except (urllib.error.URLError, OSError) as exc:
        raise NetworkError(f"{url}: {exc}") from exc
    return StreamResponse(response.status, dict(response.headers), _iter_body(response), url)


@dataclass
class Fetcher:
    """Paced, retrying GETs over a :data:`Transport`.

    ``interval_ms`` is the config's ``request_interval_ms``. ``sleep``,
    ``monotonic`` and ``jitter`` are injected so tests can assert on the
    backoff schedule without waiting for it.
    """

    transport: Transport = urllib_transport
    interval_ms: int = 250
    timeout: float = DEFAULT_TIMEOUT
    max_tries: int = MAX_TRIES
    user_agent: str = USER_AGENT
    sleep: Any = time.sleep
    monotonic: Any = time.monotonic
    jitter: Any = random.random
    _last_request_at: float | None = field(default=None, init=False, repr=False)
    _consecutive_rate_limits: int = field(default=0, init=False, repr=False)
    _consecutive_network_errors: int = field(default=0, init=False, repr=False)
    #: Every URL this fetcher has asked for, in order (tests assert on it; a
    #: run uses it for ``--explain``).
    requested_urls: list[str] = field(default_factory=list, repr=False)

    def open(
        self, url: str, *, headers: Mapping[str, str] | None = None, accept: str = "*/*"
    ) -> StreamResponse:
        """GET ``url``, retrying 429/5xx, and return a 2xx/3xx/4xx response.

        Raises :class:`NetworkError` / :class:`HttpStatusError` when the
        request itself is a write-off, and :class:`HardStop` when the whole
        run should end.
        """
        request_headers = {"User-Agent": self.user_agent, "Accept": accept}
        if headers:
            request_headers.update(headers)

        for attempt in range(1, self.max_tries + 1):
            self._pace()
            self.requested_urls.append(url)
            try:
                response = self.transport(url, request_headers, self.timeout)
            except NetworkError:
                failure = self._network_failure(url)
                if isinstance(failure, HardStop):
                    raise failure from None
                if attempt == self.max_tries:
                    raise
                self._backoff(attempt, None)
                continue
            response = self._watch_body(response)

            if response.status == 429:
                response.close()
                self._consecutive_rate_limits += 1
                if self._consecutive_rate_limits >= MAX_CONSECUTIVE_RATE_LIMITS:
                    raise RateLimitHardStop(
                        f"{self._consecutive_rate_limits} consecutive HTTP 429s; stopping "
                        "rather than burning the key. Everything already written is kept; "
                        "rerun to continue."
                    )
                if attempt == self.max_tries:
                    raise HttpStatusError(429, url)
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            self._consecutive_rate_limits = 0

            if 500 <= response.status < 600:
                response.close()
                if attempt == self.max_tries:
                    raise HttpStatusError(response.status, url)
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            return response

        raise AssertionError("unreachable: retry loop always returns or raises")

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        """GET ``url`` and parse the body as JSON; non-200 raises."""
        response = self.open(url, headers=headers, accept="application/json")
        if response.status != 200:
            response.close()
            raise HttpStatusError(response.status, url)
        body = response.read_all()
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HttpError(f"{url}: malformed JSON response") from exc

    # -- the network-alive counter ---------------------------------------

    def _network_failure(self, detail: str) -> HttpError:
        """Count one transport failure and say what to raise for it.

        Failures during a request and failures halfway through a body feed
        the same counter (spec 3.9 item 6): five in a row means the network
        is gone, which ends the run with exit code 4 and everything already
        written kept.
        """
        self._consecutive_network_errors += 1
        if self._consecutive_network_errors >= MAX_CONSECUTIVE_NETWORK_ERRORS:
            return NetworkHardStop(
                f"{self._consecutive_network_errors} consecutive network failures; "
                "stopping. Everything already written is kept; rerun to continue."
            )
        return NetworkError(detail)

    def _network_ok(self) -> None:
        self._consecutive_network_errors = 0

    def _watch_body(self, response: StreamResponse) -> StreamResponse:
        """Wrap the body so a connection that dies mid-stream is structured.

        Without this, a ``ConnectionResetError`` (or an ``IncompleteRead``,
        or a read timeout) raised while a download iterates the chunks walks
        straight past every ``except HttpError`` in the artwork stack and out
        of ``main`` as a traceback.
        """
        source = response.chunks

        def watched() -> Iterator[bytes]:
            try:
                yield from source
            except BODY_TRANSPORT_ERRORS as exc:
                detail = str(exc) if isinstance(exc, NetworkError) else f"{response.url}: {exc}"
                raise self._network_failure(detail) from exc
            finally:
                close = getattr(source, "close", None)
                if close is not None:
                    close()
            response.complete()

        response.chunks = watched()
        response.on_complete = self._network_ok
        return response

    def _pace(self) -> None:
        if self.interval_ms <= 0:
            return
        interval = self.interval_ms / 1000.0
        now = self.monotonic()
        if self._last_request_at is not None:
            wait = interval - (now - self._last_request_at)
            if wait > 0:
                self.sleep(wait)
                now = self.monotonic()
        self._last_request_at = now

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) * (1.0 + self.jitter())
        parsed = _parse_retry_after(retry_after)
        if parsed is not None:
            delay = max(delay, parsed)
        self.sleep(delay)
        # The pacing clock restarts after a backoff sleep.
        self._last_request_at = self.monotonic()


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)
