"""Tests for the argparse skeleton in __main__.py.

Every subcommand from spec 3.3 must parse. `doctor` (PR-1), `launch` (PR-3),
`art` and `status` are implemented; the rest still exit 1 with a "not
implemented" message until the PR that implements their module lands.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from moonlight_steam_sync import __main__ as main_module
from moonlight_steam_sync import config as config_module
from moonlight_steam_sync import moonlight
from moonlight_steam_sync.__main__ import build_parser, main

NOT_YET_IMPLEMENTED = ["sync", "list", "ignore", "remove"]

# `ignore` and `remove` require their mutually-exclusive `--all | names` group
# to be satisfied to even parse; every other stub takes no required args.
_ARGV_FOR_COMMAND = {
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


@pytest.mark.parametrize("command", ["art", "status"])
def test_art_and_status_need_the_shortcut_layer(command, capsys, tmp_path, monkeypatch):
    """Implemented, but they need an appid -> grid dir lookup to run against.

    Until the shortcut layer is wired in, both report that and exit 1 rather
    than pretending they found an empty library. See
    `moonlight_steam_sync.art.apply.default_target_provider`.

    Isolated from the developer's real config and key the same way
    `test_doctor_runs_and_exits_0` is: this must not read whatever happens to
    be in ~/.config on the machine running it.
    """
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("SGDB_API_KEY", raising=False)

    assert main([command]) == 1
    assert "shortcut lookup" in capsys.readouterr().err


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


def test_launch_execs_moonlight_stream_with_configured_host(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    config_path.write_text('host = "MY-GAMING-PC"\n')
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")

    captured = {}
    monkeypatch.setattr(moonlight, "find_binary", lambda: ["/usr/bin/moonlight"])
    monkeypatch.setattr(
        os, "execvp", lambda file, args: captured.update(file=file, args=args)
    )

    exit_code = main(["launch", "Elden Ring", "--", "--fps", "60"])

    assert exit_code == main_module.EXIT_OK
    assert captured["args"] == [
        "/usr/bin/moonlight",
        "stream",
        "MY-GAMING-PC",
        "Elden Ring",
        "--fps",
        "60",
    ]


def test_launch_without_a_configured_host_is_a_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")

    exit_code = main(["launch", "Elden Ring"])

    assert exit_code == main_module.EXIT_USAGE_OR_CONFIG
    assert "no host configured" in capsys.readouterr().err


def test_launch_reports_moonlight_unreachable_when_binary_missing(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.toml"
    config_path.write_text('host = "MY-GAMING-PC"\n')
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")
    monkeypatch.setattr(moonlight, "find_binary", lambda: None)

    exit_code = main(["launch", "Elden Ring"])

    assert exit_code == main_module.EXIT_MOONLIGHT_UNREACHABLE
    assert "moonlight CLI not found" in capsys.readouterr().err


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
