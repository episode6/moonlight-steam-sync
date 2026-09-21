"""argparse entry point: subcommands, exit codes, SIGINT handling.

Every subcommand in spec 3.3 is wired: ``doctor``, ``launch``, ``art`` and
``status`` to their own modules, and ``sync`` / ``list`` / ``ignore`` /
``remove`` to the orchestration in :mod:`moonlight_steam_sync.sync`. The
Decky-plugin additions (decky spec 3.4) follow the same split: ``search``
and ``match`` live in :mod:`moonlight_steam_sync.art.cli`, ``host`` in
:mod:`moonlight_steam_sync.hosts`.

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
from typing import Any, TextIO

from moonlight_steam_sync import __version__, hosts, moonlight, procenv, steam, sync
from moonlight_steam_sync.art.cli import (
    ProviderFactory,
    cmd_art,
    cmd_match,
    cmd_search,
    cmd_status,
)
from moonlight_steam_sync.config import (
    DEFAULT_KEY_FILE,
    ConfigError,
    default_exe,
    load_config,
    load_ignore_file,
    load_owned_apps,
    owned_apps_steamid3,
    toml_host,
)
from moonlight_steam_sync.reporting import Reporter
from moonlight_steam_sync.shortcuts import ShortcutsError, ShortcutsFile

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


def _add_owned_apps_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--owned-apps",
        metavar="PATH",
        help=(
            "a JSON file of the Steam games this account owns (written by the Decky "
            "plugin, spec 3.4.1); a title that matches one gets a hidden shortcut "
            "named after the owned game instead of a visible tile"
        ),
    )


def _add_hide_host_apps_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--hide-host-apps",
        action="store_true",
        help=(
            "treat the host's two default apps, \"Desktop\" and \"Steam Big Picture\", as "
            "hidden shortcuts (same name, art and controller layout; just no library tile), "
            "for a launcher that starts them another way, like the Decky plugin's panel "
            "buttons (decky spec 3.14)"
        ),
    )


def _add_commit_flag(
    parser: argparse.ArgumentParser, *, with_no_restart: bool = False
) -> None:
    """``--commit {restart,await-exit,refuse}`` (decky spec 3.5) and, on
    ``sync``, the older ``--no-restart-steam`` it keeps as an alias of
    ``refuse`` -- the two exclude each other."""
    target: Any = parser
    if with_no_restart:
        target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--commit",
        choices=list(sync.COMMIT_MODES),
        default=None,
        metavar="MODE",
        help=(
            "how shortcuts.vdf gets written while Steam runs: `restart` (shut Steam "
            "down, write, start it again; the default unless restart_steam = false), "
            "`await-exit` (wait up to 60 s for Steam to exit on its own, write the "
            "moment it is gone, never start it; for the Decky plugin, which restarts "
            "Steam itself from Game Mode) or `refuse` (exit 2 without writing; same "
            "as --no-restart-steam)"
        ),
    )
    if with_no_restart:
        target.add_argument(
            "--no-restart-steam",
            action="store_true",
            help="never stop or start Steam; exit 2 instead of writing while it runs",
        )


def _add_ignore_file_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--ignore-file",
        metavar="PATH",
        help=(
            "a JSON list of Moonlight names to ignore on top of config.toml's "
            "`ignore` (spec 3.4.3)"
        ),
    )


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
        help=(
            "print the plan (shortcuts to add, replace, remove or park, per-slot art "
            "source and URL) and write nothing"
        ),
    )
    sync_p.add_argument(
        "--no-art", action="store_true", help="skip the art phase and go straight to the write"
    )
    sync_p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help=(
            "add or replace at most N shortcuts this run (host-list order, a replacement "
            "never split, IsHidden flips not counted); the rest wait"
        ),
    )
    sync_p.add_argument(
        "--retry-missing",
        action="store_true",
        help="re-query titles and slots whose 'nothing found' result is cached",
    )
    _add_commit_flag(sync_p, with_no_restart=True)
    _add_ignore_file_flag(sync_p)
    _add_owned_apps_flag(sync_p)
    sync_p.add_argument(
        "--client-shortcut",
        action="store_true",
        help=(
            "make sure one hidden \"Moonlight\" shortcut exists that opens the Moonlight "
            "client itself (`client`), for launching it from Game Mode (spec 3.4.5)"
        ),
    )
    sync_p.add_argument(
        "--park-unpublished",
        action="store_true",
        help=(
            "hide (in place, art and pins kept) every owned shortcut this host does not "
            "publish, and show published ones again; for switching between hosts (spec 3.12)"
        ),
    )

    _add_hide_host_apps_flag(sync_p)

    art_p = sub.add_parser("art", help="(re)apply art to owned shortcuts")
    art_p.add_argument(
        "--force",
        action="store_true",
        help=(
            "re-fetch every slot, ignoring the match cache (except titles pinned with "
            "`match`), and replace the files already in grid/ (a slot ends up with "
            "exactly one file)"
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
    _add_commit_flag(art_p)

    list_p = sub.add_parser("list", help="what the host publishes: added / ignored / new")
    _add_common_host_flag(list_p)
    list_p.add_argument(
        "--cached",
        action="store_true",
        help="serve the per-host list cache instead of running moonlight (spec 3.12)",
    )
    _add_ignore_file_flag(list_p)
    _add_owned_apps_flag(list_p)
    _add_hide_host_apps_flag(list_p)

    status_p = sub.add_parser(
        "status", help="owned shortcuts and which art slots each has on disk"
    )
    _add_common_host_flag(status_p)
    status_p.add_argument(
        "--owned-apps", metavar="PATH", help="a plugin-written owned-apps JSON file (spec 3.4.1)"
    )

    _add_hide_host_apps_flag(status_p)

    search_p = sub.add_parser("search", help="find a Steam/SteamGridDB candidate for a title")
    search_p.add_argument("term")
    search_p.add_argument(
        "--owned-apps", metavar="PATH", help="a plugin-written owned-apps JSON file (spec 3.4.1)"
    )

    match_p = sub.add_parser(
        "match",
        help="pin what a Moonlight title is matched to (or unpin it); see `match --help`",
        description=(
            "Pin a title's match in the match cache (matches.json): `art --force` and "
            "--retry-missing never overwrite a pin, while an [overrides] entry in "
            "config.toml still wins over it. Prints the equivalent [overrides] line, "
            "which you can paste into config.toml to make the pin permanent (this tool "
            "never writes it). When the title has a shortcut, its grid files are deleted "
            "and its icon cleared now (one Steam restart, like `remove`) unless "
            "--defer-art leaves that to the next `sync` (or `art --force`)."
        ),
    )
    match_p.add_argument("name", metavar="NAME", help="the Moonlight app name, exactly")
    match_group = match_p.add_mutually_exclusive_group(required=True)
    match_group.add_argument(
        "--steam", type=int, metavar="APPID", help="pin this Steam appid"
    )
    match_group.add_argument(
        "--sgdb", type=int, metavar="ID", help="pin this SteamGridDB game id"
    )
    match_group.add_argument(
        "--none", action="store_true", help='pin "no match" (no lookups, no art)'
    )
    match_group.add_argument(
        "--unpin",
        action="store_true",
        help="forget the title's match so the next run resolves it again",
    )
    match_p.add_argument(
        "--defer-art",
        action="store_true",
        help=(
            "only write the cache (marking the art stale); the next `sync` (or "
            "`art --force`) replaces the art, so Steam is not restarted now"
        ),
    )
    match_p.add_argument(
        "--force-name",
        action="store_true",
        help="accept a name the host does not publish (yet) and no shortcut carries",
    )
    _add_commit_flag(match_p)

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
    _add_ignore_file_flag(ignore_p)

    remove_p = sub.add_parser("remove", help="delete owned shortcuts and their grid files")
    # `--all` and NAME... exclude each other; `--client` combines with either
    # or stands alone, so the "one of them is required" check lives in
    # main() (argparse cannot express that with one group).
    remove_group = remove_p.add_mutually_exclusive_group()
    remove_group.add_argument("--all", action="store_true", help="every owned shortcut")
    remove_group.add_argument("names", nargs="*", default=[], metavar="NAME")
    remove_p.add_argument(
        "--client",
        action="store_true",
        help=(
            "also delete the hidden \"Moonlight\" client shortcut `sync --client-shortcut` "
            "made (`--all` takes it only when `exe` is this tool)"
        ),
    )
    _add_commit_flag(remove_p)
    remove_p.set_defaults(_subparser=remove_p)

    launch_p = sub.add_parser("launch", help='exec moonlight stream <host> "Name"')
    _add_common_host_flag(launch_p)
    launch_p.add_argument("name")
    launch_p.add_argument("extra", nargs=argparse.REMAINDER)

    client_p = sub.add_parser(
        "client",
        help="exec the Moonlight client itself (what the hidden \"Moonlight\" shortcut runs)",
    )
    client_p.add_argument("extra", nargs=argparse.REMAINDER)

    doctor_p = sub.add_parser("doctor", help="report the environment this tool will run in")
    _add_common_host_flag(doctor_p)
    doctor_p.add_argument(
        "--owned-apps", metavar="PATH", help="a plugin-written owned-apps JSON file (spec 3.4.1)"
    )
    _add_ignore_file_flag(doctor_p)
    doctor_p.add_argument(
        "--client-shortcut",
        action="store_true",
        help="report whether the hidden \"Moonlight\" client shortcut exists (spec 3.4.7)",
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


def _client_shortcut_line(root: Path | None) -> str:
    """``client shortcut: present [appid]`` / ``absent`` (spec 3.4.7);
    ``unknown (...)`` when the library cannot be read, since ``doctor``
    never fails over what it reports on."""
    if root is None:
        return "client shortcut: unknown (no Steam directory)"
    try:
        user = steam.pick_user(root)
        shortcuts_file = ShortcutsFile.read(user.shortcuts_path)
    except (steam.SteamError, ShortcutsError) as exc:
        return f"client shortcut: unknown ({exc})"
    entry = shortcuts_file.client_entry(default_exe())
    if entry is None:
        return "client shortcut: absent"
    return f"client shortcut: present [{entry.appid}]"


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
    ``active host:``, ``cached hosts:``), an ``owned-apps file:`` line
    when ``--owned-apps`` is given and an ``ignore file:`` line when
    ``--ignore-file`` is; ``doctor`` never exits 1 over a bad file, since
    it is a diagnostic.

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
    moonlight_found = " ".join(moonlight_bin) if moonlight_bin else moonlight.not_found_label()
    lines.append(f"moonlight:     {moonlight_found}")

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
            steamid3_text = str(steamid3) if steamid3 is not None else "missing"
            lines.append(
                f"owned-apps file: {owned_path} ({len(apps)} apps, steamid3 {steamid3_text})"
            )

    ignore_file_flag = getattr(args, "ignore_file", None)
    if ignore_file_flag:
        ignore_path = Path(ignore_file_flag)
        try:
            ignored = load_ignore_file(ignore_path)
        except ConfigError as exc:
            lines.append(f"ignore file: {ignore_path} (invalid: {exc})")
        else:
            lines.append(f"ignore file: {ignore_path} ({len(ignored)} names)")

    if getattr(args, "client_shortcut", False):
        lines.append(_client_shortcut_line(steam_root))

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

    binary = moonlight.find_binary()
    if binary is None:
        reporter.error(
            "launch: moonlight CLI not found (native binary, flatpak, or "
            "MOONLIGHT_BIN)",
            EXIT_MOONLIGHT_UNREACHABLE,
        )
        return EXIT_MOONLIGHT_UNREACHABLE

    reporter.event("exec", host=cfg.host, name=args.name)
    # stdout/stderr are block-buffered when not attached to a terminal (a
    # pipe, as the Decky plugin uses), and os.execvp below replaces this
    # process image without ever flushing Python's buffers -- so without an
    # explicit flush here, everything emitted so far (start, exec) is lost
    # rather than reaching the reader.
    out.flush()
    err.flush()
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


def cmd_client(
    args: argparse.Namespace, *, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    """`exec` into the Moonlight client itself, no host or app (decky spec 3.4.5).

    This is what the hidden "Moonlight" shortcut ``sync --client-shortcut``
    writes runs, so *Open Moonlight* in Game Mode is a Steam-launched app.
    The host is resolved (flag-less: state file, then config) only so the
    ``exec`` event can say which one applies; Moonlight's GUI needs none.
    Never returns on success. Under ``--json``: ``start`` then ``exec``.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    json_mode = bool(getattr(args, "json", False))
    reporter = Reporter(out, err, json=json_mode, command="client", version=_version())
    reporter.start()

    cfg = load_config(args)
    binary = moonlight.find_binary()
    if binary is None:
        reporter.error(
            "client: moonlight CLI not found (native binary, flatpak, or MOONLIGHT_BIN)",
            EXIT_MOONLIGHT_UNREACHABLE,
        )
        return EXIT_MOONLIGHT_UNREACHABLE

    # With no positional before it, argparse hands REMAINDER the `--`
    # separator itself (`client -- --fullscreen`); Moonlight must not see it.
    extra = list(args.extra)
    if extra[:1] == ["--"]:
        extra = extra[1:]

    reporter.event("exec", host=cfg.host or None)
    # See cmd_launch: flush before execvp replaces the process image.
    out.flush()
    err.flush()
    try:
        moonlight.run_client(extra)
    except moonlight.MoonlightNotFoundError as exc:
        reporter.error(f"client: {exc}", EXIT_MOONLIGHT_UNREACHABLE)
        return EXIT_MOONLIGHT_UNREACHABLE
    except OSError as exc:
        reporter.error(f"client: failed to run moonlight: {exc}", EXIT_MOONLIGHT_UNREACHABLE)
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
    if argv is None:
        # A real invocation, not a test: undo a PyInstaller parent's
        # LD_LIBRARY_PATH (Decky's plugin_loader) before anything loads
        # libssl or runs flatpak. A no-op everywhere else.
        procenv.reexec_if_needed()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "remove" and not (args.all or args.names or args.client):
        # The v0.2.0 (--all | NAME ...) group requirement, restated so that
        # --client can stand alone or combine with either (exit 2, as before).
        args._subparser.error("one of the arguments --all NAME --client is required")
    deps = deps or sync.Deps()
    if provider_factory is None:

        def provider_factory(config):  # noqa: E306 - the real, Steam-aware provider
            return sync.steam_aware_provider(
                config, runner=deps.runner, mode=getattr(args, "commit", None)
            )

    try:
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "launch":
            return cmd_launch(args)
        if args.command == "client":
            return cmd_client(args)
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
        if args.command == "match":
            return cmd_match(args, load_config(args), deps=deps)
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
