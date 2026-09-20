"""The ``art``, ``status``, ``search`` and ``match`` subcommands (spec 3.3,
decky spec 3.4.4/3.4.6).

Kept next to the artwork code rather than in ``__main__`` so the entry point
stays a dispatcher: ``__main__`` parses flags and calls :func:`cmd_art` /
:func:`cmd_status` / :func:`cmd_search` / :func:`cmd_match`.

Both commands need the owned shortcuts, which come from the shortcut layer
through :class:`~moonlight_steam_sync.art.apply.TargetProvider`. Callers
(and tests) inject one; without it the commands report the missing wiring and
exit 1 rather than guess.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from moonlight_steam_sync import hosts, steam
from moonlight_steam_sync import version as _pkg_version
from moonlight_steam_sync.art.apply import (
    ArtTarget,
    RunSummary,
    TargetProvider,
    TargetsUnavailable,
    default_target_provider,
    run_art,
    slot_report,
)
from moonlight_steam_sync.art.http import Fetcher, HardStop, HttpError
from moonlight_steam_sync.art.resolve import Match, MatchCache, Resolver, default_cache_path
from moonlight_steam_sync.art.select import SLOTS, Selector
from moonlight_steam_sync.art.sgdb import SgdbClient, steam_appid_from_game
from moonlight_steam_sync.art.steamstore import SteamStoreClient
from moonlight_steam_sync.config import Config, ConfigError, load_owned_apps, owned_apps_steamid3
from moonlight_steam_sync.reporting import Reporter, match_json, slot_json_value

if TYPE_CHECKING:  # ``sync`` imports this module, so only for annotations.
    from moonlight_steam_sync.sync import Deps

# Copied rather than imported from ``__main__`` (which imports this module).
EXIT_OK = 0
EXIT_USAGE_OR_CONFIG = 1
EXIT_STEAM_RUNNING = 2
EXIT_MOONLIGHT_UNREACHABLE = 3
EXIT_NETWORK_STOPPED = 4
EXIT_SIGINT = 130

ProviderFactory = Callable[[Config], TargetProvider]


@dataclass
class ArtServices:
    """Everything the art phase needs, built from a :class:`Config`."""

    fetcher: Fetcher
    sgdb: SgdbClient
    store: SteamStoreClient
    cache: MatchCache
    resolver: Resolver
    selector: Selector


def build_services(
    config: Config,
    *,
    cache_path: Path | None = None,
    fetcher: Fetcher | None = None,
    force: bool = False,
    retry_missing: bool = False,
) -> ArtServices:
    """Wire the clients, cache, resolver and selector together."""
    fetcher = fetcher or Fetcher(interval_ms=config.request_interval_ms)
    sgdb = SgdbClient(api_key=config.sgdb_api_key, fetcher=fetcher)
    store = SteamStoreClient(fetcher=fetcher)
    cache = MatchCache(cache_path or default_cache_path())
    resolver = Resolver(
        cache=cache,
        sgdb=sgdb,
        store=store,
        overrides=config.overrides,
        force=force,
        retry_missing=retry_missing,
    )
    selector = Selector(
        fetcher=fetcher,
        sgdb=sgdb,
        store=store,
        community_fallback=config.sgdb_community_fallback,
    )
    return ArtServices(
        fetcher=fetcher,
        sgdb=sgdb,
        store=store,
        cache=cache,
        resolver=resolver,
        selector=selector,
    )


def _resolve_targets(
    config: Config,
    provider_factory: ProviderFactory | None,
    reporter: Reporter,
) -> tuple[TargetProvider, list[ArtTarget]] | None:
    try:
        provider = (
            provider_factory(config)
            if provider_factory is not None
            else default_target_provider(config)
        )
        return provider, list(provider.targets())
    except TargetsUnavailable as exc:
        # Text unchanged from before --json existed (spec 3.11): always
        # "art: ...", even from `status`, since `reporter.error` always
        # writes to stderr regardless of which command called this.
        reporter.error(f"art: {exc}", EXIT_USAGE_OR_CONFIG)
        return None


def _art_title_event_fields(index: int, total: int, result: Any) -> dict[str, Any]:
    """``title`` event fields for ``art`` (spec 3.4.6): kind comes from
    ``IsHidden`` alone -- ``art`` has no plan to know anything else from."""
    kind = "stream" if result.target.hidden else "shortcut"
    if result.skipped:
        return {
            "index": index,
            "total": total,
            "name": result.target.name,
            "kind": kind,
            "appid": result.target.appid,
            "match": None,
            "slots": {},
        }
    return {
        "index": index,
        "total": total,
        "name": result.target.name,
        "kind": kind,
        "appid": result.target.appid,
        "match": match_json(result.match),
        "slots": {key: slot_json_value(outcome.source) for key, outcome in result.slots.items()},
    }


def cmd_art(
    args: argparse.Namespace,
    config: Config,
    *,
    provider_factory: ProviderFactory | None = None,
    services: ArtServices | None = None,
    cache_path: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``moonlight-steam-sync art`` -- (re)apply art to owned shortcuts."""
    out = out or sys.stdout
    err = err or sys.stderr
    json_mode = bool(getattr(args, "json", False))
    reporter = Reporter(out, err, json=json_mode, command="art", version=_pkg_version())
    reporter.start()
    art_out = reporter.err if reporter.json else reporter.out

    found = _resolve_targets(config, provider_factory, reporter)
    if found is None:
        return EXIT_USAGE_OR_CONFIG
    provider, targets = found

    only = getattr(args, "only", None)
    if only:
        targets = [t for t in targets if t.name == only]
        if not targets:
            reporter.error(f"art: no owned shortcut named {only!r}", EXIT_USAGE_OR_CONFIG)
            return EXIT_USAGE_OR_CONFIG

    force = bool(getattr(args, "force", False))
    retry_missing = bool(getattr(args, "retry_missing", False))
    explain = bool(getattr(args, "explain", False))

    services = services or build_services(
        config, cache_path=cache_path, force=force, retry_missing=retry_missing
    )
    services.resolver.force = force
    services.resolver.retry_missing = retry_missing

    if not config.sgdb_api_key:
        reporter.note("no SteamGridDB API key configured; using Steam's store search and CDN only")

    title_index = [0]

    def on_title(result: Any) -> None:
        title_index[0] += 1
        reporter.title(**_art_title_event_fields(title_index[0], len(targets), result))

    try:
        summary = run_art(
            targets,
            services.resolver,
            services.selector,
            force=force,
            explain=explain,
            provider=provider,
            out=art_out,
            on_title=on_title if json_mode else None,
        )
    except KeyboardInterrupt:
        services.cache.flush()
        interrupted = RunSummary(stopped_early=True, stop_reason="interrupted")
        # summary is always the line before error (spec 3.4.6): emit it
        # first even though there is no real RunSummary to report from.
        reporter.event("summary", **_art_summary_event_fields(interrupted, EXIT_SIGINT))
        reporter.error("interrupted; resume with the same command", EXIT_SIGINT)
        return EXIT_SIGINT

    for line in summary.lines():
        reporter.line(line)
    interrupted_message: str | None = None
    if summary.stop_reason == "interrupted":
        # run_art catches the Ctrl-C around each title, so this -- not the
        # handler above -- is the branch a real SIGINT takes (spec 3.9.5).
        # The JSON `error` event is deferred past `summary` below (spec
        # 3.4.6: error is always last); the human text prints here, same as
        # always, since it is not part of summary.lines().
        interrupted_message = "interrupted; resume with the same command"
        print(interrupted_message, file=err)
    if summary.stopped_early:
        # The grid files written so far are durable; the icon patches are
        # re-derived from them on the next run, so nothing is lost by not
        # writing shortcuts.vdf now (spec 3.9 item 4: the write comes last).
        exit_code = _exit_code(summary)
        reporter.event("summary", **_art_summary_event_fields(summary, exit_code))
        reporter.event(
            "error",
            exit=exit_code,
            message=interrupted_message if interrupted_message is not None else summary.stop_reason,
        )
        return exit_code

    # One atomic shortcuts.vdf write for the icon patches (spec 3.6). The
    # provider's writer decides how to get Steam out of the way; a refusal is
    # exit 2, with the art already on disk and picked up by the next run.
    try:
        provider.commit()
    except steam.SteamRunningError as exc:
        reporter.event("summary", **_art_summary_event_fields(summary, EXIT_STEAM_RUNNING))
        reporter.error(f"art: {exc}", EXIT_STEAM_RUNNING)
        return EXIT_STEAM_RUNNING
    except KeyboardInterrupt:
        reporter.event("summary", **_art_summary_event_fields(summary, EXIT_SIGINT))
        reporter.error(
            "interrupted while writing shortcuts.vdf; rerun the same command", EXIT_SIGINT
        )
        return EXIT_SIGINT
    if summary.written and not _restarted(provider):
        print("restart Steam to see the new artwork", file=err)
        reporter.event("note", message="restart Steam to see the new artwork")
    exit_code = _exit_code(summary)
    reporter.event("commit", **_commit_event_fields(provider))
    reporter.event("summary", **_art_summary_event_fields(summary, exit_code))
    return exit_code


def _restarted(provider: TargetProvider) -> bool:
    """Whether the provider's commit already bounced Steam (the sync writer says)."""
    return bool(getattr(provider, "restarted_steam", False))


def _commit_event_fields(provider: TargetProvider) -> dict[str, Any]:
    """``art``'s ``commit`` event (spec 3.4.6): same key set as ``sync``'s
    -- ``written``/``restarted``/``backup``/``relaunch_error`` -- read off
    the provider's :class:`~moonlight_steam_sync.art.apply.CommitResult`
    when it has one (the real, Steam-aware provider always does)."""
    last_commit = getattr(provider, "last_commit", None)
    if last_commit is None:
        return {"written": False, "restarted": False, "backup": None, "relaunch_error": ""}
    backup = getattr(last_commit, "backup", None)
    return {
        "written": bool(getattr(last_commit, "written", False)),
        "restarted": bool(getattr(last_commit, "restarted", False)),
        "backup": backup.name if backup is not None else None,
        "relaunch_error": getattr(last_commit, "relaunch_error", "") or "",
    }


def _art_summary_event_fields(summary: RunSummary, exit_code: int) -> dict[str, Any]:
    """``art``'s ``summary`` event (spec 3.4.6): the same key set ``sync``
    uses, with ``added``/``replaced``/``removed`` always 0 -- ``art`` never
    adds, replaces or removes a shortcut."""
    return {
        "added": 0,
        "replaced": 0,
        "removed": 0,
        "filled": summary.filled,
        "missing": summary.missing,
        "unmatched": summary.unmatched,
        "duplicates": {},
        "pending": 0,
        "stopped_early": summary.stopped_early,
        "stop_reason": summary.stop_reason or None,
        "exit": exit_code,
    }


def _exit_code(summary: RunSummary) -> int:
    if summary.stopped_early:
        return EXIT_SIGINT if summary.stop_reason == "interrupted" else EXIT_NETWORK_STOPPED
    # "some titles or slots had no match" is exit 0 by design (spec 3.3/7).
    return EXIT_OK


def cmd_status(
    args: argparse.Namespace,
    config: Config,
    *,
    provider_factory: ProviderFactory | None = None,
    cache_path: Path | None = None,
    hosts_dir: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``moonlight-steam-sync status`` -- which art slots each shortcut has.

    Never runs ``moonlight list`` (spec 3.4.6/3.12): ``published`` comes
    from the resolved host's per-host list cache, read (never refreshed)
    from ``hosts_dir`` (or ``cache_path``'s sibling ``hosts/`` when unset).
    """
    out = out or sys.stdout
    err = err or sys.stderr
    json_mode = bool(getattr(args, "json", False))
    reporter = Reporter(out, err, json=json_mode, command="status", version=_pkg_version())
    reporter.start()

    owned_apps_flag = getattr(args, "owned_apps", None)
    if owned_apps_flag:
        owned_path = Path(owned_apps_flag)
        try:
            load_owned_apps(owned_path)
        except ConfigError as exc:
            reporter.error(f"status: {exc}", EXIT_USAGE_OR_CONFIG)
            return EXIT_USAGE_OR_CONFIG

    found = _resolve_targets(config, provider_factory, reporter)
    if found is None:
        return EXIT_USAGE_OR_CONFIG
    _provider, targets = found

    if owned_apps_flag:
        wanted_steamid3 = owned_apps_steamid3(Path(owned_apps_flag))
        if wanted_steamid3 is not None:
            try:
                user = steam.pick_user(steam.find_steam_root())
            except steam.SteamError as exc:
                reporter.error(f"status: {exc}", EXIT_USAGE_OR_CONFIG)
                return EXIT_USAGE_OR_CONFIG
            if wanted_steamid3 != user.steamid3:
                reporter.error(
                    f"status: owned-apps file is for Steam user {wanted_steamid3} but sync "
                    f"would write to user {user.steamid3}",
                    EXIT_USAGE_OR_CONFIG,
                )
                return EXIT_USAGE_OR_CONFIG

    if not targets:
        reporter.line("no owned shortcuts")
        reporter.event("end", count=0)
        return EXIT_OK

    resolved_hosts_dir = (
        hosts_dir
        if hosts_dir is not None
        else (cache_path.parent if cache_path is not None else default_cache_path().parent)
        / "hosts"
    )
    match_cache = MatchCache(cache_path or default_cache_path())
    host_cache = hosts.read_host_cache(resolved_hosts_dir, config.host) if config.host else None
    if host_cache is None:
        # New in this PR (the host cache did not exist in v0.2.0): only_json
        # keeps plain `status` byte-identical to v0.2.0 (spec 3.11).
        reporter.note(
            f"no cached app list for host {config.host or '(none)'}; `published` is unknown; "
            f"run `list --host {config.host or 'NAME'}`",
            only_json=True,
        )
    published_names = set(host_cache.apps) if host_cache is not None else set()

    complete = 0
    entry_count = 0
    for target in sorted(targets, key=lambda t: t.name.casefold()):
        report = slot_report(target.grid_dir, target.appid)
        if all(value != "-" for value in report.values()):
            complete += 1
        slots = " ".join(f"{slot.key}={report[slot.key]}" for slot in SLOTS)
        reporter.line(f"{target.name} [{target.appid}]: {slots}")
        match_entry = match_cache.get(target.name)
        reporter.event(
            "entry",
            name=target.name,
            app_name=target.app_name,
            appid=target.appid,
            hidden=target.hidden,
            parked=False,
            published=target.name in published_names,
            client=False,
            match=match_json(match_entry),
            slots={key: (None if value == "-" else value) for key, value in report.items()},
            stale_art=bool(match_entry is not None and match_entry.stale_art),
            cached=host_cache is not None,
            cached_when=host_cache.when if host_cache is not None else None,
        )
        entry_count += 1
    reporter.line(f"{len(targets)} shortcut(s), {complete} with every slot filled")
    reporter.event("end", count=entry_count)
    return EXIT_OK


# ---------------------------------------------------------------------------
# search (spec 3.4.6, 3.4.8)
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class _Candidate:
    source: str
    id: int
    name: str
    verified: bool
    steam_appid: int | None


def cmd_search(
    args: argparse.Namespace,
    config: Config,
    *,
    services: ArtServices | None = None,
    cache_path: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``moonlight-steam-sync search "term"`` (spec 3.4.6/3.4.8).

    SGDB (when a key is configured) then Steam's store, de-duplicated by
    Steam appid (SGDB wins), at most 10 candidates per source.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    json_mode = bool(getattr(args, "json", False))
    reporter = Reporter(out, err, json=json_mode, command="search", version=_pkg_version())
    reporter.start()

    owned: dict[int, str] = {}
    owned_apps_flag = getattr(args, "owned_apps", None)
    if owned_apps_flag:
        try:
            owned = load_owned_apps(Path(owned_apps_flag))
        except ConfigError as exc:
            reporter.error(f"search: {exc}", EXIT_USAGE_OR_CONFIG)
            return EXIT_USAGE_OR_CONFIG

    term = args.term
    services = services or build_services(config, cache_path=cache_path)

    if not config.sgdb_api_key:
        reporter.note("no SteamGridDB API key configured; using Steam's store search and CDN only")

    candidates: list[_Candidate] = []
    try:
        if config.sgdb_api_key:
            try:
                sgdb_results = services.sgdb.search(term)
            except HardStop:
                raise
            except HttpError:
                sgdb_results = []
            for item in sgdb_results[:10]:
                sgdb_id = _as_int(item.get("id"))
                if sgdb_id is None:
                    continue
                steam_appid: int | None = None
                try:
                    game = services.sgdb.game(sgdb_id)
                    steam_appid = steam_appid_from_game(game)
                except HardStop:
                    raise
                except HttpError:
                    steam_appid = None
                candidates.append(
                    _Candidate(
                        source="sgdb",
                        id=sgdb_id,
                        name=str(item.get("name") or ""),
                        verified=bool(item.get("verified")),
                        steam_appid=steam_appid,
                    )
                )

        sgdb_steam_ids = {c.steam_appid for c in candidates if c.steam_appid is not None}
        try:
            store_results = services.store.search(term)
        except HardStop:
            raise
        except HttpError:
            store_results = []
        for item in store_results[:10]:
            appid = _as_int(item.get("id"))
            if appid is None or appid in sgdb_steam_ids:
                continue
            candidates.append(
                _Candidate(
                    source="steam",
                    id=appid,
                    name=str(item.get("name") or ""),
                    verified=True,
                    steam_appid=appid,
                )
            )
    except HardStop as exc:
        reporter.error(f"search: {exc}", EXIT_NETWORK_STOPPED)
        return EXIT_NETWORK_STOPPED

    for candidate in candidates:
        owned_flag = candidate.steam_appid is not None and candidate.steam_appid in owned
        tags = []
        if candidate.verified:
            tags.append("verified")
        if owned_flag:
            tags.append("owned")
        suffix = f" [{', '.join(tags)}]" if tags else ""
        reporter.line(f"{candidate.source:5} {candidate.id:>9}  {candidate.name}{suffix}")
        reporter.event(
            "candidate",
            source=candidate.source,
            id=candidate.id,
            name=candidate.name,
            verified=candidate.verified,
            owned=owned_flag,
            steam_appid=candidate.steam_appid,
        )

    if candidates:
        reporter.line(f"{len(candidates)} candidate(s)")
    else:
        reporter.line(f'no candidates for "{term}"')
    reporter.event("end")
    return EXIT_OK


# ---------------------------------------------------------------------------
# match (decky spec 3.4.4, 3.4.6, 3.4.8)
# ---------------------------------------------------------------------------


#: How many SteamGridDB autocomplete results ``match --steam`` checks for
#: one whose Steam release is the pinned appid -- the same cap ``search``
#: applies per source.
MATCH_SGDB_CANDIDATES = 10


def _sgdb_id_for_steam_appid(
    services: ArtServices, name: str, steam_appid: int
) -> tuple[int | None, str | None]:
    """``match --steam``'s SteamGridDB id and name (decky spec 3.4.4).

    There is no Steam-appid -> SGDB-id lookup, so the id is re-found by
    ``sgdb.search(name)`` filtered to the results whose Steam release
    (``games/id/{id}?platformdata=steam``) is ``steam_appid``. ``(None,
    None)`` without a key, with no such result, or when the search itself
    fails (a transient failure only costs the community-art fallback);
    :class:`HardStop` propagates.
    """
    if not services.sgdb.enabled:
        return None, None
    try:
        results = services.sgdb.search(name)
    except HardStop:
        raise
    except HttpError:
        return None, None
    for item in results[:MATCH_SGDB_CANDIDATES]:
        sgdb_id = _as_int(item.get("id"))
        if sgdb_id is None:
            continue
        try:
            game = services.sgdb.game(sgdb_id)
        except HardStop:
            raise
        except HttpError:
            continue
        if steam_appid_from_game(game) == steam_appid:
            return sgdb_id, (str(item.get("name") or "") or None)
    return None, None


def _steam_appid_for_sgdb_id(
    services: ArtServices, sgdb_id: int
) -> tuple[int | None, str | None]:
    """``match --sgdb``'s Steam appid and the game's SteamGridDB name, from
    ``games/id/{id}?platformdata=steam`` when a key is configured (decky
    spec 3.4.4); ``(None, None)`` without one or when the call fails."""
    if not services.sgdb.enabled:
        return None, None
    try:
        game = services.sgdb.game(sgdb_id)
    except HardStop:
        raise
    except HttpError:
        return None, None
    return steam_appid_from_game(game), (str(game.get("name") or "") or None)


def override_line(
    name: str, *, steam_appid: int | None = None, sgdb_id: int | None = None
) -> str:
    """The ``[overrides]`` line equivalent to a pin (decky spec 3.4.4).

    ``"Hades II" = { steam = 1145350 }`` for ``--steam``, ``{ sgdb = N }``
    for ``--sgdb``, and ``{}`` for ``--none`` -- an empty override table is
    the config form of "no match". The key is quoted by
    :func:`moonlight_steam_sync.sync.toml_string`, the same quoting ``ignore
    --all`` prints its TOML with.
    """
    # ``sync`` imports this module, so its helpers are imported at call time.
    from moonlight_steam_sync.sync import toml_string

    key = toml_string(name)
    if steam_appid is not None:
        return f"{key} = {{ steam = {steam_appid} }}"
    if sgdb_id is not None:
        return f"{key} = {{ sgdb = {sgdb_id} }}"
    return f"{key} = {{}}"


def _id_text(value: int | None) -> str:
    return str(value) if value is not None else "none"


def cmd_match(
    args: argparse.Namespace,
    config: Config,
    *,
    deps: Deps | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``moonlight-steam-sync match "Name" (--steam N | --sgdb N | --none | --unpin)``.

    Cache surgery on ``matches.json`` (decky spec 3.4.4): writes a
    ``how == "pinned"`` entry for the title -- the one cache entry ``art
    --force`` and ``--retry-missing`` never overwrite -- or, with
    ``--unpin``, deletes the title's entry so the next run re-resolves it.
    A ``[overrides]`` entry in ``config.toml`` still wins over a pin, and
    the equivalent override line is printed so a CLI user can make the pin
    permanent by hand (the tool never writes ``config.toml``).

    When the title has an owned shortcut, its art belongs to the old match:

    * by default the title's grid files are deleted and its ``icon`` field
      cleared, through :func:`moonlight_steam_sync.sync.commit_shortcuts`
      like ``remove`` -- so under a live Steam without ``restart_steam``
      it exits 2 and touches nothing, not even the pin. Like ``remove``,
      that refusal needs a ``shortcuts.vdf`` change to refuse: when the
      ``icon`` field is already empty, ``commit_shortcuts()`` has nothing
      to write and never consults Steam, so the pin and the grid-file
      deletion go ahead with Steam up (harmless; ``art --force`` replaces
      grid files under a running Steam too);
    * with ``--defer-art`` only the cache is written and the entry records
      ``stale_art: true``, for the next ``sync`` to act on. ``--unpin
      --defer-art`` has no entry left to flag, so it leaves an ``unpinned``
      placeholder that carries the flag (the resolver treats it as a miss
      and carries the flag onto whatever the next run resolves).

    A name that is neither owned nor in the resolved host's cached app list
    (the per-host list cache ``sync``/``list`` write; ``match`` never runs
    ``moonlight``) is exit 1 unless ``--force-name`` (a pin for a title the
    host will publish later).
    """
    # ``sync`` imports this module, so the orchestration helpers (the one
    # shortcuts.vdf writer among them) are imported at call time.
    from moonlight_steam_sync import sync

    deps = deps or sync.Deps()
    out = out or sys.stdout
    err = err or sys.stderr
    json_mode = bool(getattr(args, "json", False))
    reporter = Reporter(out, err, json=json_mode, command="match", version=_pkg_version())
    reporter.start()

    name: str = args.name
    steam_flag = getattr(args, "steam", None)
    sgdb_flag = getattr(args, "sgdb", None)
    unpin = bool(getattr(args, "unpin", False))
    defer_art = bool(getattr(args, "defer_art", False))
    force_name = bool(getattr(args, "force_name", False))

    for flag, value in (("--steam", steam_flag), ("--sgdb", sgdb_flag)):
        if value is not None and value <= 0:
            reporter.error(f"match: {flag} must be a positive id", EXIT_USAGE_OR_CONFIG)
            return EXIT_USAGE_OR_CONFIG

    library = sync._open_library_or_report("match", reporter)
    if library is None:
        return EXIT_USAGE_OR_CONFIG
    owned_by_name: dict[str, Any] = {}
    for entry in library.file.owned(config.exe):
        owned_by_name.setdefault(
            entry.moonlight_name(config.launch_options, config.name_suffix), entry
        )
    shortcut = owned_by_name.get(name)

    if shortcut is None and not force_name:
        host_cache = (
            hosts.read_host_cache(deps.resolved_hosts_dir(), config.host) if config.host else None
        )
        if host_cache is None or name not in host_cache.apps:
            if not config.host:
                why = "no host is configured"
            elif host_cache is None:
                why = (
                    f"there is no cached app list for host {config.host} "
                    f"(run `list --host {config.host}`)"
                )
            else:
                why = f"{config.host}'s cached app list does not have it"
            reporter.error(
                f"match: no owned shortcut named {name!r}, and {why}; "
                "pass --force-name to pin it anyway",
                EXIT_USAGE_OR_CONFIG,
            )
            return EXIT_USAGE_OR_CONFIG

    services = deps.services or build_services(config, cache_path=deps.cache_path)
    cache = services.cache

    pin: Match | None = None
    line: str | None = None
    if not unpin:
        try:
            if steam_flag is not None:
                sgdb_id, sgdb_name = _sgdb_id_for_steam_appid(services, name, steam_flag)
                pin = Match(
                    name=name,
                    steam_appid=steam_flag,
                    sgdb_id=sgdb_id,
                    matched_name=sgdb_name or name,
                )
                line = override_line(name, steam_appid=steam_flag)
            elif sgdb_flag is not None:
                steam_appid, sgdb_name = _steam_appid_for_sgdb_id(services, sgdb_flag)
                pin = Match(
                    name=name,
                    steam_appid=steam_appid,
                    sgdb_id=sgdb_flag,
                    matched_name=sgdb_name or name,
                )
                line = override_line(name, sgdb_id=sgdb_flag)
            else:  # --none
                pin = Match(name=name)
                line = override_line(name)
        except HardStop as exc:
            reporter.error(f"match: {exc}", EXIT_NETWORK_STOPPED)
            return EXIT_NETWORK_STOPPED

    immediate = shortcut is not None and not defer_art
    commit = None
    if immediate:
        # The icon field lives in shortcuts.vdf, so clearing it goes through
        # the one writer, exactly like `remove`; a refusal (exit 2) happens
        # before the pin or any grid file is touched.
        if shortcut.icon:
            shortcut.icon = ""
        write_out = reporter.err if reporter.json else reporter.out
        try:
            commit = sync.commit_shortcuts(
                library.file, config=config, runner=deps.runner, out=write_out
            )
        except steam.SteamRunningError as exc:
            reporter.error(f"match: {exc}", EXIT_STEAM_RUNNING)
            return EXIT_STEAM_RUNNING
        # The same `commit` event `remove` emits for the same sequence, so a
        # --json consumer can see `restarted` / `relaunch_error` here too.
        reporter.event("commit", **sync._commit_event_fields(commit))

    deferred = shortcut is not None and not immediate
    if pin is not None:
        cache.pin(pin)
        if deferred:
            cache.mark_stale_art(name)
    else:
        cache.unpin(name, stale_art=deferred)

    deleted = 0
    if immediate:
        for path in sync.grid_files_for(library.grid_dir, shortcut.appid):
            with contextlib.suppress(OSError):
                path.unlink()
                deleted += 1

    if pin is None:
        reporter.line(f'unpinned "{name}"; the next run re-resolves it')
    elif pin.found:
        reporter.line(
            f'pinned "{name}": steam {_id_text(pin.steam_appid)}, sgdb {_id_text(pin.sgdb_id)}'
        )
    else:
        reporter.line(f'pinned "{name}": no match')
    if line is not None:
        reporter.line(line)
    reporter.event(
        "pinned",
        name=name,
        steam_appid=pin.steam_appid if pin is not None else None,
        sgdb_id=pin.sgdb_id if pin is not None else None,
        override_line=line,
    )

    if immediate:
        assert commit is not None
        reporter.line(commit.describe(library.user.shortcuts_path))
        reporter.line(f"deleted {deleted} grid file(s)")
    elif deferred:
        message = f'art for "{name}" will be re-fetched on the next sync'
        reporter.line(message)
        reporter.event("note", message=message)
    return EXIT_OK


__all__ = [
    "ArtServices",
    "build_services",
    "cmd_art",
    "cmd_match",
    "cmd_search",
    "cmd_status",
    "override_line",
]
