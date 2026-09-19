"""The active-host state file and the per-host `moonlight list` cache (spec 3.12).

Two independent bits of durable state, both scoped to "which host", neither
of them `config.toml`:

* the **active-host state file**
  (``$XDG_STATE_HOME/moonlight-steam-sync/active-host``, one line) --
  written only by ``host set``, read by :func:`config.load_config` between
  the ``--host`` flag and the config file's ``host`` key;
* the **per-host list cache**
  (``<cache dir>/hosts/<slug>.json``) -- a ``{"host", "when", "apps"}``
  snapshot of the last successful ``moonlight list <host>``, refreshed by
  ``sync``, ``list`` and ``ignore --all`` and read (never refreshed) by
  ``list --cached`` and ``status``.

This module imports nothing from :mod:`moonlight_steam_sync.config` or
:mod:`moonlight_steam_sync.sync` (:mod:`config` imports :func:`active_host_path`
and :func:`read_active_host` from here instead, so the "one function,
evaluated at call time" contract in spec 3.4.8 holds without a cycle); the
list cache reuses :func:`moonlight_steam_sync.art.resolve.cache_dir` for its
root, as spec 3.4.8/3.12 direct.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moonlight_steam_sync.art.resolve import cache_dir

_SLUG_BAD = re.compile(r"[^a-z0-9._-]")


def slug(name: str) -> str:
    """Lowercase *name*, anything outside ``[a-z0-9._-]`` -> ``_`` (spec 3.12)."""
    return _SLUG_BAD.sub("_", name.casefold())


# ---------------------------------------------------------------------------
# active host state file
# ---------------------------------------------------------------------------


def state_dir() -> Path:
    """``$XDG_STATE_HOME/moonlight-steam-sync`` (``~/.local/state`` by default)."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "moonlight-steam-sync"


def active_host_path() -> Path:
    """A function, not a module constant, so ``$XDG_STATE_HOME`` overrides
    made after import (tests) are honoured (spec 3.4.8)."""
    return state_dir() / "active-host"


def read_active_host(path: Path | None = None) -> str:
    """The active host, or ``""`` when unset. Whitespace-only counts as unset."""
    path = path or active_host_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.strip()


def write_active_host(name: str, path: Path | None = None) -> None:
    """Write *name* atomically; creates the state directory if needed."""
    path = path or active_host_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{name}\n", encoding="utf-8")
    os.replace(tmp, path)


def clear_active_host(path: Path | None = None) -> None:
    """Remove the state file; a no-op when it does not exist."""
    path = path or active_host_path()
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


# ---------------------------------------------------------------------------
# per-host list cache
# ---------------------------------------------------------------------------


def hosts_dir() -> Path:
    """``<cache dir>/hosts``, a sibling of ``matches.json`` (spec 3.4.8)."""
    return cache_dir() / "hosts"


def _now_iso() -> str:
    """``YYYY-MM-DDTHH:MM:SSZ``, the same form ``matches.json`` uses."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class HostCache:
    """One host's last successful ``moonlight list`` (spec 3.12)."""

    host: str
    when: str
    apps: list[str]

    def to_json(self) -> dict[str, Any]:
        return {"host": self.host, "when": self.when, "apps": list(self.apps)}

    @classmethod
    def from_json(cls, data: Any) -> HostCache | None:
        if not isinstance(data, dict):
            return None
        apps = data.get("apps")
        if not isinstance(apps, list):
            return None
        host = data.get("host")
        if not isinstance(host, str) or not host:
            return None
        return cls(host=host, when=str(data.get("when") or ""), apps=[str(a) for a in apps])

    def write(self, directory: Path) -> Path:
        """Atomically write ``<directory>/<slug(host)>.json``; returns the path."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{slug(self.host)}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path


def read_host_cache(directory: Path, host: str) -> HostCache | None:
    path = directory / f"{slug(host)}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return HostCache.from_json(data)


def host_cache_timestamp(directory: Path, host: str) -> float | None:
    """The cache file's mtime, for ``cached_when`` display purposes."""
    path = directory / f"{slug(host)}.json"
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def write_host_cache(directory: Path, host: str, apps: list[str]) -> Path:
    """Record a successful ``moonlight list`` (spec 3.4.6/3.12)."""
    return HostCache(host=host, when=_now_iso(), apps=list(apps)).write(directory)


@dataclass
class CachedHostInfo:
    """One line of ``doctor``'s ``cached hosts:`` (spec 3.4.7) or the
    ``host`` event's ``cached_hosts`` list (spec 3.4.6)."""

    name: str
    when: str
    count: int


def list_cached_hosts(directory: Path) -> list[CachedHostInfo]:
    """Every cached host, sorted by name, or ``[]`` when the directory is absent."""
    if not directory.is_dir():
        return []
    infos: list[CachedHostInfo] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cache = HostCache.from_json(data)
        if cache is not None:
            infos.append(CachedHostInfo(name=cache.host, when=cache.when, count=len(cache.apps)))
    infos.sort(key=lambda info: info.name.casefold())
    return infos


__all__ = [
    "CachedHostInfo",
    "HostCache",
    "active_host_path",
    "clear_active_host",
    "host_cache_timestamp",
    "hosts_dir",
    "list_cached_hosts",
    "read_active_host",
    "read_host_cache",
    "slug",
    "state_dir",
    "write_active_host",
    "write_host_cache",
]
