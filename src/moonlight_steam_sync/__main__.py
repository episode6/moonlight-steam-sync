"""argparse entry point: subcommands, exit codes, logging, SIGINT handling.

Only ``doctor`` does real work in this PR (spec's PR-1 scope); every other
subcommand parses its flags and then exits 1 with "not implemented", so the
CLI surface (spec 3.3) is fixed before the modules behind it exist.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from moonlight_steam_sync import __version__
from moonlight_steam_sync.config import DEFAULT_KEY_FILE, load_config

NOT_IMPLEMENTED = "not implemented"

# Exit codes (spec 3.3).
EXIT_OK = 0
EXIT_USAGE_OR_CONFIG = 1
EXIT_STEAM_RUNNING = 2
EXIT_MOONLIGHT_UNREACHABLE = 3
EXIT_NETWORK_STOPPED = 4
EXIT_SIGINT = 130


def _add_common_host_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", help="Moonlight host name (overrides config)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moonlight-steam-sync",
        description="Sync a Moonlight host's game list into Steam shortcuts, with artwork.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sync_p = sub.add_parser("sync", help="add missing shortcuts and their artwork")
    _add_common_host_flag(sync_p)
    sync_p.add_argument("--dry-run", action="store_true")
    sync_p.add_argument("--no-art", action="store_true")
    sync_p.add_argument("--limit", type=int, default=None)
    sync_p.add_argument("--retry-missing", action="store_true")
    sync_p.add_argument("--no-restart-steam", action="store_true")

    art_p = sub.add_parser("art", help="(re)apply art to owned shortcuts")
    art_p.add_argument("--force", action="store_true")
    art_p.add_argument("--retry-missing", action="store_true")
    art_p.add_argument("--only", metavar="NAME")
    art_p.add_argument("--explain", action="store_true")

    list_p = sub.add_parser("list", help="what the host publishes: added / ignored / new")
    _add_common_host_flag(list_p)

    sub.add_parser("status", help="owned shortcuts and which art slots each has on disk")

    ignore_p = sub.add_parser(
        "ignore", help="print TOML ignore = [...] lines to paste into config"
    )
    ignore_group = ignore_p.add_mutually_exclusive_group(required=True)
    ignore_group.add_argument("--all", action="store_true")
    ignore_group.add_argument("names", nargs="*", default=[])

    remove_p = sub.add_parser("remove", help="delete owned shortcuts and their grid files")
    remove_group = remove_p.add_mutually_exclusive_group(required=True)
    remove_group.add_argument("--all", action="store_true")
    remove_group.add_argument("names", nargs="*", default=[])

    launch_p = sub.add_parser("launch", help='exec moonlight stream <host> "Name"')
    launch_p.add_argument("name")
    launch_p.add_argument("extra", nargs=argparse.REMAINDER)

    sub.add_parser("doctor", help="report the environment this tool will run in")

    return parser


def _print_not_implemented(command: str) -> int:
    print(f"{command}: {NOT_IMPLEMENTED}", file=sys.stderr)
    return EXIT_USAGE_OR_CONFIG


def _find_moonlight() -> str | None:
    native = shutil.which("moonlight")
    if native:
        return native
    flatpak = shutil.which("flatpak")
    if flatpak:
        try:
            result = subprocess.run(
                [flatpak, "list", "--app", "--columns=application"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except OSError:
            return None
        if "com.moonlight_stream.Moonlight" in result.stdout:
            return "flatpak run com.moonlight_stream.Moonlight"
    return None


def _steam_running() -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-x", "steam"], capture_output=True, timeout=5, check=False
        )
        return result.returncode == 0
    except (OSError, FileNotFoundError):
        pass
    # Fall back to scanning /proc/*/comm (spec 3.6), for systems without pgrep.
    for comm_path in Path("/proc").glob("[0-9]*/comm"):
        try:
            if comm_path.read_text().strip() == "steam":
                return True
        except OSError:
            continue
    return False


def _steam_root() -> Path | None:
    for candidate in (Path.home() / ".steam" / "steam", Path.home() / ".local" / "share" / "Steam"):
        if candidate.is_dir():
            return candidate
    return None


def cmd_doctor(args: argparse.Namespace) -> int:
    """Print the environment report spec 3.3 promises: steam dir, user,
    python, moonlight path, key present?, steam running?
    """
    cfg = load_config(args)

    lines = [
        f"python:        {platform.python_version()} ({sys.executable})",
        f"platform:      {platform.platform()}",
    ]

    steam_root = _steam_root()
    lines.append(f"steam dir:     {steam_root if steam_root else 'not found'}")
    lines.append(f"steam user:    {Path.home()}")
    lines.append(f"steam running: {'yes' if _steam_running() else 'no'}")

    moonlight_path = _find_moonlight()
    lines.append(f"moonlight:     {moonlight_path if moonlight_path else 'not found'}")

    key_present = bool(cfg.sgdb_api_key)
    lines.append(f"sgdb api key:  {'present' if key_present else 'not set'}")
    config_state = "found" if cfg.config_path.is_file() else "missing, using defaults"
    lines.append(f"config file:   {cfg.config_path} ({config_state})")
    lines.append(f"key file:      {DEFAULT_KEY_FILE}")
    lines.append(f"configured host: {cfg.host or '(none set)'}")

    print("\n".join(lines))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    def _on_sigint(signum: int, frame: object) -> None:
        print("\ninterrupted; resume with the same command", file=sys.stderr)
        sys.exit(EXIT_SIGINT)

    signal.signal(signal.SIGINT, _on_sigint)

    if args.command == "doctor":
        return cmd_doctor(args)

    return _print_not_implemented(args.command)


if __name__ == "__main__":
    sys.exit(main())
