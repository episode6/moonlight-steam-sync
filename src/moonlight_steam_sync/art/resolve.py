"""Step A of spec 3.5: a Moonlight title -> a Steam appid and/or a SGDB id.

Also home to the match cache, which is the resumability contract's record of
"this title is resolved" (spec 3.9 item 2):

* ``~/.cache/moonlight-steam-sync/matches.json``, one entry per Moonlight app
  name;
* written with an atomic ``os.replace`` **immediately after each title is
  resolved**, never batched to the end of the run;
* negative results (no match, and per-slot "nothing found") are cached with a
  timestamp and are not re-queried for 7 days, so a re-run does not re-search
  200 unmatched titles. ``--retry-missing`` opts back in for this run;
  ``art --force`` ignores the cache outright.

Matching order (spec 3.5 step A): config override, then the cache, then
SteamGridDB autocomplete, then -- if there is no key, or SteamGridDB returned
nothing -- Steam's own store search. Both searches use the same normalisation
and the same pick rules.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from moonlight_steam_sync.art.http import HardStop, HttpError
from moonlight_steam_sync.art.sgdb import SgdbClient, steam_appid_from_game
from moonlight_steam_sync.art.steamstore import SteamStoreClient

#: How long a negative result stays cached (spec 6.10).
NEGATIVE_TTL = timedelta(days=7)

CACHE_VERSION = 1

_TRADEMARKS = str.maketrans({"™": "", "®": "", "©": ""})
_TRAILING_YEAR = re.compile(r"\(\s*(?:19|20)\d{2}\s*\)\s*$")
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def normalise(name: str) -> str:
    """Casefold, drop (tm)/(R)/(C), strip a trailing ``(YYYY)``, punctuation -> space.

    >>> normalise("Hades II™ (2024)")
    'hades ii'
    """
    text = name.translate(_TRADEMARKS).casefold().strip()
    text = _TRAILING_YEAR.sub("", text)
    text = _NON_WORD.sub(" ", text)
    return " ".join(text.split())


def cache_dir() -> Path:
    """``$XDG_CACHE_HOME/moonlight-steam-sync`` (``~/.cache`` by default)."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "moonlight-steam-sync"


def default_cache_path() -> Path:
    return cache_dir() / "matches.json"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@dataclass
class Match:
    """What we know about one Moonlight title.

    ``how`` is the match chain's verdict, printed by ``art --explain``:
    ``override``, ``cache``, ``sgdb:exact-verified``, ``sgdb:exact``,
    ``sgdb:fuzzy``, ``steamstore:exact``, ``steamstore:fuzzy``, ``none`` or
    ``skipped`` (``art = false`` in ``[overrides]``).
    """

    name: str
    steam_appid: int | None = None
    sgdb_id: int | None = None
    matched_name: str | None = None
    how: str = "none"
    when: str = field(default_factory=lambda: _iso(_now()))
    #: slot key -> ISO timestamp of the last time that slot found nothing.
    missing_slots: dict[str, str] = field(default_factory=dict)
    #: Human-readable match chain; never persisted.
    explain: list[str] = field(default_factory=list, compare=False)
    #: True when the search failed for a transient reason (a 5xx, a timeout,
    #: a 429 that exhausted its retries). Never persisted and never cached:
    #: a transient failure must not poison the 7-day negative window.
    transient: bool = field(default=False, compare=False)

    @property
    def found(self) -> bool:
        return self.steam_appid is not None or self.sgdb_id is not None

    @property
    def skipped(self) -> bool:
        return self.how == "skipped"

    def to_json(self) -> dict[str, Any]:
        return {
            "steam_appid": self.steam_appid,
            "sgdb_id": self.sgdb_id,
            "matched_name": self.matched_name,
            "how": self.how,
            "when": self.when,
            "missing_slots": dict(self.missing_slots),
        }

    @classmethod
    def from_json(cls, name: str, data: Mapping[str, Any]) -> Match:
        missing = data.get("missing_slots") or {}
        return cls(
            name=name,
            steam_appid=_as_int(data.get("steam_appid")),
            sgdb_id=_as_int(data.get("sgdb_id")),
            matched_name=data.get("matched_name"),
            how=str(data.get("how") or "none"),
            when=str(data.get("when") or ""),
            missing_slots={str(k): str(v) for k, v in missing.items()}
            if isinstance(missing, Mapping)
            else {},
        )


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class MatchCache:
    """``matches.json``: load once, flush after every title (spec 3.9 item 2)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._entries: dict[str, Match] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return
        except OSError:
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            # A corrupt cache is a cache miss, never a crash: the whole point
            # of the file is that it can be thrown away and rebuilt.
            return
        titles = payload.get("titles") if isinstance(payload, Mapping) else None
        if not isinstance(titles, Mapping):
            return
        for name, data in titles.items():
            if isinstance(data, Mapping):
                self._entries[str(name)] = Match.from_json(str(name), data)

    def __contains__(self, name: str) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def names(self) -> Iterable[str]:
        return tuple(self._entries)

    def get(self, name: str) -> Match | None:
        return self._entries.get(name)

    def put(self, match: Match) -> None:
        """Record a resolution and write the file straight away."""
        self._entries[match.name] = match
        self._dirty = True
        self.flush()

    def record_missing_slot(self, name: str, slot: str, when: datetime | None = None) -> None:
        """Remember that ``slot`` found nothing, subject to the same 7-day rule.

        A title with no entry yet is a title whose own lookup never
        succeeded, and a failed lookup is never cached (spec 6.10): creating
        an entry here would start a 7-day negative window off the back of a
        SteamGridDB 5xx or a timeout. So this is a no-op until
        :meth:`put` has recorded a resolution for ``name``.
        """
        entry = self._entries.get(name)
        if entry is None:
            return
        entry.missing_slots[slot] = _iso(when or _now())
        self._dirty = True

    def clear_missing_slot(self, name: str, slot: str) -> None:
        entry = self._entries.get(name)
        if entry is not None and entry.missing_slots.pop(slot, None) is not None:
            self._dirty = True

    def slot_is_known_missing(self, name: str, slot: str, now: datetime | None = None) -> bool:
        entry = self._entries.get(name)
        if entry is None:
            return False
        return is_fresh(entry.missing_slots.get(slot), now)

    def flush(self) -> None:
        """Atomic whole-file replace; a no-op when nothing changed."""
        if not self._dirty:
            return
        payload = {
            "version": CACHE_VERSION,
            "titles": {name: match.to_json() for name, match in sorted(self._entries.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        self._dirty = False


def is_fresh(timestamp: Any, now: datetime | None = None) -> bool:
    """True when a negative result is still inside its 7-day window."""
    moment = _parse_iso(timestamp)
    if moment is None:
        return False
    return (now or _now()) - moment < NEGATIVE_TTL


def pick(
    candidates: Iterable[Mapping[str, Any]],
    wanted: str,
    *,
    name_key: str = "name",
    prefer_verified: bool = True,
) -> tuple[dict[str, Any] | None, str]:
    """The pick rules from spec 3.5 step A.2, shared by both searches.

    In order: an exact normalised match that is ``verified``; an exact
    normalised match; the first result whose normalised name is a prefix of
    ours or vice-versa (reported as ``fuzzy``); else nothing.
    """
    target = normalise(wanted)
    results = [c for c in candidates if isinstance(c, Mapping)]
    exact = [c for c in results if normalise(str(c.get(name_key) or "")) == target]
    if prefer_verified:
        for candidate in exact:
            if candidate.get("verified"):
                return dict(candidate), "exact-verified"
    if exact:
        return dict(exact[0]), "exact"
    if target:
        for candidate in results:
            other = normalise(str(candidate.get(name_key) or ""))
            if other and (other.startswith(target) or target.startswith(other)):
                return dict(candidate), "fuzzy"
    return None, "none"


@dataclass
class Resolver:
    """Title -> :class:`Match`, with the cache and the overrides applied."""

    cache: MatchCache
    sgdb: SgdbClient | None = None
    store: SteamStoreClient | None = None
    overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: ``art --force``: ignore the cache entirely and re-resolve.
    force: bool = False
    #: ``--retry-missing``: re-query titles and slots whose miss is cached.
    retry_missing: bool = False

    def resolve(self, name: str) -> Match:
        """Resolve ``name``, flushing the cache before returning (spec 3.5 A.4)."""
        explain: list[str] = []

        override = self._override(name)
        if override is not None:
            if override.get("art") is False:
                match = Match(name=name, how="skipped", explain=["override: art = false"])
                return match
            steam_appid = _as_int(override.get("steam"))
            sgdb_id = _as_int(override.get("sgdb"))
            explain.append(f"override: steam={steam_appid} sgdb={sgdb_id}")
            if sgdb_id is not None and steam_appid is None:
                pinned = self._cached_override(name, sgdb_id)
                if pinned is not None:
                    steam_appid = pinned
                    explain.append(f"cache: steam appid {pinned} for the pinned sgdb id")
                elif self._sgdb_usable():
                    steam_appid = self._steam_appid_for(sgdb_id, explain)
            match = Match(
                name=name,
                steam_appid=steam_appid,
                sgdb_id=sgdb_id,
                matched_name=name,
                how="override",
                explain=explain,
            )
            self.cache.put(match)
            return match

        cached = None if self.force else self.cache.get(name)
        if cached is not None and (cached.found or self._negative_still_valid(cached)):
            cached.explain = [f"cache: how={cached.how} when={cached.when}"]
            return cached

        match = self._search(name, explain)
        if match.transient:
            # Do not write a negative result we are not sure about.
            return match
        self.cache.put(match)
        return match

    # -- internals -------------------------------------------------------

    def _override(self, name: str) -> Mapping[str, Any] | None:
        value = self.overrides.get(name)
        return value if isinstance(value, Mapping) else None

    def _cached_override(self, name: str, sgdb_id: int) -> int | None:
        """The Steam appid a previous run already looked up for this pin.

        An ``[overrides] sgdb = N`` pin otherwise costs one
        ``games/id/N?platformdata=steam`` call *per run*, against spec 3.9's
        "a second pass does only the remaining work". Only a cached entry
        written by the same pin counts, so editing the pin re-queries.
        """
        if self.force:
            return None
        cached = self.cache.get(name)
        if cached is None or cached.how != "override" or cached.sgdb_id != sgdb_id:
            return None
        return cached.steam_appid

    def _negative_still_valid(self, cached: Match) -> bool:
        if self.retry_missing:
            return False
        return is_fresh(cached.when)

    def _sgdb_usable(self) -> bool:
        return self.sgdb is not None and self.sgdb.enabled

    def _search(self, name: str, explain: list[str]) -> Match:
        transient = False
        if self._sgdb_usable():
            assert self.sgdb is not None
            try:
                results = self.sgdb.search(name)
            except HardStop:
                raise
            except HttpError as exc:
                explain.append(f"sgdb autocomplete failed: {exc}")
                results, transient = [], True
            chosen, how = pick(results, name)
            if not transient:
                explain.append(f"sgdb autocomplete: {len(results)} result(s) -> {how}")
            if chosen is not None:
                sgdb_id = _as_int(chosen.get("id"))
                steam_appid = (
                    self._steam_appid_for(sgdb_id, explain) if sgdb_id is not None else None
                )
                return Match(
                    name=name,
                    steam_appid=steam_appid,
                    sgdb_id=sgdb_id,
                    matched_name=str(chosen.get("name") or ""),
                    how=f"sgdb:{how}",
                    explain=explain,
                )
        else:
            explain.append("sgdb: no api key, skipping")

        if self.store is not None:
            store_failed = False
            try:
                items = self.store.search(name)
            except HardStop:
                raise
            except HttpError as exc:
                explain.append(f"steam storesearch failed: {exc}")
                items, transient, store_failed = [], True, True
            chosen, how = pick(items, name, prefer_verified=False)
            if not store_failed:
                explain.append(f"steam storesearch: {len(items)} result(s) -> {how}")
            if chosen is not None:
                return Match(
                    name=name,
                    steam_appid=_as_int(chosen.get("id")),
                    sgdb_id=None,
                    matched_name=str(chosen.get("name") or ""),
                    how=f"steamstore:{how}",
                    explain=explain,
                )

        if transient:
            explain.append("no match (a lookup failed; not cached)")
            return Match(name=name, how="none", explain=explain, transient=True)
        explain.append("no match")
        return Match(name=name, how="none", explain=explain)

    def _steam_appid_for(self, sgdb_id: int, explain: list[str]) -> int | None:
        if not self._sgdb_usable():
            return None
        assert self.sgdb is not None
        try:
            game = self.sgdb.game(sgdb_id)
        except HardStop:
            raise
        except HttpError as exc:
            explain.append(f"sgdb games/id/{sgdb_id} failed: {exc}")
            return None
        steam_appid = steam_appid_from_game(game)
        explain.append(
            f"sgdb games/id/{sgdb_id}?platformdata=steam -> "
            f"{steam_appid if steam_appid is not None else 'no steam release'}"
        )
        return steam_appid
