"""The orchestration: list -> diff -> art -> write -> restart (spec 3.6, 3.7, 3.9).

This module is the only place that rewrites a real Steam library and stops
and starts Steam, so its shape follows the failure orderings rather than
the happy path:

* **The existing library is the source of truth** (spec 3.7). ``sync`` adds
  ``(host list - ignore) - owned shortcuts``; a shortcut is "owned" when its
  unquoted ``Exe`` is the configured ``exe`` (realpath compared, spec 3.3),
  which is how the SteamTinkerLaunch-era entries are adopted without a
  state file. Nothing is ever matched by name.
* **Art first, one write, one restart** (spec 3.6). The art phase runs with
  Steam up (grid files are inert until a restart) and records every step
  on disk as it goes; the single non-incremental step -- writing
  ``shortcuts.vdf`` -- comes last and is skipped entirely when the art
  phase stopped early, so an interrupted run costs no finished work and
  leaves the library exactly as it was (spec 3.9 items 1 and 4).
* **Steam is never written under** (spec 2.1). :func:`commit_shortcuts` is
  the one path from an in-memory :class:`ShortcutsFile` to disk, in one
  of three modes (``--commit``, decky spec 3.5): it checks whether Steam
  is running and then refuses (exit 2), or restarts it --
  ``steam -shutdown`` -> wait -> write -> ``steam -silent``, with
  ``SIGINT`` deferred across that window so Ctrl-C can never leave Steam
  down with the file unwritten -- or, for the Decky plugin, ``await-exit``:
  waits for something else to take the client down, writes the instant it
  is gone, and never starts it.
* **Progress lives on disk, never only in memory** (spec 3.9). A slot is
  done when its grid file exists, a title when it is in ``matches.json``,
  a shortcut when it is in ``shortcuts.vdf``. Re-running after any failure
  does only the remaining work; the e2e tests in ``tests/test_sync_e2e.py``
  prove it against a 500-title library.

``list``, ``ignore`` and ``remove`` share the same plan and the same
write path, so they live here too.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import signal
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from moonlight_steam_sync import hosts, moonlight, steam
from moonlight_steam_sync import version as _pkg_version
from moonlight_steam_sync.art import resolve as art_resolve
from moonlight_steam_sync.art.apply import (
    ArtTarget,
    CommitResult,
    RunSummary,
    SteamShortcutProvider,
    patch_icon,
    run_art,
)
from moonlight_steam_sync.art.cli import ArtServices, build_services
from moonlight_steam_sync.art.http import HardStop
from moonlight_steam_sync.art.resolve import Match, MatchCache, Resolver, default_cache_path
from moonlight_steam_sync.art.select import SLOTS, Selector, existing_slot_file
from moonlight_steam_sync.config import (
    Config,
    ConfigError,
    default_exe,
    load_ignore_file,
    load_owned_apps,
    owned_apps_steamid3,
)
from moonlight_steam_sync.reporting import (
    KIND_CLIENT,
    KIND_DUPLICATE,
    KIND_HOST_APP,
    KIND_IGNORED,
    KIND_PARKED,
    KIND_SHORTCUT,
    KIND_STREAM,
    STREAM_HOWS,
    Reporter,
    is_default_host_app,
    is_stream_match,
    match_json,
    slot_json_value,
)
from moonlight_steam_sync.shortcuts import (
    CLIENT_APP_NAME,
    CLIENT_LAUNCH_OPTIONS,
    Shortcut,
    ShortcutsError,
    ShortcutsFile,
    app_name_for,
    render_launch_options,
)
from moonlight_steam_sync.steam import ProcessRunner, SteamRunningError

# Copied rather than imported from ``__main__`` (which imports this module).
EXIT_OK = 0
EXIT_USAGE_OR_CONFIG = 1
EXIT_STEAM_RUNNING = 2
EXIT_MOONLIGHT_UNREACHABLE = 3
EXIT_NETWORK_STOPPED = 4
EXIT_SIGINT = 130

RESUME_HINT = "interrupted; resume with the same command"

#: Labels ``list`` prints in front of each host app.
LABEL_ADDED = "added"
LABEL_IGNORED = "ignored"
LABEL_NEW = "new"
LABEL_PARKED = "parked"


# ---------------------------------------------------------------------------
# dependencies (the seams the tests replace)
# ---------------------------------------------------------------------------


ListApps = Callable[[str], list[moonlight.App]]


@dataclass
class Deps:
    """Everything that touches the outside world, in one injectable bundle.

    ``list_apps`` runs the ``moonlight`` CLI, ``runner`` is the subprocess
    seam for Steam, ``services`` is the artwork stack (built from the config
    when ``None``; tests hand in one wired to a fake transport) and
    ``cache_path`` overrides where ``matches.json`` lives.
    """

    list_apps: ListApps = moonlight.list_apps
    runner: ProcessRunner = field(default_factory=ProcessRunner)
    services: ArtServices | None = None
    cache_path: Path | None = None
    #: Overrides where the per-host list cache lives (spec 3.4.8); follows
    #: ``cache_path`` when unset, so ``World.run`` (which sets ``cache_path``
    #: but never ``$XDG_CACHE_HOME``) never touches a developer's real cache.
    hosts_dir: Path | None = None

    def resolved_hosts_dir(self) -> Path:
        if self.hosts_dir is not None:
            return self.hosts_dir
        base = self.cache_path.parent if self.cache_path is not None else art_resolve.cache_dir()
        return base / "hosts"


# ---------------------------------------------------------------------------
# the library: one Steam user's shortcuts.vdf, read once per run
# ---------------------------------------------------------------------------


@dataclass
class Library:
    """The picked Steam user and their parsed shortcut store."""

    user: steam.SteamUser
    file: ShortcutsFile

    @property
    def grid_dir(self) -> Path:
        return self.user.grid_dir


def open_library() -> Library:
    """Find Steam, pick the user, parse ``shortcuts.vdf``.

    A parse error aborts here, before anything is touched (spec 3.6); a
    missing or empty file is a valid, empty library (fresh Steam user).
    Raises :class:`steam.SteamError` or :class:`ShortcutsError`.
    """
    user = steam.pick_user(steam.find_steam_root())
    return Library(user=user, file=ShortcutsFile.read(user.shortcuts_path))


def _open_library_or_report(command: str, reporter: Reporter) -> Library | None:
    try:
        return open_library()
    except steam.SteamError as exc:
        reporter.error(f"{command}: {exc}", EXIT_USAGE_OR_CONFIG)
    except ShortcutsError as exc:
        reporter.error(f"{command}: {exc}", EXIT_USAGE_OR_CONFIG)
        print(f"{command}: nothing was touched", file=reporter.err)
    return None


def _list_host_or_report(
    command: str, config: Config, deps: Deps, reporter: Reporter
) -> list[moonlight.App] | None:
    """Run ``moonlight list``, and on success refresh the per-host cache
    (spec 3.4.6/3.12) -- every caller (``sync``, ``list``, ``ignore --all``)
    shares this one helper, so all three refresh it with no new flag."""
    try:
        apps = deps.list_apps(config.host)
    except (
        moonlight.MoonlightNotFoundError,
        moonlight.MoonlightUnreachableError,
    ) as exc:
        reporter.error(f"{command}: {exc}", EXIT_MOONLIGHT_UNREACHABLE)
        return None
    try:
        hosts.write_host_cache(deps.resolved_hosts_dir(), config.host, [a.name for a in apps])
    except OSError as exc:
        # Best-effort (spec 3.11/3.12): an unwritable cache dir must not turn
        # an otherwise-successful list into a failed command.
        # v0.2.0 never printed anything for this (spec 3.11): the cache
        # write is new in this PR, so its failure note is --json-only,
        # like the other notes new in this PR.
        reporter.note(f"could not write host list cache: {exc}", only_json=True)
    return apps


def _ignore_file_path(args: argparse.Namespace, config: Config) -> Path | None:
    flag = getattr(args, "ignore_file", None)
    if flag:
        return Path(flag)
    return config.ignore_file_path


def _load_ignore_extra_or_report(
    command: str, args: argparse.Namespace, config: Config, reporter: Reporter
) -> list[str] | None:
    """The ``--ignore-file`` names (decky spec 3.4.3), or ``None`` after
    reporting exit 1 for a missing or invalid file -- called before the
    library is opened or Moonlight is asked anything (spec 3.4.8)."""
    path = _ignore_file_path(args, config)
    if path is None:
        return []
    try:
        return load_ignore_file(path)
    except ConfigError as exc:
        reporter.error(f"{command}: {exc}", EXIT_USAGE_OR_CONFIG)
        return None


def _owned_apps_path(args: argparse.Namespace, config: Config) -> Path | None:
    flag = getattr(args, "owned_apps", None)
    if flag:
        return Path(flag)
    return config.owned_apps_path


def _load_owned_or_report(
    command: str, args: argparse.Namespace, config: Config, reporter: Reporter
) -> tuple[bool, dict[int, str] | None]:
    """``(ok, owned)``: the ``--owned-apps`` map (decky spec 3.4.1), ``None``
    without the flag, or ``(False, None)`` after reporting exit 1 for a
    missing or invalid file -- before the library is opened or Moonlight is
    asked anything (spec 3.4.8). A ``launch_options`` template without
    ``{name}`` is refused too: a hidden entry's ``AppName`` is the owned
    game's, so only the launch options can carry the Moonlight name back."""
    path = _owned_apps_path(args, config)
    if path is None:
        return True, None
    try:
        owned = load_owned_apps(path)
    except ConfigError as exc:
        reporter.error(f"{command}: {exc}", EXIT_USAGE_OR_CONFIG)
        return False, None
    if "{name}" not in config.launch_options:
        reporter.error(
            f"{command}: --owned-apps needs a launch_options template with {{name}} in it "
            f"(got {config.launch_options!r}); a hidden entry's name is the owned game's, "
            "so the launch options are the only way back to the Moonlight name",
            EXIT_USAGE_OR_CONFIG,
        )
        return False, None
    return True, owned


def _check_owned_user(
    command: str, args: argparse.Namespace, config: Config, library: Library, reporter: Reporter
) -> bool:
    """The steamid3 check (decky spec 3.4.1): a stale owned-apps file from
    another account can never hide shortcuts on the wrong one."""
    path = _owned_apps_path(args, config)
    if path is None:
        return True
    wanted = owned_apps_steamid3(path)
    if wanted is None or wanted == library.user.steamid3:
        return True
    reporter.error(
        f"{command}: owned-apps file is for Steam user {wanted} but sync would write to "
        f"user {library.user.steamid3}",
        EXIT_USAGE_OR_CONFIG,
    )
    return False


def _need_host(command: str, config: Config, reporter: Reporter) -> bool:
    if config.host:
        return True
    reporter.error(
        f"{command}: no host configured; pass --host or set `host` in {config.config_path}",
        EXIT_USAGE_OR_CONFIG,
    )
    return False


# ---------------------------------------------------------------------------
# the plan: (host list - ignore) - owned shortcuts (spec 3.7), with kinds
# (decky spec 3.2), replacements (3.4.2), duplicates (3.3), parking (3.12)
# and the client entry (3.4.5)
# ---------------------------------------------------------------------------

# The title kinds (``KIND_*``) and ``STREAM_HOWS`` live in
# :mod:`moonlight_steam_sync.reporting`, which ``art/cli`` (imported by this
# module) shares; they are re-exported here for callers of the plan.

#: ``Replacement.reason`` values (decky spec 3.4.2).
REASON_KIND_CHANGED = "kind-changed"
REASON_NAME_CHANGED = "name-changed"
REASON_REMATCHED = "rematched"


@dataclass
class Adopted:
    """An owned entry already in the library, and its host app if still published."""

    shortcut: Shortcut
    name: str
    app: moonlight.App | None = None
    #: ``stream`` / ``shortcut`` for a published name, ``parked`` for an
    #: entry that is (or will be, with ``--park-unpublished``) hidden because
    #: the active host does not publish it, ``client`` for the Moonlight
    #: client entry (decky spec 3.2, 3.4.5, 3.12).
    kind: str = KIND_SHORTCUT


@dataclass
class Addition:
    """A host app with no shortcut yet, and the entry that would be written for it."""

    app: moonlight.App
    shortcut: Shortcut


@dataclass
class Replacement:
    """An owned entry whose ``AppName`` (and so appid) must change (decky spec 3.4.2).

    A rename is never an in-place edit -- the appid is derived from the
    name, so the grid files would silently stop matching -- but a remove +
    append whose grid files are *renamed* to the new appid before the art
    phase, so a kind flip costs zero downloads. ``rematched`` (the title's
    ``stale_art`` flag, decky spec 3.4.4) deletes them instead: the art on
    disk belongs to the old match.
    """

    old: Shortcut
    new: Shortcut
    reason: str
    #: The Moonlight name, for progress lines and the ``replace`` event.
    name: str = ""


@dataclass
class Plan:
    """What one run would do, before it does any of it."""

    host: str
    apps: list[moonlight.App]
    ignored: list[moonlight.App] = field(default_factory=list)
    #: Host apps that already have an owned shortcut (by name or by appid)
    #: that needs no replacement.
    present: list[moonlight.App] = field(default_factory=list)
    #: Every owned entry in the library that stays (published or not), plus
    #: the client entry; the old half of a replacement and a duplicate's
    #: entry are not in here.
    adopted: list[Adopted] = field(default_factory=list)
    to_add: list[Addition] = field(default_factory=list)
    #: New apps and replacements beyond ``--limit``, in host-list order, for
    #: the next run (``--limit`` counts both together, decky spec 6.13).
    pending: list[Addition | Replacement] = field(default_factory=list)
    limit: int | None = None
    #: Moonlight name -> ``stream`` / ``shortcut`` / ``duplicate`` /
    #: ``host-app`` for every published, non-ignored name (decky spec 3.4.2,
    #: 3.14).
    kinds: dict[str, str] = field(default_factory=dict)
    to_replace: list[Replacement] = field(default_factory=list)
    #: Losing Moonlight name -> winning Moonlight name (decky spec 3.3).
    duplicates: dict[str, str] = field(default_factory=dict)
    #: Owned entries of duplicate titles, removed with their grid files.
    to_remove: list[Shortcut] = field(default_factory=list)
    #: In-place ``IsHidden`` flips (decky spec 3.11, 3.12): entries to hide
    #: and entries to show again. The appid does not change, so no
    #: replacement, no grid rename, no downloads.
    to_park: list[Shortcut] = field(default_factory=list)
    to_unpark: list[Shortcut] = field(default_factory=list)
    #: Published ``stream`` entries that are visible right now (someone
    #: unhid them) and, under ``--hide-host-apps``, visible ``host-app``
    #: entries (decky spec 3.14): hidden in place by their kind. Not parking --
    #: parking is for names the active host does not publish (decky spec
    #: 3.12) -- so reported apart from ``to_park`` and never in the
    #: ``plan`` event's park counts.
    to_hide: list[Shortcut] = field(default_factory=list)
    #: Published names whose owned entry is hidden but of kind ``shortcut``
    #: -- what ``list`` labels ``parked`` (decky spec 3.12); only computed
    #: when ``--owned-apps`` says what the kinds are.
    parked_names: set[str] = field(default_factory=set)
    #: Published names that have an owned entry of any kind right now.
    existing_names: set[str] = field(default_factory=set)
    #: The Moonlight client entry (decky spec 3.4.5) already in the library,
    #: and the one to write when ``--client-shortcut`` asks for it.
    client: Shortcut | None = None
    client_to_add: Shortcut | None = None
    #: Published names whose match carries ``stale_art`` and that get an
    #: entry *this run* (a ``rematched`` replacement, or a fresh addition
    #: whose old grid files may still be on disk): ``sync`` deletes their
    #: art before the art phase and clears the flag once it has run (decky
    #: spec 3.4.4). A stale title held back by ``--limit`` is not in here,
    #: because its old entry -- and its old art -- stay this run.
    stale: set[str] = field(default_factory=set)
    #: ``--hide-host-apps`` was passed (decky spec 3.14): the only time the
    #: ``plan`` / ``summary`` events carry their ``host-app`` counts, so
    #: every other run's JSON is v0.3.1's, key for key.
    hide_host_apps: bool = False

    def label(self, app: moonlight.App) -> str:
        if app in self.ignored or app.name in self.duplicates:
            # A duplicate gets no tile (decky spec 3.3): for the human list
            # that is "ignored", whatever entry it holds right now; the
            # `app` event's `kind` / `duplicate_of` say why.
            return LABEL_IGNORED
        if app.name in self.parked_names:
            return LABEL_PARKED
        if app in self.present or app.name in self.existing_names:
            return LABEL_ADDED
        return LABEL_NEW

    def kind(self, app: moonlight.App) -> str:
        """The kind ``list`` reports for a host app (decky spec 3.4.6)."""
        if app in self.ignored:
            return KIND_IGNORED
        if app.name in self.parked_names:
            return KIND_PARKED
        return self.kinds.get(app.name, KIND_SHORTCUT)

    @property
    def stream_count(self) -> int:
        return sum(1 for kind in self.kinds.values() if kind == KIND_STREAM)

    @property
    def shortcut_count(self) -> int:
        return sum(1 for kind in self.kinds.values() if kind == KIND_SHORTCUT)

    @property
    def host_app_count(self) -> int:
        return sum(1 for kind in self.kinds.values() if kind == KIND_HOST_APP)

    @property
    def parked_count(self) -> int:
        """Parked entries after the run (decky spec 3.4.6)."""
        return sum(1 for item in self.adopted if item.kind == KIND_PARKED)

    @property
    def is_empty(self) -> bool:
        return not (
            self.to_add
            or self.adopted
            or self.to_replace
            or self.to_remove
            or self.to_park
            or self.to_unpark
            or self.to_hide
            or self.client_to_add
        )

    def header(self) -> str:
        line = (
            f"{self.host}: {len(self.apps)} app(s) published, {len(self.ignored)} ignored, "
            f"{len(self.present)} already in Steam, {len(self.to_add)} to add"
        )
        if self.to_replace:
            line += f", {len(self.to_replace)} to replace"
        if self.to_remove:
            line += f", {len(self.to_remove)} to remove"
        if self.to_park:
            line += f", {len(self.to_park)} to park"
        if self.to_unpark:
            line += f", {len(self.to_unpark)} to unpark"
        if self.to_hide:
            line += f", {len(self.to_hide)} to hide"
        if self.pending:
            line += f", {len(self.pending)} pending (--limit {self.limit})"
        return line


def build_plan(
    config: Config,
    apps: Sequence[moonlight.App],
    shortcuts_file: ShortcutsFile,
    *,
    limit: int | None = None,
    ignore_extra: Sequence[str] = (),
    matches: Mapping[str, Match | None] | None = None,
    owned: Mapping[int, str] | None = None,
    park_unpublished: bool = False,
    client_shortcut: bool = False,
    hide_host_apps: bool = False,
) -> Plan:
    """Diff the host list against the library (spec 3.7, decky spec 3.4.2).

    Ignore is matched on the Moonlight app name, exact (spec 3.2), against
    ``config.ignore`` plus ``ignore_extra`` -- the ``--ignore-file`` names
    (decky spec 3.4.3), a union. Ownership is by ``Exe`` (spec 3.3); the
    name behind an owned entry is recovered from its launch options. A host
    app whose would-be appid is already in the file is "the same shortcut"
    (spec 3.6) even if the name recovery disagrees. Duplicate names in the
    host list count once.

    ``matches`` (Moonlight name -> the title's :class:`Match`, or ``None``)
    and ``owned`` (Steam appid -> display name, from ``--owned-apps``)
    decide each published name's *kind* (decky spec 3.2): a title whose
    match is a Steam game in ``owned`` -- by an exact or pinned match, never
    a fuzzy one -- is ``stream`` and gets a hidden entry named after the
    owned game (3.3); the first such title per owned game wins and later
    ones are ``duplicate`` (no entry; an existing one is removed with its
    art); everything else is ``shortcut``. A title whose match carries
    ``stale_art`` (``match --defer-art``, 3.4.4) becomes a ``rematched``
    replacement whatever its name and whatever its ``how`` (the ``unpinned``
    placeholder included), so its art is re-fetched -- with or without
    ``owned``: ``sync --owned-apps`` resolves ``matches`` ahead of the plan
    (3.4.2), a plain ``sync`` and ``list --owned-apps`` read them from the
    cache. Without ``owned`` every kind is ``shortcut`` and, with no flagged
    entry, the plan is v0.2.0's.

    Per published name, an existing entry whose ``AppName`` is not the one
    its kind wants is a :class:`Replacement` (``kind-changed`` /
    ``name-changed``); one that only differs in ``IsHidden`` is flipped in
    place (:attr:`Plan.to_hide` for a visible ``stream`` entry,
    :attr:`Plan.to_unpark` for a hidden ``shortcut`` one, decky spec 3.11)
    -- both only once ``owned`` is given, since v0.2.0 adopted an entry
    under any name. ``--limit`` counts additions and replacements together,
    in host-list order, and never splits a replacement or counts a flip.

    With ``park_unpublished`` (decky spec 3.12) every owned entry the host
    does not publish is hidden in place, and a hidden ``shortcut``-kind
    entry the host does publish is shown again. With ``client_shortcut``
    the Moonlight client entry (3.4.5) is added when missing; it is always
    listed under ``adopted`` (kind ``client``) when present, whoever
    ``config.exe`` is.

    With ``hide_host_apps`` (decky spec 3.14) a published name that is one
    of the host's two default apps is kind ``host-app`` -- decided on the
    name alone and *before* ``stream``, so it never takes an owned game and
    is never a duplicate. Its entry is a ``shortcut`` entry with
    ``IsHidden = 1``: same ``AppName``, same appid, so an existing visible
    one is hidden in place (:attr:`Plan.to_hide`), never replaced.
    """
    tool_exe = default_exe()
    client = shortcuts_file.client_entry(tool_exe)
    owned_entries = [s for s in shortcuts_file.owned(config.exe) if s is not client]
    owned_by_name: dict[str, Shortcut] = {}
    names_of: dict[int, str] = {}
    for entry in owned_entries:
        name = entry.moonlight_name(config.launch_options, config.name_suffix)
        names_of[id(entry)] = name
        owned_by_name.setdefault(name, entry)

    matches = matches or {}
    plan = Plan(
        host=config.host,
        apps=list(apps),
        limit=limit,
        client=client,
        hide_host_apps=hide_host_apps,
    )
    if client_shortcut and client is None:
        plan.client_to_add = client_shortcut_entry()
    ignore = set(config.ignore) | set(ignore_extra)
    seen: set[str] = set()
    apps_by_name: dict[str, moonlight.App] = {}
    changes: list[Addition | Replacement] = []
    #: Owned Steam appid -> (winning Moonlight name, its hidden entry's appid).
    taken: dict[int, tuple[str, int]] = {}
    replaced_or_removed: set[int] = set()
    stale_names: set[str] = set()
    naming_active = owned is not None
    # v0.2.0 adopted an owned entry under any name and never touched
    # IsHidden; both only change once a flag says what the kinds are.
    flips_active = naming_active or park_unpublished or hide_host_apps
    for app in apps:
        name = app.name
        if name in seen:
            continue
        seen.add(name)
        apps_by_name[name] = app
        if name in ignore:
            plan.ignored.append(app)
            continue

        match = matches.get(name)
        stale = bool(match is not None and match.stale_art)
        kind, owned_name = KIND_SHORTCUT, None
        if hide_host_apps and is_default_host_app(name):
            kind = KIND_HOST_APP
        elif is_stream_match(match, owned):
            assert owned is not None and match is not None and match.steam_appid is not None
            kind, owned_name = KIND_STREAM, owned[match.steam_appid]
        entry = owned_by_name.get(name)
        if entry is not None:
            plan.existing_names.add(name)
        if kind == KIND_STREAM:
            assert match is not None and match.steam_appid is not None
            winner = taken.get(match.steam_appid)
            if winner is not None:
                # Decky spec 3.3 / decision 14: the first title in host-list
                # order gets the hidden entry; a later one is almost always
                # a wrong match, so it gets no tile at all. An entry whose
                # appid *is* the winner's hidden entry (the host order
                # changed since it was written) belongs to the winner now
                # and is left alone; anything else this name owns goes.
                winner_name, winner_appid = winner
                plan.kinds[name] = KIND_DUPLICATE
                plan.duplicates[name] = winner_name
                if entry is not None and entry.appid != winner_appid:
                    plan.to_remove.append(entry)
                    replaced_or_removed.add(id(entry))
                continue
        plan.kinds[name] = kind

        desired = new_shortcut(config, name, kind=kind, owned_name=owned_name)
        if kind == KIND_STREAM:
            assert match is not None and match.steam_appid is not None
            taken[match.steam_appid] = (name, desired.appid)
        if entry is None:
            entry = shortcuts_file.by_appid(desired.appid)
            if entry is not None and id(entry) in replaced_or_removed:
                # That appid is on its way out with an earlier title's
                # replacement (a `stream` entry turned `host-app`, decky
                # spec 3.14): this title needs an entry of its own.
                entry = None
            if entry is not None:
                plan.existing_names.add(name)
        if stale:
            stale_names.add(name)
        if entry is None:
            changes.append(Addition(app=app, shortcut=desired))
            continue
        same_name = entry.app_name == desired.app_name
        hidden_now, hidden_wanted = bool(entry.is_hidden), bool(desired.is_hidden)
        if stale or (naming_active and not same_name):
            if stale:
                reason = REASON_REMATCHED
            elif hidden_now != hidden_wanted:
                reason = REASON_KIND_CHANGED
            else:
                reason = REASON_NAME_CHANGED
            changes.append(Replacement(old=entry, new=desired, reason=reason, name=name))
            replaced_or_removed.add(id(entry))
        else:
            plan.present.append(app)
            # Decky spec 3.11: IsHidden is the one field edited in place. A
            # `stream` entry someone unhid is hidden again by its kind
            # (owned-apps knowledge) -- not parked, the host publishes it;
            # a hidden `shortcut` entry is shown again only by
            # --park-unpublished, which is what parked it.
            # A `host-app` entry (3.14) is hidden the same way. Without
            # --owned-apps that is whatever name v0.2.0's adoption found it
            # under; with it, a misnamed entry never gets here -- it was a
            # replacement above, as for a `shortcut` title, because the
            # name it carries may be an owned game's (a former `stream`
            # entry), whose appid the real Stream entry needs.
            if hidden_wanted and not hidden_now and (
                kind == KIND_HOST_APP or (same_name and naming_active)
            ):
                plan.to_hide.append(entry)
            elif same_name and hidden_now and not hidden_wanted and park_unpublished:
                plan.to_unpark.append(entry)
        if hidden_now and kind == KIND_SHORTCUT and flips_active:
            plan.parked_names.add(name)

    if limit is not None and limit >= 0:
        to_do, plan.pending = changes[:limit], changes[limit:]
    else:
        to_do = changes
    for change in to_do:
        if isinstance(change, Addition):
            plan.to_add.append(change)
            if change.app.name in stale_names:
                plan.stale.add(change.app.name)
        else:
            plan.to_replace.append(change)
            if change.name in stale_names:
                plan.stale.add(change.name)
    for change in list(plan.pending):
        if isinstance(change, Replacement):
            # Not this run: the old entry stays exactly as it is.
            replaced_or_removed.discard(id(change.old))
            # ... and so does its appid: an addition that was waiting for
            # it waits with it.
            blocked = [a for a in plan.to_add if a.shortcut.appid == change.old.appid]
            for addition in blocked:
                plan.to_add.remove(addition)
                plan.pending.append(addition)

    unparking = {id(entry) for entry in plan.to_unpark}
    for entry in owned_entries:
        if id(entry) in replaced_or_removed:
            continue
        name = names_of[id(entry)]
        app = apps_by_name.get(name)
        if app is None:
            # Not published by the active host (decky spec 3.12).
            if park_unpublished and not entry.is_hidden:
                plan.to_park.append(entry)
            kind = KIND_PARKED if (park_unpublished or entry.is_hidden) else KIND_SHORTCUT
        elif name in plan.parked_names and id(entry) not in unparking:
            # Published, hidden, and staying hidden this run (no
            # --park-unpublished to unpark it): still a parked entry.
            kind = KIND_PARKED
        else:
            kind = plan.kinds.get(name, KIND_SHORTCUT)
            if kind == KIND_DUPLICATE:
                kind = KIND_SHORTCUT
        plan.adopted.append(Adopted(shortcut=entry, name=name, app=app, kind=kind))
    if client is not None:
        plan.adopted.append(
            Adopted(shortcut=client, name=CLIENT_APP_NAME, app=None, kind=KIND_CLIENT)
        )
    return plan


def new_shortcut(
    config: Config,
    name: str,
    *,
    kind: str = KIND_SHORTCUT,
    owned_name: str | None = None,
) -> Shortcut:
    """The entry ``sync`` writes for a Moonlight app (spec 3.6 write policy).

    A ``stream`` title (decky spec 3.3) gets a *hidden* entry whose
    ``AppName`` is the owned game's display name, exactly as the plugin
    reported it and with no ``name_suffix``: Steam Input keys a shortcut's
    layout by its lowercased name, so a same-named shortcut is offered the
    retail game's layouts for free. The launch options still carry the
    Moonlight name, which is what ownership and the plan recover.

    A ``host-app`` title (decky spec 3.14) gets the ``shortcut`` entry,
    hidden: the same ``AppName`` and so the same appid, which is what keeps
    an existing tile's art and controller layout when it is hidden.
    """
    if kind == KIND_STREAM:
        assert owned_name is not None
        shortcut = Shortcut.create(
            app_name=owned_name,
            exe=config.exe,
            start_dir=config.start_dir,
            launch_options=render_launch_options(config.launch_options, name),
        )
        shortcut.is_hidden = 1
        return shortcut
    shortcut = Shortcut.create(
        app_name=app_name_for(name, config.name_suffix),
        exe=config.exe,
        start_dir=config.start_dir,
        launch_options=render_launch_options(config.launch_options, name),
    )
    if kind == KIND_HOST_APP:
        shortcut.is_hidden = 1
    return shortcut


def client_shortcut_entry() -> Shortcut:
    """The Moonlight client shortcut ``sync --client-shortcut`` writes (decky
    spec 3.4.5): hidden, ``AppName = "Moonlight"``, ``Exe`` the tool's own
    path -- always, even when ``config.exe`` is a user script that would
    not understand ``client`` -- and ``LaunchOptions = "client"``."""
    shortcut = Shortcut.create(
        app_name=CLIENT_APP_NAME,
        exe=default_exe(),
        launch_options=CLIENT_LAUNCH_OPTIONS,
    )
    shortcut.is_hidden = 1
    return shortcut


def resolve_titles(
    names: Sequence[str], resolver: Resolver, *, out: TextIO | None = None
) -> dict[str, Match]:
    """Resolve every title before the plan is built (decky spec 3.4.2).

    Cache-first; each miss makes the same lookups the art phase would, and
    the cache is flushed per title, so an interrupted run resumes. The art
    phase then finds every title cached and makes no lookup of its own. A
    title that cost a lookup gets one progress line on ``out``.
    :class:`~moonlight_steam_sync.art.http.HardStop` and
    ``KeyboardInterrupt`` propagate with everything so far on disk.
    """
    matches: dict[str, Match] = {}
    total = len(names)
    for index, name in enumerate(names, start=1):
        cached = resolver.cache.get(name)
        match = resolver.resolve(name)
        if match.transient and cached is not None and cached.stale_art:
            # A failed lookup wrote nothing, so the flag is still on the
            # cache entry (an ``unpinned`` placeholder, say); the plan reads
            # it from there, whatever the entry's ``how`` (decky spec 3.4.4).
            match.stale_art = True
        matches[name] = match
        if out is not None and match is not cached:
            how = match.how if not match.transient else f"{match.how} (lookup failed, not cached)"
            print(f"resolve [{index}/{total}] {name}: {how}", file=out)
    return matches


def art_targets(plan: Plan, grid_dir: Path) -> list[ArtTarget]:
    """Every adopted entry, every replacement's new entry and every planned
    shortcut, in that order (spec 3.6) -- never the client entry (decky
    spec 3.4.5: no lookups for "Moonlight")."""
    targets = [
        ArtTarget(
            name=item.name,
            appid=item.shortcut.appid,
            grid_dir=grid_dir,
            hidden=bool(item.shortcut.is_hidden),
            app_name=item.shortcut.app_name,
            kind=item.kind,
        )
        for item in plan.adopted
        if item.kind != KIND_CLIENT
    ]
    targets.extend(
        ArtTarget(
            name=item.name,
            appid=item.new.appid,
            grid_dir=grid_dir,
            hidden=bool(item.new.is_hidden),
            app_name=item.new.app_name,
            kind=plan.kinds.get(item.name, KIND_SHORTCUT),
        )
        for item in plan.to_replace
    )
    targets.extend(
        ArtTarget(
            name=item.app.name,
            appid=item.shortcut.appid,
            grid_dir=grid_dir,
            hidden=bool(item.shortcut.is_hidden),
            app_name=item.shortcut.app_name,
            kind=plan.kinds.get(item.app.name, KIND_SHORTCUT),
        )
        for item in plan.to_add
    )
    return targets


def apply_replacement(replacement: Replacement, library: Library) -> None:
    """Swap the entry in memory and move its art on disk (decky spec 3.4.2).

    The vdf change is in memory only until :func:`commit_shortcuts`; the
    grid files are renamed (``os.replace``) to the new appid *now*, before
    the art phase, so a kind flip costs zero downloads. Idempotent for the
    rerun after a crash: a rename whose source is gone and whose target
    exists is done, and a target that already exists is never overwritten
    (the stray source is dropped). A ``rematched`` replacement deletes the
    old files instead, because that art belongs to the old match.
    """
    library.file.replace(replacement.old, replacement.new)
    old_appid, new_appid = replacement.old.appid, replacement.new.appid
    if replacement.reason == REASON_REMATCHED:
        delete_grid_files(library.grid_dir, old_appid)
        if new_appid != old_appid:
            delete_grid_files(library.grid_dir, new_appid)
        return
    if new_appid == old_appid:
        return
    for slot in steam.GRID_SLOTS:
        part = library.grid_dir / f"{steam.grid_stem(old_appid, slot)}.part"
        with contextlib.suppress(OSError):
            part.unlink()
        source = steam.find_grid_file(library.grid_dir, old_appid, slot)
        if source is None:
            continue
        if steam.find_grid_file(library.grid_dir, new_appid, slot) is not None:
            with contextlib.suppress(OSError):
                source.unlink()
            continue
        target = source.with_name(f"{steam.grid_stem(new_appid, slot)}{source.suffix}")
        os.replace(source, target)


def describe_replacement(replacement: Replacement) -> str:
    """The one-line form of a replacement (decky spec 3.4.2):
    ``replace  <name>: <old AppName> [hidden] -> <new AppName> [visible] (kind-changed)``."""

    def visibility(entry: Shortcut) -> str:
        return "hidden" if entry.is_hidden else "visible"

    return (
        f"replace  {replacement.name}: {replacement.old.app_name} "
        f"[{visibility(replacement.old)}] -> {replacement.new.app_name} "
        f"[{visibility(replacement.new)}] ({replacement.reason})"
    )


def delete_grid_files(grid_dir: Path, appid: int) -> int:
    """Delete the five grid files (and any ``.part``) for ``appid``; returns how many went."""
    deleted = 0
    for path in grid_files_for(grid_dir, appid):
        with contextlib.suppress(OSError):
            path.unlink()
            deleted += 1
    return deleted


def patch_icons_from_disk(shortcuts_file: ShortcutsFile, targets: Sequence[ArtTarget]) -> int:
    """Point ``icon`` at an ``_icon`` file that already exists (spec 3.6).

    This is what ``--no-art`` (and a run whose art phase found every slot
    already filled) relies on: the field follows the file on disk, whoever
    put it there. Returns how many entries changed.
    """
    patched = 0
    for target in targets:
        entry = shortcuts_file.by_appid(target.appid)
        if entry is None:
            continue
        existing = steam.find_grid_file(target.grid_dir, target.appid, "icon")
        if existing is not None and patch_icon(entry, existing.resolve()):
            patched += 1
    return patched


class LibraryProvider:
    """The art phase's seam for ``sync``: patches the in-memory file only.

    ``commit`` is deliberately a no-op -- ``sync`` performs the one write
    itself, after the art phase and after Steam is down (spec 3.6).
    """

    def __init__(self, shortcuts_file: ShortcutsFile, targets: Sequence[ArtTarget]) -> None:
        self._file = shortcuts_file
        self._targets = list(targets)
        self.patched = 0

    def targets(self) -> Sequence[ArtTarget]:
        return list(self._targets)

    def set_icon(self, appid: int, icon_path: Path) -> None:
        entry = self._file.by_appid(appid)
        if entry is not None and patch_icon(entry, icon_path):
            self.patched += 1

    def commit(self) -> None:
        return None


# ---------------------------------------------------------------------------
# the write: shutdown -> write -> relaunch (spec 3.6)
# ---------------------------------------------------------------------------


@dataclass
class Commit:
    """What :func:`commit_shortcuts` did."""

    written: bool = False
    restarted: bool = False
    backup: Path | None = None
    #: Steam was shut down and the file written, but ``steam -silent`` failed.
    relaunch_error: str = ""
    #: ``--commit await-exit`` (decky spec 3.5): the file was written after
    #: waiting for a running Steam to exit on its own; nothing relaunched
    #: it (``restarted`` stays false). Not set when Steam was not running
    #: to begin with, when there was nothing to write, or in any other mode.
    awaited_exit: bool = False
    #: The serialised file already matched the one on disk, so there was
    #: nothing to write: either the plan changed nothing, or what it changed
    #: serialised to the same bytes (a ``rematched`` replacement that keeps
    #: its appid and icon path, decky spec 3.4.4). Every refusal raises, so
    #: ``written or unchanged`` -- :attr:`applied` -- is "the in-memory file
    #: is what is on disk now".
    unchanged: bool = False

    @property
    def applied(self) -> bool:
        return self.written or self.unchanged

    def describe(self, shortcuts_path: Path) -> str:
        if not self.written and not self.restarted and not self.relaunch_error:
            return "nothing to write"
        parts = []
        if self.written:
            parts.append(f"wrote {shortcuts_path}")
            if self.backup is not None:
                parts.append(f"backup {self.backup.name}")
        if self.restarted:
            parts.append("Steam restarted")
        if self.relaunch_error:
            parts.append(
                f"Steam was shut down but could not be relaunched ({self.relaunch_error}); "
                "start it by hand"
            )
        return "; ".join(parts)


@contextlib.contextmanager
def sigint_deferred(out: TextIO | None = None) -> Iterator[None]:
    """Ignore ``SIGINT`` for the shutdown -> write -> relaunch window.

    Once Steam has been asked to quit, the only acceptable end states are
    "written and relaunched" or "not written and relaunched" -- never Steam
    down with nothing done. Everything before this window is safe to
    interrupt (spec 3.9 item 4); this window is at most the 30 s shutdown
    wait plus one atomic write. In ``await-exit`` mode this tool never
    takes Steam down, so there is nothing to protect during its wait and
    only the write itself (backup rotation plus ``os.replace``,
    milliseconds) sits inside this window (decky spec 3.13 A2). Off the
    main thread ``signal`` refuses, and that is fine: there is nothing to
    defer there.
    """
    try:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:
        previous = None
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


#: The ``--commit`` modes (decky spec 3.5). :data:`COMMIT_RESTART` is
#: v0.2.0's ``restart_steam = true`` behaviour and :data:`COMMIT_REFUSE`
#: its ``false`` (``--no-restart-steam``); :data:`COMMIT_AWAIT_EXIT` is
#: for the Decky plugin, which takes the client down itself from Game Mode.
COMMIT_RESTART = "restart"
COMMIT_AWAIT_EXIT = "await-exit"
COMMIT_REFUSE = "refuse"
COMMIT_MODES = (COMMIT_RESTART, COMMIT_AWAIT_EXIT, COMMIT_REFUSE)

#: How long ``await-exit`` waits for a running Steam to go before giving up
#: (exit 2, file untouched), and how often it looks (decky spec 3.5: 100 ms,
#: not the 500 ms of the ``restart`` mode's own shutdown wait -- the gap
#: between the client exiting and the session relaunching it is what the
#: write has to land in). Read at call time so a test can shorten them.
AWAIT_EXIT_TIMEOUT_S = 60.0
AWAIT_EXIT_POLL_S = 0.1

#: The exact ``await-exit`` timeout text (decky spec 3.5 step 5); the
#: command prefix is added by the caller like every other error.
AWAIT_EXIT_TIMED_OUT = "steam did not exit; shortcuts.vdf not written"

#: Told the ``await-exit`` timeout, in seconds, the moment the wait starts
#: -- the caller emits ``awaiting-steam-exit`` (spec 3.4.6) from it.
AwaitingHook = Callable[[float], None]


def resolve_commit_mode(mode: str | None, config: Config) -> str:
    """The effective ``--commit`` mode: the flag, else ``config.restart_steam``
    (decky spec 3.4.8: unset reproduces v0.2.0 exactly, ``restart`` forces a
    restart even when the file says ``false``, ``await-exit`` ignores it)."""
    if mode is None:
        return COMMIT_RESTART if config.restart_steam else COMMIT_REFUSE
    if mode not in COMMIT_MODES:
        raise ValueError(f"unknown commit mode {mode!r}")
    return mode


def commit_shortcuts(
    shortcuts_file: ShortcutsFile,
    *,
    config: Config,
    runner: ProcessRunner | None = None,
    out: TextIO | None = None,
    art_written: bool = False,
    mode: str | None = None,
    on_awaiting: AwaitingHook | None = None,
) -> Commit:
    """The one path from an in-memory shortcut store to disk (spec 3.6).

    ``mode`` is the ``--commit`` mode (decky spec 3.5); ``None`` -- every
    call site before that flag existed -- means whatever
    ``config.restart_steam`` says (:func:`resolve_commit_mode`).

    * Nothing to write and no new art -> do nothing at all (no restart).
    * Steam not running -> write; it is not started (the user shut it, and
      the next launch picks the file up). Every mode.
    * Steam running, ``refuse`` -> raise :class:`SteamRunningError` if the
      file changed (exit 2, the caller's art stays on disk); if only art
      changed, do nothing and let the caller say "restart Steam to see it".
    * Steam running, ``restart`` -> ``steam -shutdown``, wait up to 30 s
      (still up afterwards raises :class:`SteamRunningError`), write,
      ``steam -silent``. One restart per run, however big (spec 3.9 item
      8), and ``SIGINT`` is deferred across it.
    * Steam running, ``await-exit`` -> if the file changed, call
      ``on_awaiting`` and poll :func:`steam.is_running` every
      :data:`AWAIT_EXIT_POLL_S` for up to :data:`AWAIT_EXIT_TIMEOUT_S`;
      write the instant it is gone (``SIGINT`` deferred across the write
      only -- the wait itself is interruptible, decky spec 3.13 A2) and
      never relaunch it (``Commit.awaited_exit``). Still up at the
      deadline raises :class:`SteamRunningError` with
      :data:`AWAIT_EXIT_TIMED_OUT`, file untouched. Only art changed ->
      do nothing and return at once, the same as ``refuse``: whether new
      grid files are worth a restart is the caller's (the plugin's) call.
    """
    out = out or sys.stdout
    runner = runner or ProcessRunner()
    requested = mode
    mode = resolve_commit_mode(mode, config)
    changed = shortcuts_file.changed
    if not changed and not art_written:
        return Commit(unchanged=True)

    running = steam.is_running(runner)
    if running and mode == COMMIT_REFUSE:
        if changed and requested == COMMIT_REFUSE:
            raise SteamRunningError(
                "Steam is running and --commit refuse was given, so shortcuts.vdf was not "
                "written (Steam would overwrite it on exit). Artwork already on disk is "
                "kept and picked up on the next restart; quit Steam and rerun, or use "
                "--commit restart / --commit await-exit."
            )
        if changed:
            # v0.2.0's text, word for word: this is the `restart_steam =
            # false` / `--no-restart-steam` path (spec 3.11).
            raise SteamRunningError(
                "Steam is running and restart_steam is false, so shortcuts.vdf was not "
                "written (Steam would overwrite it on exit). Artwork already on disk is "
                "kept and picked up on the next restart; quit Steam and rerun, or drop "
                "--no-restart-steam / set restart_steam = true."
            )
        return Commit(unchanged=True)
    if running and mode == COMMIT_AWAIT_EXIT:
        if not changed:
            return Commit(unchanged=True)
        return _await_exit_and_write(
            shortcuts_file, runner=runner, out=out, on_awaiting=on_awaiting
        )

    result = Commit(unchanged=not changed)
    with sigint_deferred():
        if running:
            print("shutting down Steam (Ctrl-C is deferred until it is back up)", file=out)
            try:
                gone = steam.shutdown(runner)
            except steam.SteamError as exc:
                raise SteamRunningError(
                    f"{exc}; shortcuts.vdf was not written. Quit Steam by hand and rerun "
                    "the same command."
                ) from exc
            if not gone:
                raise SteamRunningError(
                    "Steam did not exit within 30 s after `steam -shutdown`; shortcuts.vdf "
                    "was not written. Quit Steam by hand and rerun the same command."
                )
        if changed:
            before = _backups(shortcuts_file)
            result.written = shortcuts_file.write()
            result.backup = next(iter(sorted(_backups(shortcuts_file) - before)), None)
        if running:
            try:
                steam.relaunch(runner)
            except steam.SteamError as exc:
                # The write is done and safe; only the convenience failed.
                result.relaunch_error = str(exc)
            else:
                result.restarted = True
    return result


def _await_exit_and_write(
    shortcuts_file: ShortcutsFile,
    *,
    runner: ProcessRunner,
    out: TextIO,
    on_awaiting: AwaitingHook | None,
) -> Commit:
    """The ``await-exit`` half of :func:`commit_shortcuts`: Steam is running
    and the file has changed. Never sends ``steam -shutdown`` and never
    spawns ``steam``: the client's exit is someone else's doing (the
    plugin's ``StartShutdown`` from Game Mode, or the user quitting it),
    and in a gamescope session the session relaunches it (decky spec 2.3).
    """
    timeout = AWAIT_EXIT_TIMEOUT_S
    print(
        f"waiting for Steam to exit (up to {timeout:.0f} s) before writing shortcuts.vdf",
        file=out,
    )
    if on_awaiting is not None:
        on_awaiting(timeout)
    # Interruptible on purpose: nothing has been done to Steam, so a Ctrl-C
    # here leaves the file untouched and exits 130 (decky spec 3.13 A2).
    gone = steam.wait_for_exit(runner, timeout=timeout, poll_interval=AWAIT_EXIT_POLL_S)
    if not gone:
        raise SteamRunningError(AWAIT_EXIT_TIMED_OUT)
    result = Commit(awaited_exit=True)
    with sigint_deferred():
        before = _backups(shortcuts_file)
        result.written = shortcuts_file.write()
        result.backup = next(iter(sorted(_backups(shortcuts_file) - before)), None)
    return result


def _backups(shortcuts_file: ShortcutsFile) -> set[Path]:
    path = shortcuts_file.path
    if path is None or not path.parent.is_dir():
        return set()
    return set(path.parent.glob(f"{path.name}.bak-*"))


def steam_aware_provider(
    config: Config, *, runner: ProcessRunner | None = None, mode: str | None = None
) -> SteamShortcutProvider:
    """The ``art`` command's provider: commits through :func:`commit_shortcuts`.

    ``mode`` is ``art --commit`` (decky spec 3.13 A1), passed straight
    through. The provider's :attr:`~SteamShortcutProvider.commit_out` and
    :attr:`~SteamShortcutProvider.on_awaiting_exit` are read when the
    write happens, so ``cmd_art`` can route the writer's human lines and
    the ``awaiting-steam-exit`` event through its own :class:`Reporter`.
    """

    def write(shortcuts_file: ShortcutsFile) -> CommitResult:
        commit = commit_shortcuts(
            shortcuts_file,
            config=config,
            runner=runner,
            out=provider.commit_out,
            mode=mode,
            on_awaiting=provider.on_awaiting_exit,
        )
        return CommitResult(
            written=commit.written,
            restarted=commit.restarted,
            backup=commit.backup,
            relaunch_error=commit.relaunch_error,
            awaited_exit=commit.awaited_exit,
        )

    provider = SteamShortcutProvider(config, writer=write)
    return provider


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


@dataclass
class SyncOptions:
    dry_run: bool = False
    no_art: bool = False
    limit: int | None = None
    retry_missing: bool = False
    #: ``--park-unpublished`` (decky spec 3.12).
    park_unpublished: bool = False
    #: ``--client-shortcut`` (decky spec 3.4.5).
    client_shortcut: bool = False
    #: ``--hide-host-apps`` (decky spec 3.14).
    hide_host_apps: bool = False
    #: ``--commit`` (decky spec 3.5); ``None`` lets ``restart_steam`` decide.
    commit: str | None = None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> SyncOptions:
        return cls(
            dry_run=bool(getattr(args, "dry_run", False)),
            no_art=bool(getattr(args, "no_art", False)),
            limit=getattr(args, "limit", None),
            retry_missing=bool(getattr(args, "retry_missing", False)),
            park_unpublished=bool(getattr(args, "park_unpublished", False)),
            client_shortcut=bool(getattr(args, "client_shortcut", False)),
            hide_host_apps=bool(getattr(args, "hide_host_apps", False)),
            commit=getattr(args, "commit", None),
        )


def _plan_event_fields(plan: Plan) -> dict[str, Any]:
    """``plan`` event fields (spec 3.4.6): the header's counts plus the
    kinds (``stream``/``shortcut`` = published titles of that kind this run,
    ``parked`` = parked entries after the run) and the duplicates map."""
    fields = {
        "host": plan.host,
        "published": len(plan.apps),
        "ignored": len(plan.ignored),
        "present": len(plan.present),
        "to_add": len(plan.to_add),
        "to_replace": len(plan.to_replace),
        "to_park": len(plan.to_park),
        "to_unpark": len(plan.to_unpark),
        "to_remove": len(plan.to_remove),
        "pending": len(plan.pending),
        "limit": plan.limit,
        "stream": plan.stream_count,
        "shortcut": plan.shortcut_count,
        "parked": plan.parked_count,
        "duplicates": dict(plan.duplicates),
    }
    if plan.hide_host_apps:
        fields["host_app"] = plan.host_app_count
    return fields


def _added_by_kind(plan: Plan, *, written: bool) -> dict[str, int]:
    """``summary.added_by_kind`` (decky spec 3.13 A3): the *new* entries per
    kind -- never a replacement -- and, like ``added``, zeros when nothing
    was written."""
    counts = {KIND_STREAM: 0, KIND_SHORTCUT: 0}
    if plan.hide_host_apps:
        counts[KIND_HOST_APP] = 0
    if written:
        for item in plan.to_add:
            kind = plan.kinds.get(item.app.name, KIND_SHORTCUT)
            counts[kind if kind in counts else KIND_SHORTCUT] += 1
    return counts


def _summary_event_fields(
    plan: Plan,
    summary: RunSummary | None,
    commit: Commit | None,
    *,
    exit_code: int,
    stop_reason: str | None = None,
) -> dict[str, Any]:
    written = commit is not None and commit.written
    # A replacement or removal the commit applied counts even when the file
    # did not change bytes (a `rematched` replacement that keeps its appid
    # and icon path, decky spec 3.4.4); an addition always changes them, so
    # `added` keeps its "0 when nothing was written" reading (spec 3.4.6).
    applied = commit is not None and commit.applied
    resolved_stop_reason = stop_reason if stop_reason is not None else (
        summary.stop_reason if summary and summary.stopped_early else None
    )
    return {
        "added": len(plan.to_add) if written else 0,
        "replaced": len(plan.to_replace) if applied else 0,
        "removed": len(plan.to_remove) if applied else 0,
        "filled": summary.filled if summary is not None else 0,
        "missing": summary.missing if summary is not None else 0,
        "unmatched": summary.unmatched if summary is not None else [],
        "duplicates": dict(plan.duplicates),
        "pending": len(plan.pending),
        "stopped_early": resolved_stop_reason is not None,
        "stop_reason": resolved_stop_reason,
        "exit": exit_code,
        "added_by_kind": _added_by_kind(plan, written=written),
    }


def _title_event_fields(index: int, total: int, result: Any) -> dict[str, Any]:
    """``title`` event fields (spec 3.4.6); ``kind`` is the plan's kind for
    the title, carried on the :class:`ArtTarget` by :func:`art_targets`."""
    if result.skipped:
        return {
            "index": index,
            "total": total,
            "name": result.target.name,
            "kind": result.target.kind,
            "appid": result.target.appid,
            "match": None,
            "slots": {},
        }
    return {
        "index": index,
        "total": total,
        "name": result.target.name,
        "kind": result.target.kind,
        "appid": result.target.appid,
        "match": match_json(result.match),
        "slots": {key: slot_json_value(outcome.source) for key, outcome in result.slots.items()},
    }


def _remove_summary_event_fields(
    removed: int, exit_code: int, *, stop_reason: str | None = None
) -> dict[str, Any]:
    """``remove``'s ``summary`` (spec 3.4.6): ``sync``'s key set minus
    ``added_by_kind`` (a ``sync``-only key, decky spec 3.13 A3), with only
    ``removed`` ever non-zero -- ``remove`` has no art phase or plan of its
    own. ``stop_reason`` is set the way :func:`_summary_event_fields` sets
    it: ``"interrupted"`` when a Ctrl-C during the ``--commit await-exit``
    wait ended the run (spec 3.4.6, decky spec 3.13 A2), else ``None``."""
    return {
        "added": 0,
        "replaced": 0,
        "removed": removed,
        "filled": 0,
        "missing": 0,
        "unmatched": [],
        "duplicates": {},
        "pending": 0,
        "stopped_early": stop_reason is not None,
        "stop_reason": stop_reason,
        "exit": exit_code,
    }


def _replace_event_fields(replacement: Replacement) -> dict[str, Any]:
    return {
        "name": replacement.name,
        "old_appid": replacement.old.appid,
        "new_appid": replacement.new.appid,
        "reason": replacement.reason,
    }


def _commit_event_fields(commit: Commit) -> dict[str, Any]:
    return {
        "written": bool(commit.written),
        "restarted": bool(commit.restarted),
        "backup": commit.backup.name if commit.backup is not None else None,
        "relaunch_error": commit.relaunch_error,
    }


def awaiting_hook(reporter: Reporter) -> AwaitingHook:
    """The ``awaiting-steam-exit`` event (spec 3.4.6), for
    :func:`commit_shortcuts`'s ``on_awaiting``; the human line is the
    writer's own (it goes to ``out`` like "shutting down Steam" does)."""

    def emit(timeout_s: float) -> None:
        reporter.event("awaiting-steam-exit", timeout_s=int(timeout_s))

    return emit


def cmd_sync(
    args: argparse.Namespace,
    config: Config,
    *,
    deps: Deps | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``moonlight-steam-sync sync`` (spec 3.3, 3.6, 3.7, 3.9)."""
    deps = deps or Deps()
    out = out or sys.stdout
    err = err or sys.stderr
    reporter = Reporter(
        out, err, json=bool(getattr(args, "json", False)), command="sync", version=_pkg_version()
    )
    reporter.start()
    art_out = reporter.err if reporter.json else reporter.out
    options = SyncOptions.from_args(args)
    if options.limit is not None and options.limit < 0:
        reporter.error("sync: --limit must be zero or more", EXIT_USAGE_OR_CONFIG)
        return EXIT_USAGE_OR_CONFIG
    ignore_extra = _load_ignore_extra_or_report("sync", args, config, reporter)
    if ignore_extra is None:
        return EXIT_USAGE_OR_CONFIG
    ok, owned = _load_owned_or_report("sync", args, config, reporter)
    if not ok:
        return EXIT_USAGE_OR_CONFIG

    if not _need_host("sync", config, reporter):
        return EXIT_USAGE_OR_CONFIG
    library = _open_library_or_report("sync", reporter)
    if library is None:
        return EXIT_USAGE_OR_CONFIG
    if not _check_owned_user("sync", args, config, library, reporter):
        return EXIT_USAGE_OR_CONFIG
    apps = _list_host_or_report("sync", config, deps, reporter)
    if apps is None:
        return EXIT_MOONLIGHT_UNREACHABLE

    # With --owned-apps the kinds depend on the matches, so every published
    # title is resolved *before* the plan (decky spec 3.4.2) -- lookups
    # only, cache-first and flushed per title, even under --no-art.
    services: ArtServices | None = None
    if not options.no_art or owned is not None:
        services = deps.services or build_services(
            config, cache_path=deps.cache_path, retry_missing=options.retry_missing
        )
        services.resolver.force = False
        services.resolver.retry_missing = options.retry_missing
        if not config.sgdb_api_key:
            reporter.note(
                "no SteamGridDB API key configured; using Steam's store search and CDN only"
            )

    ignore = set(config.ignore) | set(ignore_extra)
    names = [name for name in dict.fromkeys(app.name for app in apps) if name not in ignore]
    matches: dict[str, Match | None]
    if owned is not None:
        assert services is not None
        try:
            matches = dict(resolve_titles(names, services.resolver, out=art_out))
        except HardStop as exc:
            # Before the plan exists there is no `summary` to owe (spec
            # 3.4.6); the cache holds every title resolved so far.
            reporter.error(f"sync: {exc}", EXIT_NETWORK_STOPPED)
            return EXIT_NETWORK_STOPPED
        except KeyboardInterrupt:
            services.cache.flush()
            reporter.error(RESUME_HINT, EXIT_SIGINT)
            return EXIT_SIGINT
    else:
        # No resolution ahead of the plan, but the plan still reads each
        # title's cache entry: a `stale_art` flag (`match --defer-art`,
        # decky spec 3.4.4) is a `rematched` replacement in a plain sync
        # too, whatever the entry's `how` -- the placeholder `--unpin`
        # leaves included. A cache read only, no lookups, no HTTP; with no
        # flagged entry the plan is v0.2.0's (3.11).
        cache = (
            services.cache
            if services is not None
            else MatchCache(deps.cache_path or default_cache_path())
        )
        matches = {name: cache.get(name) for name in names}

    plan = build_plan(
        config,
        apps,
        library.file,
        limit=options.limit,
        ignore_extra=ignore_extra,
        matches=matches,
        owned=owned,
        park_unpublished=options.park_unpublished,
        hide_host_apps=options.hide_host_apps,
        client_shortcut=options.client_shortcut,
    )
    reporter.line(plan.header())
    reporter.plan(**_plan_event_fields(plan))

    try:
        if options.dry_run:
            # --no-art: no per-slot plan (the services, if any, were only
            # there to resolve the kinds).
            return _dry_run(
                plan, library, config, None if options.no_art else services, reporter
            )
        return _sync(plan, library, config, options, services, deps, reporter)
    except KeyboardInterrupt:
        if services is not None:
            services.cache.flush()
        reporter.event(
            "summary",
            **_summary_event_fields(
                plan, None, None, exit_code=EXIT_SIGINT, stop_reason="interrupted"
            ),
        )
        reporter.error(RESUME_HINT, EXIT_SIGINT)
        return EXIT_SIGINT


def _sync(
    plan: Plan,
    library: Library,
    config: Config,
    options: SyncOptions,
    services: ArtServices | None,
    deps: Deps,
    reporter: Reporter,
) -> int:
    art_out = reporter.err if reporter.json else reporter.out
    if plan.is_empty:
        reporter.line("nothing to do")
        reporter.event("summary", **_summary_event_fields(plan, None, None, exit_code=EXIT_OK))
        return EXIT_OK

    # In memory only: the file is not written until commit_shortcuts, and
    # not at all if the art phase stops early. Appending now is what lets
    # the art phase patch `icon` on a shortcut that does not exist yet.
    # The grid-file moves below, though, happen now (decky spec 3.4.2): a
    # replacement's art is renamed to its new appid, a duplicate's and a
    # rematched title's art is deleted -- each idempotent, so the rerun
    # after a crash finds them done and does the rest.
    for entry in plan.to_remove:
        library.file.remove(entry)
        delete_grid_files(library.grid_dir, entry.appid)
    for item in plan.to_replace:
        apply_replacement(item, library)
        reporter.line(describe_replacement(item))
        reporter.event("replace", **_replace_event_fields(item))
    for item in plan.to_add:
        library.file.append(item.shortcut)
        if item.app.name in plan.stale:
            # No entry, but the flag says whatever art sits under this
            # appid came from an earlier match (decky spec 3.4.4).
            delete_grid_files(library.grid_dir, item.shortcut.appid)
    if plan.client_to_add is not None:
        library.file.append(plan.client_to_add)
    # Decky spec 3.11/3.12: IsHidden is the one field edited in place.
    for entry in plan.to_park:
        entry.is_hidden = 1
    for entry in plan.to_unpark:
        entry.is_hidden = 0
    for entry in plan.to_hide:
        entry.is_hidden = 1
    targets = art_targets(plan, library.grid_dir)

    title_counter = itertools.count(1)

    def on_title(result: Any) -> None:
        index = next(title_counter)
        reporter.title(**_title_event_fields(index, len(targets), result))

    summary: RunSummary | None = None
    # With --owned-apps the services exist for the resolution step even
    # under --no-art; the art phase itself is what --no-art skips.
    if services is not None and not options.no_art:
        provider = LibraryProvider(library.file, targets)
        summary = run_art(
            targets,
            services.resolver,
            services.selector,
            provider=provider,
            out=art_out,
            on_title=on_title if reporter.json else None,
        )
        for line in summary.lines():
            reporter.line(line)
        if summary.stopped_early:
            # Spec 3.9 items 1 and 4: everything durable is already on disk,
            # shortcuts.vdf is untouched, and the same command finishes the job.
            exit_code = (
                EXIT_SIGINT if summary.stop_reason == "interrupted" else EXIT_NETWORK_STOPPED
            )
            reporter.event(
                "summary",
                **_summary_event_fields(
                    plan,
                    summary,
                    None,
                    exit_code=exit_code,
                    stop_reason="interrupted" if exit_code == EXIT_SIGINT else summary.stop_reason,
                ),
            )
            if exit_code == EXIT_SIGINT:
                # Original behaviour always printed this to err, json or not.
                reporter.error(RESUME_HINT, EXIT_SIGINT)
            else:
                # Original behaviour printed nothing extra to err here (the
                # stop reason is already on `out`/`err` via summary.lines());
                # only add the --json error event, never a new err line.
                reporter.event("error", exit=EXIT_NETWORK_STOPPED, message=summary.stop_reason)
            return exit_code
        # The art phase has run for every stale title, so its art is now
        # the current match's (decky spec 3.4.4); never after an early stop,
        # and never under --no-art, which fetched nothing.
        for name in sorted(plan.stale):
            services.cache.clear_stale_art(name)
    # The field follows the file whoever wrote it (spec 3.6); with --no-art
    # this is the only icon patching there is.
    patch_icons_from_disk(library.file, targets)

    try:
        commit = commit_shortcuts(
            library.file,
            config=config,
            runner=deps.runner,
            out=art_out,
            art_written=bool(summary and summary.written),
            mode=options.commit,
            on_awaiting=awaiting_hook(reporter),
        )
    except SteamRunningError as exc:
        reporter.event(
            "summary",
            **_summary_event_fields(plan, summary, None, exit_code=EXIT_STEAM_RUNNING),
        )
        reporter.error(f"sync: {exc}", EXIT_STEAM_RUNNING)
        return EXIT_STEAM_RUNNING
    except KeyboardInterrupt:
        # Only the await-exit wait lets a Ctrl-C through here (decky spec
        # 3.13 A2): the file is untouched, the art is on disk, and the
        # rerun only writes. Reported from here rather than cmd_sync's
        # catch-all so the summary carries the real art-phase numbers.
        if services is not None:
            services.cache.flush()
        reporter.event(
            "summary",
            **_summary_event_fields(
                plan, summary, None, exit_code=EXIT_SIGINT, stop_reason="interrupted"
            ),
        )
        reporter.error(RESUME_HINT, EXIT_SIGINT)
        return EXIT_SIGINT

    reporter.line(commit.describe(library.user.shortcuts_path))
    reporter.event("commit", **_commit_event_fields(commit))
    for line in _final_summary(plan, summary, commit):
        reporter.line(line)
        if line == "restart Steam to see the new artwork" or line.endswith(
            "rerun to add the next batch"
        ):
            # Spec 3.4.6: these two human lines are additionally emitted as
            # `note` events under --json, on top of the plain `line` above.
            reporter.event("note", message=line)
    reporter.event("summary", **_summary_event_fields(plan, summary, commit, exit_code=EXIT_OK))
    return EXIT_OK


def _final_summary(plan: Plan, summary: RunSummary | None, commit: Commit) -> list[str]:
    added = len(plan.to_add) if commit.written else 0
    line = (
        f"added {added} shortcut(s), adopted {len(plan.adopted)}, "
        f"ignored {len(plan.ignored)}, already present {len(plan.present)}"
    )
    # Only ever appended when the plan holds such a change, so a run with
    # none of the new flags prints exactly the v0.2.0 line (decky spec 3.11).
    # These count what the commit *applied*, not what changed bytes: a
    # `rematched` replacement that keeps its appid and icon path serialises
    # to the same file, and its art was still deleted and re-fetched.
    applied = commit.applied
    if plan.to_replace:
        line += f", replaced {len(plan.to_replace) if applied else 0}"
    if plan.to_remove:
        line += f", removed {len(plan.to_remove) if applied else 0}"
    if plan.to_park:
        line += f", parked {len(plan.to_park) if applied else 0}"
    if plan.to_unpark:
        line += f", unparked {len(plan.to_unpark) if applied else 0}"
    if plan.to_hide:
        line += f", hidden {len(plan.to_hide) if applied else 0}"
    if plan.client_to_add is not None and applied:
        line += ", added the Moonlight client shortcut"
    lines = [line]
    for loser, winner in plan.duplicates.items():
        lines.append(f"duplicate {loser}: same owned game as {winner}; no shortcut written")
    if plan.to_add and not commit.written:
        lines.append(
            f"{len(plan.to_add)} planned shortcut(s) were not written (see above)"
        )
    other_changes = (
        len(plan.to_replace)
        + len(plan.to_remove)
        + len(plan.to_park)
        + len(plan.to_unpark)
        + len(plan.to_hide)
        + (1 if plan.client_to_add is not None else 0)
    )
    if other_changes and not commit.applied:
        lines.append(
            f"{other_changes} planned replacement(s), removal(s) and IsHidden flip(s) were not "
            "written (see above)"
        )
    if plan.pending:
        lines.append(
            f"{len(plan.pending)} still pending after --limit {plan.limit}; "
            "rerun to add the next batch"
        )
    if summary is not None and summary.written and not commit.restarted:
        lines.append("restart Steam to see the new artwork")
    return lines


# -- dry run ---------------------------------------------------------------


def _dry_run(
    plan: Plan,
    library: Library,
    config: Config,
    services: ArtServices | None,
    reporter: Reporter,
) -> int:
    """Print the plan: shortcuts to add, replace, remove, park or hide, and
    the per-slot art source and URL.

    Touches nothing under Steam. With art enabled it does resolve each title
    (that is the only way to know a CDN URL), so the match cache is the one
    thing a dry run writes -- the real run then reuses every resolution.
    Emits ``replace`` per replacement and ``summary`` (exit 0, or 4 on a
    hard stop) and no ``title`` events (spec 3.4.6): its per-slot lines are
    human-only, on ``err`` under ``--json``.
    """
    resolver = services.resolver if services is not None else None
    selector = services.selector if services is not None else None

    def slot_plan(target: ArtTarget) -> None:
        if resolver is not None and selector is not None:
            _print_slot_plan(target, resolver, selector, reporter)

    try:
        for item in plan.adopted:
            if item.kind == KIND_CLIENT:
                # The client entry never gets art (decky spec 3.4.5).
                reporter.line(f"  adopted  {item.name} [{item.shortcut.appid}] (client shortcut)")
                continue
            reporter.line(f"  adopted  {item.name} [{item.shortcut.appid}]")
            slot_plan(ArtTarget(item.name, item.shortcut.appid, library.grid_dir))
        for entry in plan.to_remove:
            name = entry.moonlight_name(config.launch_options, config.name_suffix)
            reporter.line(
                f"  remove   {name} [{entry.appid}] (same owned game as "
                f"{plan.duplicates.get(name, '?')})"
            )
        for item in plan.to_replace:
            reporter.line(f"  {describe_replacement(item)}")
            reporter.event("replace", **_replace_event_fields(item))
            slot_plan(ArtTarget(item.name, item.new.appid, library.grid_dir))
        for item in plan.to_add:
            reporter.line(f"  add      {item.app.name} [{item.shortcut.appid}]")
            slot_plan(ArtTarget(item.app.name, item.shortcut.appid, library.grid_dir))
        if plan.client_to_add is not None:
            reporter.line(
                f"  add      {CLIENT_APP_NAME} [{plan.client_to_add.appid}] (client shortcut)"
            )
        for entry in plan.to_park:
            name = entry.moonlight_name(config.launch_options, config.name_suffix)
            reporter.line(f"  park     {name} [{entry.appid}]")
        for entry in plan.to_unpark:
            name = entry.moonlight_name(config.launch_options, config.name_suffix)
            reporter.line(f"  unpark   {name} [{entry.appid}]")
        for entry in plan.to_hide:
            name = entry.moonlight_name(config.launch_options, config.name_suffix)
            what = "host app" if plan.kinds.get(name) == KIND_HOST_APP else "stream entry"
            reporter.line(f"  hide     {name} [{entry.appid}] ({what}, hidden by its kind)")
        for item in plan.pending:
            name = item.app.name if isinstance(item, Addition) else item.name
            reporter.line(f"  pending  {name} (beyond --limit {plan.limit})")
        for app in plan.ignored:
            reporter.line(f"  ignored  {app.name}")
    except HardStop as exc:
        reporter.line(str(exc))
        reporter.event(
            "summary",
            **_summary_event_fields(
                plan, None, None, exit_code=EXIT_NETWORK_STOPPED, stop_reason=str(exc)
            ),
        )
        # Original behaviour printed nothing to err here (the message is
        # already on `out`/`err` via reporter.line above); only add the
        # --json error event, never a new err line.
        reporter.event("error", exit=EXIT_NETWORK_STOPPED, message=str(exc))
        return EXIT_NETWORK_STOPPED
    reporter.line("dry run: nothing written")
    reporter.event("summary", **_summary_event_fields(plan, None, None, exit_code=EXIT_OK))
    return EXIT_OK


def _print_slot_plan(
    target: ArtTarget, resolver: Resolver, selector: Selector, reporter: Reporter
) -> None:
    match = resolver.resolve(target.name)
    if match.skipped:
        reporter.line("           art: skipped (overrides)")
        return
    reporter.line(f"           match: {match.how}")
    for slot in SLOTS:
        existing = existing_slot_file(target.grid_dir, target.appid, slot)
        if existing is not None:
            reporter.line(f"           {slot.key}: kept {existing.name}")
            continue
        first = next(iter(selector.candidates(slot, match)), None)
        where = first.describe() if first is not None else "no source"
        reporter.line(f"           {slot.key}: {where}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _other_hosts_apps(hosts_directory: Path, current_host: str) -> dict[str, list[str]]:
    """Every *other* cached host's app names, keyed by host name (spec 3.12)."""
    result: dict[str, list[str]] = {}
    for info in hosts.list_cached_hosts(hosts_directory):
        if info.name.casefold() == current_host.casefold():
            continue
        cache = hosts.read_host_cache(hosts_directory, info.name)
        if cache is not None:
            result[info.name] = cache.apps
    return result


def _same_game_as(
    name: str, match_cache: MatchCache, other_hosts: dict[str, list[str]]
) -> list[dict[str, str]]:
    """``same-game-as`` pairs for *name* (spec 3.4.8, 3.12).

    Two names are "the same game" when both resolved (``how`` not
    ``none``/``skipped``) and share a non-null ``steam_appid``, or -- when
    neither has one -- a non-null ``sgdb_id``. Identical spellings are never
    reported (one shared entry); a name missing from ``matches.json`` is
    skipped entirely, never resolved for this.
    """
    entry = match_cache.get(name)
    if entry is None or entry.how in ("none", "skipped"):
        return []
    results: list[dict[str, str]] = []
    for host_name, names in other_hosts.items():
        for other_name in dict.fromkeys(names):
            if other_name == name:
                continue
            other = match_cache.get(other_name)
            if other is None or other.how in ("none", "skipped"):
                continue
            same = False
            if entry.steam_appid is not None and entry.steam_appid == other.steam_appid or (
                entry.steam_appid is None
                and other.steam_appid is None
                and entry.sgdb_id is not None
                and entry.sgdb_id == other.sgdb_id
            ):
                same = True
            if same:
                results.append({"name": other_name, "host": host_name})
    return results


def cmd_list(
    args: argparse.Namespace,
    config: Config,
    *,
    deps: Deps | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``moonlight-steam-sync list``: the host's apps, annotated added / ignored / new.

    ``--cached`` (spec 3.4.6/3.12) never runs Moonlight: it serves the
    per-host list cache and exits 3, the same code a failed live list gets,
    when there is none for this host.
    """
    deps = deps or Deps()
    out = out or sys.stdout
    err = err or sys.stderr
    reporter = Reporter(
        out, err, json=bool(getattr(args, "json", False)), command="list", version=_pkg_version()
    )
    reporter.start()
    ignore_extra = _load_ignore_extra_or_report("list", args, config, reporter)
    if ignore_extra is None:
        return EXIT_USAGE_OR_CONFIG
    ok, owned = _load_owned_or_report("list", args, config, reporter)
    if not ok:
        return EXIT_USAGE_OR_CONFIG
    if not _need_host("list", config, reporter):
        return EXIT_USAGE_OR_CONFIG
    library = _open_library_or_report("list", reporter)
    if library is None:
        return EXIT_USAGE_OR_CONFIG
    if not _check_owned_user("list", args, config, library, reporter):
        return EXIT_USAGE_OR_CONFIG

    hosts_directory = deps.resolved_hosts_dir()
    cached_mode = bool(getattr(args, "cached", False))
    cached_when: str | None = None
    if cached_mode:
        cache = hosts.read_host_cache(hosts_directory, config.host)
        if cache is None:
            reporter.error(
                f"list: no cached app list for host {config.host}; run "
                f"`list --host {config.host}` while it is reachable",
                EXIT_MOONLIGHT_UNREACHABLE,
            )
            return EXIT_MOONLIGHT_UNREACHABLE
        apps = [moonlight.App(name=n) for n in cache.apps]
        cached_when = cache.when
    else:
        apps = _list_host_or_report("list", config, deps, reporter)
        if apps is None:
            return EXIT_MOONLIGHT_UNREACHABLE

    match_cache = MatchCache(deps.cache_path or default_cache_path())
    # `list` never resolves a title (decky spec 3.4.8): with --owned-apps
    # the kinds come from whatever matches.json already holds.
    matches: dict[str, Match | None] | None = None
    if owned is not None:
        matches = {app.name: match_cache.get(app.name) for app in apps}
    plan = build_plan(
        config,
        apps,
        library.file,
        ignore_extra=ignore_extra,
        matches=matches,
        owned=owned,
        hide_host_apps=bool(getattr(args, "hide_host_apps", False)),
    )
    other_hosts = _other_hosts_apps(hosts_directory, config.host)
    seen: set[str] = set()
    count = 0
    for app in plan.apps:
        if app.name in seen:
            continue
        seen.add(app.name)
        label = plan.label(app)
        kind = plan.kind(app)
        entry = match_cache.get(app.name)
        match = match_json(entry) if entry is not None else None
        fuzzy = bool(entry and entry.how.endswith(":fuzzy"))
        same = _same_game_as(app.name, match_cache, other_hosts)
        reporter.line(f"{label:8} {app.name}")
        if app.name in plan.duplicates:
            reporter.line(f"         duplicate-of: {plan.duplicates[app.name]}")
        for item in same:
            reporter.line(f"         same-game-as: {item['name']} ({item['host']})")
        reporter.event(
            "app",
            name=app.name,
            label=label,
            kind=kind,
            match=match,
            fuzzy=fuzzy,
            duplicate_of=plan.duplicates.get(app.name),
            same_game_as=same,
            cached=cached_mode,
            cached_when=cached_when,
        )
        count += 1
    reporter.line(plan.header())
    reporter.event("end", count=count, host=config.host)
    return EXIT_OK


# ---------------------------------------------------------------------------
# ignore
# ---------------------------------------------------------------------------


def toml_string(value: str) -> str:
    """A TOML basic string. JSON's escapes are a subset of TOML's."""
    return json.dumps(value, ensure_ascii=False)


def ignore_lines(names: Sequence[str]) -> list[str]:
    """The ``ignore = [...]`` block, one name per line, ready to paste."""
    if not names:
        return ["ignore = []"]
    return ["ignore = [", *(f"    {toml_string(name)}," for name in names), "]"]


def cmd_ignore(
    args: argparse.Namespace,
    config: Config,
    *,
    deps: Deps | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``moonlight-steam-sync ignore --all | "Name"...`` -- prints TOML to paste.

    The tool never writes ``config.toml`` (spec 6.7): the printed array is
    the current ``ignore`` list plus, for ``--all``, every host app that has
    no shortcut yet -- the "draw a line under everything on the PC" workflow
    from spec 3.7. Under ``--json`` the report moves to stderr and the
    command emits ``start`` then ``end`` (spec 3.4.6).
    """
    deps = deps or Deps()
    out = out or sys.stdout
    err = err or sys.stderr
    reporter = Reporter(
        out, err, json=bool(getattr(args, "json", False)), command="ignore", version=_pkg_version()
    )
    reporter.start()
    ignore_extra = _load_ignore_extra_or_report("ignore", args, config, reporter)
    if ignore_extra is None:
        return EXIT_USAGE_OR_CONFIG

    names = list(config.ignore)
    if getattr(args, "all", False):
        if not _need_host("ignore", config, reporter):
            return EXIT_USAGE_OR_CONFIG
        library = _open_library_or_report("ignore", reporter)
        if library is None:
            return EXIT_USAGE_OR_CONFIG
        apps = _list_host_or_report("ignore", config, deps, reporter)
        if apps is None:
            return EXIT_MOONLIGHT_UNREACHABLE
        # The --ignore-file names already keep an app out of `to_add`, so
        # they are not offered here again; the printed block stays what it
        # always was -- config.toml's list plus the new names -- because it
        # is meant for config.toml (decky spec 3.4.3).
        plan = build_plan(config, apps, library.file, ignore_extra=ignore_extra)
        names.extend(item.app.name for item in plan.to_add)
    else:
        names.extend(getattr(args, "names", []) or [])

    deduped = list(dict.fromkeys(names))
    lines = ignore_lines(deduped)
    for line in lines:
        reporter.line(line)
    print(
        f"paste the block above into {config.config_path} (this tool never writes it)",
        file=err,
    )
    reporter.event("end", count=len(lines))
    return EXIT_OK


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def grid_files_for(grid_dir: Path, appid: int) -> list[Path]:
    """The five grid files for ``appid`` that exist, plus any stray ``.part``."""
    files: list[Path] = []
    for slot in steam.GRID_SLOTS:
        existing = steam.find_grid_file(grid_dir, appid, slot)
        if existing is not None:
            files.append(existing)
        part = grid_dir / f"{steam.grid_stem(appid, slot)}.part"
        if part.is_file():
            files.append(part)
    return files


def cmd_remove(
    args: argparse.Namespace,
    config: Config,
    *,
    deps: Deps | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``moonlight-steam-sync remove --all | "Name"...`` (spec 3.6).

    Deletes owned entries and their five grid files, through the same
    shutdown -> write -> relaunch path as ``sync``. An unknown name is a
    usage error before anything is touched; the grid files go only after
    the write succeeded, so a refused write (exit 2) leaves the art in
    place for the shortcut that is still there.
    """
    deps = deps or Deps()
    out = out or sys.stdout
    err = err or sys.stderr
    reporter = Reporter(
        out, err, json=bool(getattr(args, "json", False)), command="remove", version=_pkg_version()
    )
    reporter.start()

    library = _open_library_or_report("remove", reporter)
    if library is None:
        return EXIT_USAGE_OR_CONFIG
    owned = library.file.owned(config.exe)
    # The Moonlight client entry (decky spec 3.4.5) is identified by its
    # launch options and the tool's own exe, never by name: `--all` takes it
    # only when `config.exe` is the tool (ownership is still by exe),
    # `--client` takes it whoever `config.exe` is, and no name reaches it.
    client = library.file.client_entry(default_exe())
    by_name: dict[str, Shortcut] = {}
    for entry in owned:
        if entry is client:
            continue
        by_name.setdefault(entry.moonlight_name(config.launch_options, config.name_suffix), entry)

    if getattr(args, "all", False):
        victims = list(owned)
    else:
        wanted = list(getattr(args, "names", []) or [])
        unknown = [name for name in wanted if name not in by_name]
        if unknown:
            unknown_message = "remove: no owned shortcut named " + ", ".join(
                repr(n) for n in unknown
            )
            print(unknown_message, file=err)
            print("remove: nothing was touched", file=err)
            # error.message is the informative line, not the generic one
            # printed alongside it (both stay on stderr either way).
            reporter.event("error", exit=EXIT_USAGE_OR_CONFIG, message=unknown_message)
            return EXIT_USAGE_OR_CONFIG
        victims = [by_name[name] for name in dict.fromkeys(wanted)]
    if getattr(args, "client", False) and client is not None and not any(
        v is client for v in victims
    ):
        victims.append(client)

    if not victims:
        reporter.line("nothing to remove")
        reporter.event("summary", **_remove_summary_event_fields(0, EXIT_OK))
        return EXIT_OK

    for entry in victims:
        library.file.remove(entry)
    write_out = reporter.err if reporter.json else reporter.out
    try:
        commit = commit_shortcuts(
            library.file,
            config=config,
            runner=deps.runner,
            out=write_out,
            mode=getattr(args, "commit", None),
            on_awaiting=awaiting_hook(reporter),
        )
    except SteamRunningError as exc:
        reporter.error(f"remove: {exc}", EXIT_STEAM_RUNNING)
        return EXIT_STEAM_RUNNING
    except KeyboardInterrupt:
        # The await-exit wait is interruptible (decky spec 3.13 A2): nothing
        # was written and no grid file touched.
        reporter.event(
            "summary",
            **_remove_summary_event_fields(0, EXIT_SIGINT, stop_reason="interrupted"),
        )
        reporter.error(RESUME_HINT, EXIT_SIGINT)
        return EXIT_SIGINT

    deleted = 0
    for entry in victims:
        for path in grid_files_for(library.grid_dir, entry.appid):
            with contextlib.suppress(OSError):
                path.unlink()
                deleted += 1
    reporter.line(commit.describe(library.user.shortcuts_path))
    reporter.event("commit", **_commit_event_fields(commit))
    reporter.line(f"removed {len(victims)} shortcut(s) and {deleted} grid file(s)")
    reporter.event("summary", **_remove_summary_event_fields(len(victims), EXIT_OK))
    return EXIT_OK


__all__ = [
    "AWAIT_EXIT_POLL_S",
    "AWAIT_EXIT_TIMED_OUT",
    "AWAIT_EXIT_TIMEOUT_S",
    "COMMIT_AWAIT_EXIT",
    "COMMIT_MODES",
    "COMMIT_REFUSE",
    "COMMIT_RESTART",
    "KIND_CLIENT",
    "KIND_DUPLICATE",
    "KIND_HOST_APP",
    "KIND_IGNORED",
    "KIND_PARKED",
    "KIND_SHORTCUT",
    "KIND_STREAM",
    "REASON_KIND_CHANGED",
    "REASON_NAME_CHANGED",
    "REASON_REMATCHED",
    "STREAM_HOWS",
    "Addition",
    "Adopted",
    "Commit",
    "Deps",
    "Library",
    "LibraryProvider",
    "Plan",
    "Replacement",
    "SyncOptions",
    "apply_replacement",
    "art_targets",
    "awaiting_hook",
    "build_plan",
    "client_shortcut_entry",
    "cmd_ignore",
    "cmd_list",
    "cmd_remove",
    "cmd_sync",
    "commit_shortcuts",
    "delete_grid_files",
    "describe_replacement",
    "grid_files_for",
    "ignore_lines",
    "new_shortcut",
    "open_library",
    "patch_icons_from_disk",
    "resolve_commit_mode",
    "resolve_titles",
    "steam_aware_provider",
    "toml_string",
]
