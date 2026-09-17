"""Tests for config.py precedence rules (spec 3.2).

Precedence under test: CLI flags > config file > built-in defaults, and the
SteamGridDB API key's own chain: config file (or its flag) > SGDB_API_KEY env
> ~/.config/moonlight-steam-sync/sgdb-api-key file.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from moonlight_steam_sync.config import (
    default_exe,
    default_launch_options,
    load_config,
)

CONFIG_TOML = """
host = "MY-GAMING-PC"
name_suffix = " (streaming)"
ignore = ["Desktop", "Steam Big Picture"]
restart_steam = false
request_interval_ms = 500

[steamgriddb]
api_key = "file-key"
community_fallback = false

[overrides]
"Some Weird Launcher Name" = { steam = 1245620 }
"Fan Game" = { sgdb = 5247018 }
"Desktop" = { art = false }
"""


def ns(**kwargs):
    """A minimal argparse.Namespace stand-in: missing attrs read as None."""
    n = argparse.Namespace()
    for k, v in kwargs.items():
        setattr(n, k, v)
    return n


def test_defaults_when_no_file_no_flags(tmp_path: Path):
    missing = tmp_path / "config.toml"
    cfg = load_config(config_path=missing, env={}, key_file=tmp_path / "sgdb-api-key")

    assert cfg.host == ""
    assert cfg.name_suffix == ""
    assert cfg.ignore == []
    assert cfg.restart_steam is True
    assert cfg.request_interval_ms == 250
    assert cfg.sgdb_api_key == ""
    assert cfg.sgdb_community_fallback is True
    assert cfg.exe == default_exe()
    assert cfg.launch_options == default_launch_options(cfg.exe)
    assert cfg.start_dir == str(Path(cfg.exe).parent)


def test_file_overrides_defaults(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TOML)

    cfg = load_config(config_path=config_path, env={}, key_file=tmp_path / "sgdb-api-key")

    assert cfg.host == "MY-GAMING-PC"
    assert cfg.name_suffix == " (streaming)"
    assert cfg.ignore == ["Desktop", "Steam Big Picture"]
    assert cfg.restart_steam is False
    assert cfg.request_interval_ms == 500
    assert cfg.sgdb_community_fallback is False
    assert cfg.overrides["Some Weird Launcher Name"] == {"steam": 1245620}
    assert cfg.overrides["Fan Game"] == {"sgdb": 5247018}
    assert cfg.overrides["Desktop"] == {"art": False}


def test_flags_override_file(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TOML)

    args = ns(host="OTHER-PC", restart_steam=False, request_interval_ms=1000)
    cfg = load_config(args, config_path=config_path, env={}, key_file=tmp_path / "sgdb-api-key")

    assert cfg.host == "OTHER-PC"
    # Flag not passed for name_suffix -> file value survives.
    assert cfg.name_suffix == " (streaming)"
    assert cfg.request_interval_ms == 1000


def test_flags_override_defaults_with_no_file(tmp_path: Path):
    missing = tmp_path / "config.toml"
    args = ns(host="FLAG-ONLY-PC")
    cfg = load_config(args, config_path=missing, env={}, key_file=tmp_path / "sgdb-api-key")

    assert cfg.host == "FLAG-ONLY-PC"
    assert cfg.name_suffix == ""


def test_unset_flag_attribute_does_not_shadow_file(tmp_path: Path):
    """A Namespace missing the attribute entirely behaves like None: no override."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(CONFIG_TOML)

    args = ns()  # no attributes at all
    cfg = load_config(args, config_path=config_path, env={}, key_file=tmp_path / "sgdb-api-key")

    assert cfg.host == "MY-GAMING-PC"


@pytest.mark.parametrize(
    "config_toml,env,key_file_contents,expected",
    [
        # 1. config file wins over everything else.
        (
            '[steamgriddb]\napi_key = "from-config"\n',
            {"SGDB_API_KEY": "from-env"},
            "from-keyfile",
            "from-config",
        ),
        # 2. no config value -> env wins over the key file.
        (
            "",
            {"SGDB_API_KEY": "from-env"},
            "from-keyfile",
            "from-env",
        ),
        # 3. no config, no env -> key file is the last resort.
        (
            "",
            {},
            "from-keyfile",
            "from-keyfile",
        ),
        # 4. nothing anywhere -> empty string, not an error (key is optional).
        (
            "",
            {},
            None,
            "",
        ),
    ],
)
def test_api_key_lookup_order(
    tmp_path: Path, config_toml: str, env: dict, key_file_contents, expected: str
):
    config_path = tmp_path / "config.toml"
    config_path.write_text(config_toml)

    key_file = tmp_path / "sgdb-api-key"
    if key_file_contents is not None:
        key_file.write_text(key_file_contents + "\n")

    cfg = load_config(config_path=config_path, env=env, key_file=key_file)

    assert cfg.sgdb_api_key == expected


def test_launch_options_default_depends_on_exe(tmp_path: Path):
    """Default launch_options is 'launch \"{name}\"' only when exe is still
    this tool's own path (spec 3.2); a custom exe gets the bare template."""
    missing = tmp_path / "config.toml"

    cfg_default_exe = load_config(config_path=missing, env={}, key_file=tmp_path / "kf")
    assert cfg_default_exe.launch_options == 'launch "{name}"'

    args = ns(exe="/home/deck/server-scripts/chimera/stream.sh")
    cfg_custom_exe = load_config(args, config_path=missing, env={}, key_file=tmp_path / "kf")
    assert cfg_custom_exe.launch_options == '"{name}"'
    assert cfg_custom_exe.start_dir == "/home/deck/server-scripts/chimera"


def test_start_dir_defaults_to_exe_dirname(tmp_path: Path):
    missing = tmp_path / "config.toml"
    args = ns(exe="/opt/moonlight/stream.sh")
    cfg = load_config(args, config_path=missing, env={}, key_file=tmp_path / "kf")
    assert cfg.start_dir == "/opt/moonlight"


def test_flag_can_override_start_dir_explicitly(tmp_path: Path):
    missing = tmp_path / "config.toml"
    args = ns(exe="/opt/moonlight/stream.sh", start_dir="/custom/dir")
    cfg = load_config(args, config_path=missing, env={}, key_file=tmp_path / "kf")
    assert cfg.start_dir == "/custom/dir"
