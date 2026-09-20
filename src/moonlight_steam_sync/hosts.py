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
and :func:`resolve_host` from here instead, so the "one function,
evaluated at call time" contract in spec 3.4.8 holds without a cycle, and
the flag -> state file -> config precedence is written down once); the
list cache reuses :func:`moonlight_steam_sync.art.resolve.cache_dir` for its
root, as spec 3.4.8/3.12 direct. :mod:`moonlight_steam_sync.reporting`
imports nothing from the package, so ``cmd_host`` shares its ``Reporter``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from moonlight_steam_sync.art.resolve import cache_dir
from moonlight_steam_sync.reporting import Reporter

_SLUG_BAD = re.compile(r"[^a-z0-9._-]")


def slug(name: str) -> str:
    """Lowercase *name*, anything outside ``[a-z0-9._-]`` -> ``_`` (spec 3.12).

    Not injective: ``My Host`` and ``My_Host`` share ``my_host.json``.
    :func:`read_host_cache` therefore checks the name stored *in* the file,
    so a collision costs the two hosts their cache (each overwrites the
    other's) but never serves one host the other's app list.
    """
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
    cache = HostCache.from_json(data)
    # Same slug, different host (see slug()): not this host's list. Case is
    # folded on purpose -- host names are case-insensitive, and the slug
    # already treats MY-GAMING-PC and my-gaming-pc as one host.
    if cache is not None and cache.host.casefold() != host.casefold():
        return None
    return cache


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


# ---------------------------------------------------------------------------
# `host show|set|clear` (spec 3.4/3.4.8/3.12)
# ---------------------------------------------------------------------------

#: "flag"/"state"/"config" (spec 3.4.6/3.4.8) -> the human labels doctor and
#: `host` use.
_SOURCE_LABEL = {"flag": "--host", "state": "state file", "config": "config"}

# The exact 15-column label padding spec 3.4.7's examples use.
_ACTIVE_HOST_LABEL = "active host:   "
_CACHED_HOSTS_LABEL = "cached hosts:  "


def _cached_hosts_text(infos: list[CachedHostInfo]) -> str:
    if not infos:
        return "none"
    return ", ".join(f"{info.name} ({info.count} apps, {info.when})" for info in infos)


def format_active_host_line(host: str, source: str) -> str:
    """``active host:   NAME (state file)`` / ``(none set)`` (spec 3.4.7/3.4.8)."""
    if not host:
        return f"{_ACTIVE_HOST_LABEL}(none set)"
    return f"{_ACTIVE_HOST_LABEL}{host} ({_SOURCE_LABEL.get(source, source)})"


def format_cached_hosts_line(infos: list[CachedHostInfo]) -> str:
    return f"{_CACHED_HOSTS_LABEL}{_cached_hosts_text(infos)}"


def resolve_host(host_flag: str, state_file: Path, config_host: str) -> tuple[str, str]:
    """``--host`` flag -> state file -> ``config.toml`` (spec 3.4.8/3.12)."""
    if host_flag:
        return host_flag, "flag"
    state_host = read_active_host(state_file)
    if state_host:
        return state_host, "state"
    return config_host, "config"


def cmd_host(
    args: Any,
    *,
    config_path: Path,
    config_host: str,
    state_file: Path | None = None,
    hosts_directory: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    json_mode: bool = False,
    version: str = "",
) -> int:
    """``moonlight-steam-sync host show|set|clear`` (spec 3.4, 3.4.8, 3.12).

    Takes the config-file ``host`` value and the config path as plain
    arguments rather than a :class:`~moonlight_steam_sync.config.Config`, so
    this module keeps importing nothing from ``config`` or ``sync`` (module
    docstring).
    """
    out = out or sys.stdout
    err = err or sys.stderr
    state_file = state_file if state_file is not None else active_host_path()
    hosts_directory = hosts_directory if hosts_directory is not None else hosts_dir()

    reporter = Reporter(out, err, json=json_mode, command="host", version=version)
    reporter.start()

    def report_state(host: str, source: str) -> None:
        infos = list_cached_hosts(hosts_directory)
        reporter.line(format_active_host_line(host, source))
        reporter.line(format_cached_hosts_line(infos))
        reporter.event(
            "host",
            name=host or None,
            source=source,
            cached_hosts=[
                {"name": info.name, "when": info.when, "count": info.count} for info in infos
            ],
        )

    action = getattr(args, "host_action", None)
    if action == "set":
        name = str(args.name)
        write_active_host(name, state_file)
        report_state(name, "state")
        return 0
    if action == "clear":
        clear_active_host(state_file)
        host, source = resolve_host("", state_file, config_host)
        report_state(host, source)
        return 0

    # "show" (default)
    host_flag = str(getattr(args, "host", "") or "")
    host, source = resolve_host(host_flag, state_file, config_host)
    if not host:
        message = (
            "host: no host configured; pass --host, run `host set NAME`, "
            f"or set `host` in {config_path}"
        )
        # "no host configured" is contractual (spec 3.4.6, the `host`
        # bullet): the Decky plugin reads exit 1 plus that text as its
        # first-run state. tests/test_hosts.py pins it.
        reporter.error(message, 1)
        return 1
    report_state(host, source)
    return 0


__all__ = [
    "CachedHostInfo",
    "HostCache",
    "active_host_path",
    "clear_active_host",
    "cmd_host",
    "format_active_host_line",
    "format_cached_hosts_line",
    "host_cache_timestamp",
    "hosts_dir",
    "list_cached_hosts",
    "read_active_host",
    "read_host_cache",
    "resolve_host",
    "slug",
    "state_dir",
    "write_active_host",
    "write_host_cache",
]
