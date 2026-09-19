"""argparse entry point: subcommands, exit codes, SIGINT handling.

Every subcommand in spec 3.3 is wired: ``doctor``, ``launch``, ``art`` and
``status`` to their own modules, and ``sync`` / ``list`` / ``ignore`` /
``remove`` to the orchestration in :mod:`moonlight_steam_sync.sync`.

SIGINT: Python's default ``KeyboardInterrupt`` is what the long-running
commands rely on. ``run_art`` catches it around each title so the match
cache is flushed and an in-flight ``.part`` is abandoned, ``sync`` then
skips the ``shortcuts.vdf`` write, and whatever escapes is caught here and
turned into exit 130 with the "resume with the same command" line (spec
3.9 item 5). The one window where Ctrl-C must *not* land -- Steam down,
file not yet written -- defers the signal itself (``sync.sigint_deferred``).
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from importlib import metadata
from pathlib import Path
from typing import TextIO

from moonlight_steam_sync import __version__, hosts, moonlight, steam, sync
from moonlight_steam_sync.art.cli import ProviderFactory, cmd_art, cmd_search, cmd_status
from moonlight_steam_sync.config import (
    DEFAULT_KEY_FILE,
    ConfigError,
    load_config,
    load_owned_apps,
    owned_apps_steamid3,
    toml_host,
)
from moonlight_steam_sync.reporting import Reporter

# Exit codes (spec 3.3).
EXIT_OK = 0
EXIT_USAGE_OR_CONFIG = 1
EXIT_STEAM_RUNNING = 2
EXIT_MOONLIGHT_UNREACHABLE = 3
EXIT_NETWORK_STOPPED = 4
EXIT_SIGINT = 130


def _version() -> str:
    """The version string ``--version`` prints.

    ``pyproject.toml`` declares ``version`` as ``dynamic`` and sourced from
    ``moonlight_steam_sync.__version__`` (see ``[tool.setuptools.dynamic]``),
    so that attribute is the one place the number is written down. When the
    tool is installed as a package (``pip install .``, an editable checkout,
    or a wheel), ``importlib.metadata`` reads the *installed* metadata --
    the same value, but resolved the way any other installed distribution's
    version is, so ``--version`` matches ``pip show``. The release zipapp
    (spec 3.1) is never pip-installed -- it is a bare ``.pyz`` with no
    ``dist-info`` alongside it -- so metadata lookup fails there and this
    falls back to the literal ``__version__`` the module was built with.
    """
    try:
        return metadata.version("moonlight-steam-sync")
    except metadata.PackageNotFoundError:
        return __version__


def _add_common_host_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", help="Moonlight host name (overrides config)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moonlight-steam-sync",
        description="Sync a Moonlight host's game list into Steam shortcuts, with artwork.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    parser.add_argument(
        "--json",
        action="store_true",
        help="one JSON object per stdout line; human progress moves to stderr (spec 3.4.6)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync_p = sub.add_parser("sync", help="add missing shortcuts and their artwork")
    _add_common_host_flag(sync_p)
    sync_p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan (shortcuts to add, per-slot art source and URL) and write nothing",
    )
    sync_p.add_argument(
        "--no-art", action="store_true", help="skip the art phase and go straight to the write"
    )
    sync_p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="add at most N new shortcuts this run (host-list order); the rest wait",
    )
    sync_p.add_argument(
        "--retry-missing",
        action="store_true",
        help="re-query titles and slots whose 'nothing found' result is cached",
    )
    sync_p.add_argument(
        "--no-restart-steam",
        action="store_true",
        help="never stop or start Steam; exit 2 instead of writing while it runs",
    )

    art_p = sub.add_parser("art", help="(re)apply art to owned shortcuts")
    art_p.add_argument(
        "--force",
        action="store_true",
        help=(
            "re-fetch every slot, ignoring the match cache, and replace the files "
            "already in grid/ (a slot ends up with exactly one file)"
        ),
    )
    art_p.add_argument(
        "--retry-missing",
        action="store_true",
        help="re-query titles and slots whose 'nothing found' result is cached",
    )
    art_p.add_argument(
        "--only", metavar="NAME", help="dress just this Moonlight app name"
    )
    art_p.add_argument(
        "--explain",
        action="store_true",
        help="print the match chain and the URL tried for every slot",
    )

    list_p = sub.add_parser("list", help="what the host publishes: added / ignored / new")
    _add_common_host_flag(list_p)
    list_p.add_argument(
        "--cached",
        action="store_true",
        help="serve the per-host list cache instead of running moonlight (spec 3.12)",
    )

    status_p = sub.add_parser(
        "status", help="owned shortcuts and which art slots each has on disk"
    )
    _add_common_host_flag(status_p)
    status_p.add_argument(
        "--owned-apps", metavar="PATH", help="a plugin-written owned-apps JSON file (spec 3.4.1)"
    )

    search_p = sub.add_parser("search", help="find a Steam/SteamGridDB candidate for a title")
    search_p.add_argument("term")
    search_p.add_argument(
        "--owned-apps", metavar="PATH", help="a plugin-written owned-apps JSON file (spec 3.4.1)"
    )

    host_p = sub.add_parser("host", help="show or change the active Moonlight host (spec 3.12)")
    host_sub = host_p.add_subparsers(dest="host_action", required=True)
    host_show_p = host_sub.add_parser("show", help="print the resolved host and where it came from")
    _add_common_host_flag(host_show_p)
    host_set_p = host_sub.add_parser("set", help="write the active-host state file")
    host_set_p.add_argument("name")
    host_sub.add_parser("clear", help="remove the active-host state file")

    ignore_p = sub.add_parser(
        "ignore", help="print TOML ignore = [...] lines to paste into config"
    )
    _add_common_host_flag(ignore_p)
    ignore_group = ignore_p.add_mutually_exclusive_group(required=True)
    ignore_group.add_argument(
        "--all", action="store_true", help="every host app that has no shortcut yet"
    )
    ignore_group.add_argument("names", nargs="*", default=[], metavar="NAME")

    remove_p = sub.add_parser("remove", help="delete owned shortcuts and their grid files")
    remove_group = remove_p.add_mutually_exclusive_group(required=True)
    remove_group.add_argument("--all", action="store_true", help="every owned shortcut")
    remove_group.add_argument("names", nargs="*", default=[], metavar="NAME")

    launch_p = sub.add_parser("launch", help='exec moonlight stream <host> "Name"')
    _add_common_host_flag(launch_p)
    launch_p.add_argument("name")
    launch_p.add_argument("extra", nargs=argparse.REMAINDER)

    doctor_p = sub.add_parser("doctor", help="report the environment this tool will run in")
    _add_common_host_flag(doctor_p)
    doctor_p.add_argument(
        "--owned-apps", metavar="PATH", help="a plugin-written owned-apps JSON file (spec 3.4.1)"
    )

    return parser


def _steam_running() -> bool:
    return steam.is_running()


def _steam_root() -> Path | None:
    try:
        return steam.find_steam_root()
    except steam.SteamNotFoundError:
        return None


def _steam_user_line(root: Path | None) -> str:
    """One line describing which Steam account `sync` would write to."""
    if root is None:
        return "steam user:    unknown (no Steam directory)"
    try:
        user = steam.pick_user(root)
    except steam.SteamError as exc:
        return f"steam user:    undecided ({exc})"
    label = user.account_name or user.persona_name
    who = f"{user.steamid3}{f' ({label})' if label else ''}"
    return f"steam user:    {who} -> {user.shortcuts_path}"


def _session_kind() -> str:
    """``gamescope``/``desktop``/``unknown`` (spec 3.4.7, the PR-0 probe)."""
    gamescope_wayland = os.environ.get("GAMESCOPE_WAYLAND_DISPLAY", "")
    xdg_desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if gamescope_wayland or "gamescope" in xdg_desktop.casefold():
        return "gamescope"
    if xdg_desktop or os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
        return "desktop"
    return "unknown"


def _session_line() -> str:
    gamescope_wayland = os.environ.get("GAMESCOPE_WAYLAND_DISPLAY", "")
    xdg_desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    return (
        f"session:       {_session_kind()} (XDG_CURRENT_DESKTOP={xdg_desktop or '(unset)'}, "
        f"GAMESCOPE_WAYLAND_DISPLAY={gamescope_wayland or '(unset)'})"
    )


def cmd_doctor(
    args: argparse.Namespace, *, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    """Print the environment report spec 3.3 promises: steam dir, user,
    python, moonlight path, key present?, steam running?

    Spec 3.4.7 appends three more lines unconditionally (``session:``,
    ``active host:``, ``cached hosts:``) and an ``owned-apps file:`` line
    when ``--owned-apps`` is given; ``doctor`` never exits 1 over a bad
    file, since it is a diagnostic.

    Under ``--json`` (spec 3.4.6): the report moves to stderr (one line at a
    time -- the same bytes as the single joined ``print`` below, just split
    across calls) and stdout carries ``start`` then
    ``{"event":"end","count":N}``.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    json_mode = bool(getattr(args, "json", False))
    reporter = Reporter(out, err, json=json_mode, command="doctor", version=_version())
    reporter.start()
    cfg = load_config(args)

    lines = [
        f"python:        {platform.python_version()} ({sys.executable})",
        f"platform:      {platform.platform()}",
    ]

    steam_root = _steam_root()
    lines.append(f"steam dir:     {steam_root if steam_root else 'not found'}")
    # The OS home directory, distinct from the steamid3/userdata identity on
    # the next line, which is the Steam account `sync` would write to.
    lines.append(f"home dir:      {Path.home()}")
    lines.append(_steam_user_line(steam_root))
    lines.append(f"steam running: {'yes' if _steam_running() else 'no'}")

    moonlight_bin = moonlight.find_binary()
    lines.append(f"moonlight:     {' '.join(moonlight_bin) if moonlight_bin else 'not found'}")

    key_present = bool(cfg.sgdb_api_key)
    lines.append(f"sgdb api key:  {'present' if key_present else 'not set'}")
    config_state = "found" if cfg.config_path.is_file() else "missing, using defaults"
    lines.append(f"config file:   {cfg.config_path} ({config_state})")
    lines.append(f"key file:      {DEFAULT_KEY_FILE}")
    lines.append(f"configured host: {cfg.host or '(none set)'}")

    lines.append(_session_line())
    lines.append(hosts.format_active_host_line(cfg.host, cfg.host_source))
    lines.append(hosts.format_cached_hosts_line(hosts.list_cached_hosts(hosts.hosts_dir())))

    owned_apps_flag = getattr(args, "owned_apps", None)
    if owned_apps_flag:
        owned_path = Path(owned_apps_flag)
        try:
            apps = load_owned_apps(owned_path)
        except ConfigError as exc:
            lines.append(f"owned-apps file: {owned_path} (invalid: {exc})")
        else:
            steamid3 = owned_apps_steamid3(owned_path)
            lines.append(
                f"owned-apps file: {owned_path} ({len(apps)} apps, steamid3 {steamid3})"
            )

    for line in lines:
        reporter.line(line)
    reporter.event("end", count=len(lines))
    return EXIT_OK


def cmd_launch(
    args: argparse.Namespace, *, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    """`exec` into `moonlight stream <host> "<name>"` (spec 3.3).

    Never returns on success: :func:`moonlight.stream` replaces this
    process via ``os.execvp``. Under ``--json`` (spec 3.4.6): ``start`` then
    ``exec`` (its only other possible line is ``error``).
    """
    out = out or sys.stdout
    err = err or sys.stderr
    json_mode = bool(getattr(args, "json", False))
    reporter = Reporter(out, err, json=json_mode, command="launch", version=_version())
    reporter.start()

    cfg = load_config(args)
    if not cfg.host:
        reporter.error(
            "launch: no host configured; set `host` in ~/.config/moonlight-steam-sync/config.toml",
            EXIT_USAGE_OR_CONFIG,
        )
        return EXIT_USAGE_OR_CONFIG

    reporter.event("exec", host=cfg.host, name=args.name)
    try:
        moonlight.stream(cfg.host, args.name, args.extra)
    except moonlight.MoonlightNotFoundError as exc:
        reporter.error(f"launch: {exc}", EXIT_MOONLIGHT_UNREACHABLE)
        return EXIT_MOONLIGHT_UNREACHABLE
    except OSError as exc:
        # os.execvp failed to replace the process (e.g. the resolved binary
        # vanished between find_binary() and exec).
        reporter.error(f"launch: failed to run moonlight: {exc}", EXIT_MOONLIGHT_UNREACHABLE)
        return EXIT_MOONLIGHT_UNREACHABLE
    return EXIT_OK  # pragma: no cover -- unreachable when execvp succeeds


def main(
    argv: list[str] | None = None,
    *,
    provider_factory: ProviderFactory | None = None,
    deps: sync.Deps | None = None,
) -> int:
    """Parse ``argv`` and run the subcommand.

    ``provider_factory`` is the seam onto the shortcut layer used by ``art``
    and ``status`` (see :mod:`moonlight_steam_sync.art.apply`) and ``deps``
    the one used by ``sync`` / ``list`` / ``ignore`` / ``remove`` (see
    :class:`moonlight_steam_sync.sync.Deps`); tests inject fakes.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    deps = deps or sync.Deps()
    if provider_factory is None:

        def provider_factory(config):  # noqa: E306 - the real, Steam-aware provider
            return sync.steam_aware_provider(config, runner=deps.runner)

    try:
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "launch":
            return cmd_launch(args)
        if args.command == "art":
            return cmd_art(args, load_config(args), provider_factory=provider_factory)
        if args.command == "status":
            return cmd_status(
                args,
                load_config(args),
                provider_factory=provider_factory,
                cache_path=deps.cache_path,
                hosts_dir=deps.hosts_dir,
            )
        if args.command == "search":
            return cmd_search(args, load_config(args), cache_path=deps.cache_path)
        if args.command == "host":
            cfg = load_config(args)
            return hosts.cmd_host(
                args,
                config_path=cfg.config_path,
                config_host=toml_host(cfg.config_path),
                json_mode=bool(getattr(args, "json", False)),
                version=_version(),
            )
        if args.command == "sync":
            return sync.cmd_sync(args, load_config(args), deps=deps)
        if args.command == "list":
            return sync.cmd_list(args, load_config(args), deps=deps)
        if args.command == "ignore":
            return sync.cmd_ignore(args, load_config(args), deps=deps)
        if args.command == "remove":
            return sync.cmd_remove(args, load_config(args), deps=deps)
    except KeyboardInterrupt:
        # Every subcommand function emits its own `start` as its first line,
        # so by the time a bare KeyboardInterrupt reaches here that has
        # already happened; this only needs to add the `error` that must
        # always be last (spec 3.4.6). Whatever subcommand this was hit
        # before it had a `plan` to report (sync's own internal handler
        # covers everything after that), so no `summary` is owed here.
        print(f"\n{sync.RESUME_HINT}", file=sys.stderr)
        if bool(getattr(args, "json", False)):
            Reporter(
                sys.stdout, sys.stderr, json=True, command=str(args.command), version=_version()
            ).event("error", exit=EXIT_SIGINT, message=sync.RESUME_HINT)
        return EXIT_SIGINT
    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return EXIT_USAGE_OR_CONFIG  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
