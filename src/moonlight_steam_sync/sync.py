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
  the one path from an in-memory :class:`ShortcutsFile` to disk: it checks
  whether Steam is running, refuses (exit 2) when it may not restart it,
  otherwise ``steam -shutdown`` -> wait -> write -> ``steam -silent``,
  with ``SIGINT`` deferred across that window so Ctrl-C can never leave
  Steam down with the file unwritten.
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
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from moonlight_steam_sync import hosts, moonlight, steam
from moonlight_steam_sync import version as _pkg_version
from moonlight_steam_sync.art import resolve as art_resolve
from moonlight_steam_sync.art.apply import (
    ArtTarget,
    RunSummary,
    SteamShortcutProvider,
    patch_icon,
    run_art,
)
from moonlight_steam_sync.art.cli import ArtServices, build_services
from moonlight_steam_sync.art.http import HardStop
from moonlight_steam_sync.art.resolve import MatchCache, Resolver, default_cache_path
from moonlight_steam_sync.art.select import SLOTS, Selector, existing_slot_file
from moonlight_steam_sync.config import Config
from moonlight_steam_sync.reporting import Reporter, match_json, slot_json_value
from moonlight_steam_sync.shortcuts import (
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
    hosts.write_host_cache(deps.resolved_hosts_dir(), config.host, [a.name for a in apps])
    return apps


def _need_host(command: str, config: Config, reporter: Reporter) -> bool:
    if config.host:
        return True
    reporter.error(
        f"{command}: no host configured; pass --host or set `host` in {config.config_path}",
        EXIT_USAGE_OR_CONFIG,
    )
    return False


# ---------------------------------------------------------------------------
# the plan: (host list - ignore) - owned shortcuts (spec 3.7)
# ---------------------------------------------------------------------------


@dataclass
class Adopted:
    """An owned entry already in the library, and its host app if still published."""

    shortcut: Shortcut
    name: str
    app: moonlight.App | None = None


@dataclass
class Addition:
    """A host app with no shortcut yet, and the entry that would be written for it."""

    app: moonlight.App
    shortcut: Shortcut


@dataclass
class Plan:
    """What one run would do, before it does any of it."""

    host: str
    apps: list[moonlight.App]
    ignored: list[moonlight.App] = field(default_factory=list)
    #: Host apps that already have an owned shortcut (by name or by appid).
    present: list[moonlight.App] = field(default_factory=list)
    #: Every owned entry in the library, published or not.
    adopted: list[Adopted] = field(default_factory=list)
    to_add: list[Addition] = field(default_factory=list)
    #: New apps beyond ``--limit``, in host-list order, for the next run.
    pending: list[Addition] = field(default_factory=list)
    limit: int | None = None

    def label(self, app: moonlight.App) -> str:
        if app in self.ignored:
            return LABEL_IGNORED
        if app in self.present:
            return LABEL_ADDED
        return LABEL_NEW

    def header(self) -> str:
        line = (
            f"{self.host}: {len(self.apps)} app(s) published, {len(self.ignored)} ignored, "
            f"{len(self.present)} already in Steam, {len(self.to_add)} to add"
        )
        if self.pending:
            line += f", {len(self.pending)} pending (--limit {self.limit})"
        return line


def build_plan(
    config: Config,
    apps: Sequence[moonlight.App],
    shortcuts_file: ShortcutsFile,
    *,
    limit: int | None = None,
) -> Plan:
    """Diff the host list against the library (spec 3.7).

    Ignore is matched on the Moonlight app name, exact (spec 3.2). Ownership
    is by ``Exe`` (spec 3.3); the name behind an owned entry is recovered
    from its launch options. A host app whose would-be appid is already in
    the file is "the same shortcut" (spec 3.6) even if the name recovery
    disagrees. Duplicate names in the host list count once.
    """
    owned = shortcuts_file.owned(config.exe)
    owned_by_name: dict[str, Shortcut] = {}
    for entry in owned:
        name = entry.moonlight_name(config.launch_options, config.name_suffix)
        owned_by_name.setdefault(name, entry)

    plan = Plan(host=config.host, apps=list(apps), limit=limit)
    ignore = set(config.ignore)
    seen: set[str] = set()
    apps_by_name: dict[str, moonlight.App] = {}
    additions: list[Addition] = []
    for app in apps:
        if app.name in seen:
            continue
        seen.add(app.name)
        apps_by_name[app.name] = app
        if app.name in ignore:
            plan.ignored.append(app)
            continue
        if app.name in owned_by_name:
            plan.present.append(app)
            continue
        shortcut = new_shortcut(config, app.name)
        if shortcuts_file.by_appid(shortcut.appid) is not None:
            plan.present.append(app)
            continue
        additions.append(Addition(app=app, shortcut=shortcut))

    if limit is not None and limit >= 0:
        plan.to_add, plan.pending = additions[:limit], additions[limit:]
    else:
        plan.to_add = additions

    for entry in owned:
        name = entry.moonlight_name(config.launch_options, config.name_suffix)
        plan.adopted.append(Adopted(shortcut=entry, name=name, app=apps_by_name.get(name)))
    return plan


def new_shortcut(config: Config, name: str) -> Shortcut:
    """The entry ``sync`` writes for a Moonlight app (spec 3.6 write policy)."""
    return Shortcut.create(
        app_name=app_name_for(name, config.name_suffix),
        exe=config.exe,
        start_dir=config.start_dir,
        launch_options=render_launch_options(config.launch_options, name),
    )


def art_targets(plan: Plan, grid_dir: Path) -> list[ArtTarget]:
    """Every adopted and every planned shortcut, adopted first (spec 3.6)."""
    targets = [
        ArtTarget(name=item.name, appid=item.shortcut.appid, grid_dir=grid_dir)
        for item in plan.adopted
    ]
    targets.extend(
        ArtTarget(name=item.app.name, appid=item.shortcut.appid, grid_dir=grid_dir)
        for item in plan.to_add
    )
    return targets


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
    wait plus one atomic write. Off the main thread ``signal`` refuses, and
    that is fine: there is nothing to defer there.
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


def commit_shortcuts(
    shortcuts_file: ShortcutsFile,
    *,
    config: Config,
    runner: ProcessRunner | None = None,
    out: TextIO | None = None,
    art_written: bool = False,
) -> Commit:
    """The one path from an in-memory shortcut store to disk (spec 3.6).

    * Nothing to write and no new art -> do nothing at all (no restart).
    * Steam not running -> write; it is not started (the user shut it, and
      the next launch picks the file up).
    * Steam running and ``restart_steam`` false -> raise
      :class:`SteamRunningError` if the file changed (exit 2, the caller's
      art stays on disk); if only art changed, do nothing and let the
      caller say "restart Steam to see it".
    * Steam running and ``restart_steam`` true -> ``steam -shutdown``, wait
      up to 30 s (still up afterwards raises :class:`SteamRunningError`),
      write, ``steam -silent``. One restart per run, however big (spec 3.9
      item 8), and ``SIGINT`` is deferred across it.
    """
    out = out or sys.stdout
    runner = runner or ProcessRunner()
    changed = shortcuts_file.changed
    if not changed and not art_written:
        return Commit()

    running = steam.is_running(runner)
    if running and not config.restart_steam:
        if changed:
            raise SteamRunningError(
                "Steam is running and restart_steam is false, so shortcuts.vdf was not "
                "written (Steam would overwrite it on exit). Artwork already on disk is "
                "kept and picked up on the next restart; quit Steam and rerun, or drop "
                "--no-restart-steam / set restart_steam = true."
            )
        return Commit()

    result = Commit()
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


def _backups(shortcuts_file: ShortcutsFile) -> set[Path]:
    path = shortcuts_file.path
    if path is None or not path.parent.is_dir():
        return set()
    return set(path.parent.glob(f"{path.name}.bak-*"))


def steam_aware_provider(
    config: Config, *, runner: ProcessRunner | None = None
) -> SteamShortcutProvider:
    """The ``art`` command's provider: commits through :func:`commit_shortcuts`."""

    def write(shortcuts_file: ShortcutsFile) -> bool:
        return commit_shortcuts(shortcuts_file, config=config, runner=runner).restarted

    return SteamShortcutProvider(config, writer=write)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


@dataclass
class SyncOptions:
    dry_run: bool = False
    no_art: bool = False
    limit: int | None = None
    retry_missing: bool = False

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> SyncOptions:
        return cls(
            dry_run=bool(getattr(args, "dry_run", False)),
            no_art=bool(getattr(args, "no_art", False)),
            limit=getattr(args, "limit", None),
            retry_missing=bool(getattr(args, "retry_missing", False)),
        )


def _plan_event_fields(plan: Plan) -> dict[str, Any]:
    """``plan`` event fields (spec 3.4.6). PR-1 has no replace/park/duplicate
    machinery yet, so those counts are all zero and every kind is
    ``"shortcut"``."""
    return {
        "host": plan.host,
        "published": len(plan.apps),
        "ignored": len(plan.ignored),
        "present": len(plan.present),
        "to_add": len(plan.to_add),
        "to_replace": 0,
        "to_park": 0,
        "to_unpark": 0,
        "to_remove": 0,
        "pending": len(plan.pending),
        "limit": plan.limit,
        "stream": 0,
        "shortcut": len(plan.apps) - len(plan.ignored),
        "parked": 0,
        "duplicates": {},
    }


def _summary_event_fields(
    plan: Plan,
    summary: RunSummary | None,
    commit: Commit | None,
    *,
    exit_code: int,
    stop_reason: str | None = None,
) -> dict[str, Any]:
    added = len(plan.to_add) if commit is not None and commit.written else 0
    return {
        "added": added,
        "replaced": 0,
        "removed": 0,
        "filled": summary.filled if summary is not None else 0,
        "missing": summary.missing if summary is not None else 0,
        "unmatched": summary.unmatched if summary is not None else [],
        "duplicates": {},
        "pending": len(plan.pending),
        "stopped_early": bool(summary and summary.stopped_early),
        "stop_reason": stop_reason if stop_reason is not None else (
            summary.stop_reason if summary and summary.stopped_early else None
        ),
        "exit": exit_code,
    }


def _title_event_fields(index: int, total: int, result: Any) -> dict[str, Any]:
    if result.skipped:
        return {
            "index": index,
            "total": total,
            "name": result.target.name,
            "kind": "shortcut",
            "appid": result.target.appid,
            "match": None,
            "slots": {},
        }
    return {
        "index": index,
        "total": total,
        "name": result.target.name,
        "kind": "shortcut",
        "appid": result.target.appid,
        "match": match_json(result.match),
        "slots": {key: slot_json_value(outcome.source) for key, outcome in result.slots.items()},
    }


def _commit_event_fields(commit: Commit) -> dict[str, Any]:
    return {
        "written": bool(commit.written),
        "restarted": bool(commit.restarted),
        "backup": commit.backup.name if commit.backup is not None else None,
        "relaunch_error": commit.relaunch_error,
    }


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
    options = SyncOptions.from_args(args)
    if options.limit is not None and options.limit < 0:
        reporter.error("sync: --limit must be zero or more", EXIT_USAGE_OR_CONFIG)
        return EXIT_USAGE_OR_CONFIG

    if not _need_host("sync", config, reporter):
        return EXIT_USAGE_OR_CONFIG
    library = _open_library_or_report("sync", reporter)
    if library is None:
        return EXIT_USAGE_OR_CONFIG
    apps = _list_host_or_report("sync", config, deps, reporter)
    if apps is None:
        return EXIT_MOONLIGHT_UNREACHABLE

    plan = build_plan(config, apps, library.file, limit=options.limit)
    reporter.line(plan.header())
    reporter.event("plan", **_plan_event_fields(plan))

    services: ArtServices | None = None
    if not options.no_art:
        services = deps.services or build_services(
            config, cache_path=deps.cache_path, retry_missing=options.retry_missing
        )
        services.resolver.force = False
        services.resolver.retry_missing = options.retry_missing
        if not config.sgdb_api_key:
            reporter.note(
                "no SteamGridDB API key configured; using Steam's store search and CDN only"
            )

    try:
        if options.dry_run:
            return _dry_run(plan, library, services, reporter)
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
    if not plan.to_add and not plan.adopted:
        reporter.line("nothing to do")
        reporter.event("summary", **_summary_event_fields(plan, None, None, exit_code=EXIT_OK))
        return EXIT_OK

    # In memory only: the file is not written until commit_shortcuts, and
    # not at all if the art phase stops early. Appending now is what lets
    # the art phase patch `icon` on a shortcut that does not exist yet.
    for item in plan.to_add:
        library.file.append(item.shortcut)
    targets = art_targets(plan, library.grid_dir)

    title_counter = itertools.count(1)

    def on_title(result: Any) -> None:
        index = next(title_counter)
        reporter.event("title", **_title_event_fields(index, len(targets), result))

    summary: RunSummary | None = None
    if services is not None:
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
        )
    except SteamRunningError as exc:
        reporter.event(
            "summary",
            **_summary_event_fields(plan, summary, None, exit_code=EXIT_STEAM_RUNNING),
        )
        reporter.error(f"sync: {exc}", EXIT_STEAM_RUNNING)
        return EXIT_STEAM_RUNNING

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
    lines = [
        f"added {added} shortcut(s), adopted {len(plan.adopted)}, "
        f"ignored {len(plan.ignored)}, already present {len(plan.present)}"
    ]
    if plan.to_add and not commit.written:
        lines.append(
            f"{len(plan.to_add)} planned shortcut(s) were not written (see above)"
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


def _dry_run(plan: Plan, library: Library, services: ArtServices | None, reporter: Reporter) -> int:
    """Print the plan: shortcuts to add, and per-slot art source and URL.

    Touches nothing under Steam. With art enabled it does resolve each title
    (that is the only way to know a CDN URL), so the match cache is the one
    thing a dry run writes -- the real run then reuses every resolution.
    Emits ``summary`` (exit 0, or 4 on a hard stop) and no ``title`` events
    (spec 3.4.6): its per-slot lines are human-only, on ``err`` under
    ``--json``.
    """
    resolver = services.resolver if services is not None else None
    selector = services.selector if services is not None else None

    def slot_plan(target: ArtTarget) -> None:
        if resolver is not None and selector is not None:
            _print_slot_plan(target, resolver, selector, reporter)

    try:
        for item in plan.adopted:
            reporter.line(f"  adopted  {item.name} [{item.shortcut.appid}]")
            slot_plan(ArtTarget(item.name, item.shortcut.appid, library.grid_dir))
        for item in plan.to_add:
            reporter.line(f"  add      {item.app.name} [{item.shortcut.appid}]")
            slot_plan(ArtTarget(item.app.name, item.shortcut.appid, library.grid_dir))
        for item in plan.pending:
            reporter.line(f"  pending  {item.app.name} (beyond --limit {plan.limit})")
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
                break
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
    if not _need_host("list", config, reporter):
        return EXIT_USAGE_OR_CONFIG
    library = _open_library_or_report("list", reporter)
    if library is None:
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

    plan = build_plan(config, apps, library.file)
    match_cache = MatchCache(deps.cache_path or default_cache_path())
    other_hosts = _other_hosts_apps(hosts_directory, config.host)
    seen: set[str] = set()
    count = 0
    for app in plan.apps:
        if app.name in seen:
            continue
        seen.add(app.name)
        label = plan.label(app)
        kind = LABEL_IGNORED if label == LABEL_IGNORED else "shortcut"
        entry = match_cache.get(app.name)
        match = match_json(entry) if entry is not None else None
        fuzzy = bool(entry and entry.how.endswith(":fuzzy"))
        same = _same_game_as(app.name, match_cache, other_hosts)
        reporter.line(f"{label:8} {app.name}")
        for item in same:
            reporter.line(f"         same-game-as: {item['name']} ({item['host']})")
        reporter.event(
            "app",
            name=app.name,
            label=label,
            kind=kind,
            match=match,
            fuzzy=fuzzy,
            duplicate_of=None,
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
        plan = build_plan(config, apps, library.file)
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
    by_name: dict[str, Shortcut] = {}
    for entry in owned:
        by_name.setdefault(entry.moonlight_name(config.launch_options, config.name_suffix), entry)

    if getattr(args, "all", False):
        victims = list(owned)
    else:
        wanted = list(getattr(args, "names", []) or [])
        unknown = [name for name in wanted if name not in by_name]
        if unknown:
            print(
                "remove: no owned shortcut named " + ", ".join(repr(n) for n in unknown),
                file=err,
            )
            reporter.error("remove: nothing was touched", EXIT_USAGE_OR_CONFIG)
            return EXIT_USAGE_OR_CONFIG
        victims = [by_name[name] for name in dict.fromkeys(wanted)]

    if not victims:
        reporter.line("nothing to remove")
        reporter.event("summary", added=0, replaced=0, removed=0, exit=EXIT_OK)
        return EXIT_OK

    for entry in victims:
        library.file.remove(entry)
    write_out = reporter.err if reporter.json else reporter.out
    try:
        commit = commit_shortcuts(library.file, config=config, runner=deps.runner, out=write_out)
    except SteamRunningError as exc:
        reporter.error(f"remove: {exc}", EXIT_STEAM_RUNNING)
        return EXIT_STEAM_RUNNING

    deleted = 0
    for entry in victims:
        for path in grid_files_for(library.grid_dir, entry.appid):
            with contextlib.suppress(OSError):
                path.unlink()
                deleted += 1
    reporter.line(commit.describe(library.user.shortcuts_path))
    reporter.event("commit", **_commit_event_fields(commit))
    reporter.line(f"removed {len(victims)} shortcut(s) and {deleted} grid file(s)")
    reporter.event("summary", added=0, replaced=0, removed=len(victims), exit=EXIT_OK)
    return EXIT_OK


__all__ = [
    "Addition",
    "Adopted",
    "Commit",
    "Deps",
    "Library",
    "LibraryProvider",
    "Plan",
    "SyncOptions",
    "art_targets",
    "build_plan",
    "cmd_ignore",
    "cmd_list",
    "cmd_remove",
    "cmd_sync",
    "commit_shortcuts",
    "grid_files_for",
    "ignore_lines",
    "new_shortcut",
    "open_library",
    "patch_icons_from_disk",
    "steam_aware_provider",
    "toml_string",
]
