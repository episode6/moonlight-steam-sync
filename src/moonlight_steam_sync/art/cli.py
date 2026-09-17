"""The ``art`` and ``status`` subcommands (spec 3.3).

Kept next to the artwork code rather than in ``__main__`` so the entry point
stays a dispatcher: ``__main__`` parses flags and calls :func:`cmd_art` /
:func:`cmd_status`.

Both commands need the owned shortcuts, which come from the shortcut layer
through :class:`~moonlight_steam_sync.art.apply.TargetProvider`. Callers
(and tests) inject one; without it the commands report the missing wiring and
exit 1 rather than guess.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from moonlight_steam_sync import steam
from moonlight_steam_sync.art.apply import (
    ArtTarget,
    RunSummary,
    TargetProvider,
    TargetsUnavailable,
    default_target_provider,
    run_art,
    slot_report,
)
from moonlight_steam_sync.art.http import Fetcher
from moonlight_steam_sync.art.resolve import MatchCache, Resolver, default_cache_path
from moonlight_steam_sync.art.select import SLOTS, Selector
from moonlight_steam_sync.art.sgdb import SgdbClient
from moonlight_steam_sync.art.steamstore import SteamStoreClient
from moonlight_steam_sync.config import Config

# Copied rather than imported from ``__main__`` (which imports this module).
EXIT_OK = 0
EXIT_USAGE_OR_CONFIG = 1
EXIT_STEAM_RUNNING = 2
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
    out: TextIO,
) -> tuple[TargetProvider, list[ArtTarget]] | None:
    try:
        provider = (
            provider_factory(config)
            if provider_factory is not None
            else default_target_provider(config)
        )
        return provider, list(provider.targets())
    except TargetsUnavailable as exc:
        print(f"art: {exc}", file=out)
        return None


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

    found = _resolve_targets(config, provider_factory, err)
    if found is None:
        return EXIT_USAGE_OR_CONFIG
    provider, targets = found

    only = getattr(args, "only", None)
    if only:
        targets = [t for t in targets if t.name == only]
        if not targets:
            print(f"art: no owned shortcut named {only!r}", file=err)
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
        print(
            "note: no SteamGridDB API key configured; using Steam's store search and CDN only",
            file=err,
        )

    try:
        summary = run_art(
            targets,
            services.resolver,
            services.selector,
            force=force,
            explain=explain,
            provider=provider,
            out=out,
        )
    except KeyboardInterrupt:
        services.cache.flush()
        print("interrupted; resume with the same command", file=err)
        return EXIT_SIGINT

    for line in summary.lines():
        print(line, file=out)
    if summary.stop_reason == "interrupted":
        # run_art catches the Ctrl-C around each title, so this -- not the
        # handler above -- is the branch a real SIGINT takes (spec 3.9.5).
        print("interrupted; resume with the same command", file=err)
    if summary.stopped_early:
        # The grid files written so far are durable; the icon patches are
        # re-derived from them on the next run, so nothing is lost by not
        # writing shortcuts.vdf now (spec 3.9 item 4: the write comes last).
        return _exit_code(summary)

    # One atomic shortcuts.vdf write for the icon patches (spec 3.6). The
    # provider's writer decides how to get Steam out of the way; a refusal is
    # exit 2, with the art already on disk and picked up by the next run.
    try:
        provider.commit()
    except steam.SteamRunningError as exc:
        print(f"art: {exc}", file=err)
        return EXIT_STEAM_RUNNING
    except KeyboardInterrupt:
        print("interrupted while writing shortcuts.vdf; rerun the same command", file=err)
        return EXIT_SIGINT
    if summary.written and not _restarted(provider):
        print("restart Steam to see the new artwork", file=err)
    return _exit_code(summary)


def _restarted(provider: TargetProvider) -> bool:
    """Whether the provider's commit already bounced Steam (the sync writer says)."""
    return bool(getattr(provider, "restarted_steam", False))


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
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """``moonlight-steam-sync status`` -- which art slots each shortcut has."""
    del args
    out = out or sys.stdout
    err = err or sys.stderr

    found = _resolve_targets(config, provider_factory, err)
    if found is None:
        return EXIT_USAGE_OR_CONFIG
    _provider, targets = found

    if not targets:
        print("no owned shortcuts", file=out)
        return EXIT_OK

    complete = 0
    for target in sorted(targets, key=lambda t: t.name.casefold()):
        report = slot_report(target.grid_dir, target.appid)
        if all(value != "-" for value in report.values()):
            complete += 1
        slots = " ".join(f"{slot.key}={report[slot.key]}" for slot in SLOTS)
        print(f"{target.name} [{target.appid}]: {slots}", file=out)
    print(f"{len(targets)} shortcut(s), {complete} with every slot filled", file=out)
    return EXIT_OK


__all__ = [
    "ArtServices",
    "build_services",
    "cmd_art",
    "cmd_status",
]
