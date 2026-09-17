"""Tests for the argparse skeleton in __main__.py.

Every subcommand from spec 3.3 must parse and, except for `doctor`, exit 1
with a "not implemented" message -- this PR only wires the CLI surface and
config.py, everything else is stubbed for later PRs.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from moonlight_steam_sync import __main__ as main_module
from moonlight_steam_sync import config as config_module
from moonlight_steam_sync.__main__ import build_parser, main

NOT_YET_IMPLEMENTED = ["sync", "art", "list", "status", "launch", "ignore", "remove"]

# `ignore` and `remove` require their mutually-exclusive `--all | names` group
# to be satisfied to even parse; every other stub takes no required args.
_ARGV_FOR_COMMAND = {
    "launch": ["launch", "X"],
    "ignore": ["ignore", "--all"],
    "remove": ["remove", "--all"],
}


def test_build_parser_accepts_every_documented_subcommand():
    parser = build_parser()
    parser.parse_args(["sync"])
    parser.parse_args(
        ["sync", "--host", "H", "--dry-run", "--no-art", "--limit", "5", "--no-restart-steam"]
    )
    parser.parse_args(["art", "--force", "--only", "Some Game", "--explain"])
    parser.parse_args(["list", "--host", "H"])
    parser.parse_args(["status"])
    parser.parse_args(["ignore", "--all"])
    parser.parse_args(["ignore", "Some Game"])
    parser.parse_args(["remove", "--all"])
    parser.parse_args(["remove", "Some Game", "Another Game"])
    parser.parse_args(["launch", "Some Game"])
    parser.parse_args(["launch", "Some Game", "--", "--fps", "60"])
    parser.parse_args(["doctor"])


@pytest.mark.parametrize("command", NOT_YET_IMPLEMENTED)
def test_stub_commands_exit_1(command, capsys):
    exit_code = main(_ARGV_FOR_COMMAND.get(command, [command]))
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "not implemented" in captured.err


def test_doctor_runs_and_exits_0(capsys, tmp_path, monkeypatch):
    """Isolated from the developer's real environment: doctor must not read
    the real ~/.config/moonlight-steam-sync/config.toml, the real key file,
    or a real SGDB_API_KEY, all of which would make this test's outcome
    depend on whoever's machine runs it.
    """
    key_file = tmp_path / "sgdb-api-key"
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", key_file)
    monkeypatch.setattr(main_module, "DEFAULT_KEY_FILE", key_file)
    monkeypatch.delenv("SGDB_API_KEY", raising=False)

    exit_code = main(["doctor"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "python:" in out
    assert "steam dir:" in out
    assert "moonlight:" in out
    assert "sgdb api key:" in out
    assert "steam running:" in out
    assert "sgdb api key:  not set" in out


def test_no_subcommand_is_a_usage_error():
    result = subprocess.run(
        [sys.executable, "-m", "moonlight_steam_sync"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2  # argparse's own usage-error exit code


def test_version_flag():
    result = subprocess.run(
        [sys.executable, "-m", "moonlight_steam_sync", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "moonlight-steam-sync" in result.stdout
