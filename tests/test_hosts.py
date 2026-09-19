"""``hosts.py``: the active-host state file and the per-host list cache
(spec 3.4.6, 3.4.8, 3.12; PR-1 acceptance).

Builds on the ``world`` / ``fresh_world`` fixtures and the ``host_publishes``
/ ``make_config`` helpers from ``tests/test_sync_e2e.py`` rather than
duplicating the Steam-tree and fake-moonlight plumbing (spec 4 preamble: new
tests import those rather than editing the frozen e2e file).
"""

from __future__ import annotations

import argparse
import io
import json
import os

import pytest

from moonlight_steam_sync import __main__ as main_module
from moonlight_steam_sync import config as config_module
from moonlight_steam_sync import hosts, moonlight
from moonlight_steam_sync.__main__ import main
from moonlight_steam_sync.art.resolve import Match, MatchCache
from moonlight_steam_sync.config import load_config
from tests.fakes import install_fake_moonlight
from tests.test_sync_e2e import HOST, fresh_world, host_publishes, make_config, world  # noqa: F401

OFFICE = "OFFICE-PC"


@pytest.fixture(autouse=True)
def _state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


# ---------------------------------------------------------------------------
# active-host precedence: flag > state file > config.toml
# ---------------------------------------------------------------------------


def test_host_precedence_flag_over_state_over_config(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('host = "CONFIG-PC"\n')
    key_file = tmp_path / "sgdb-api-key"

    cfg = load_config(None, config_path=config_path, key_file=key_file)
    assert (cfg.host, cfg.host_source) == ("CONFIG-PC", "config")

    hosts.write_active_host("STATE-PC")
    cfg = load_config(None, config_path=config_path, key_file=key_file)
    assert (cfg.host, cfg.host_source) == ("STATE-PC", "state")

    cfg = load_config(
        argparse.Namespace(host="FLAG-PC"), config_path=config_path, key_file=key_file
    )
    assert (cfg.host, cfg.host_source) == ("FLAG-PC", "flag")


def test_empty_state_file_counts_as_absent(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('host = "CONFIG-PC"\n')
    hosts.active_host_path().parent.mkdir(parents=True, exist_ok=True)
    hosts.active_host_path().write_text("   \n")

    cfg = load_config(None, config_path=config_path, key_file=tmp_path / "key")
    assert (cfg.host, cfg.host_source) == ("CONFIG-PC", "config")


# ---------------------------------------------------------------------------
# `host set` / `host show` / `host clear`
# ---------------------------------------------------------------------------


def test_host_set_writes_only_the_state_file(tmp_path):
    config_path = tmp_path / "config.toml"
    out, err = io.StringIO(), io.StringIO()
    code = hosts.cmd_host(
        argparse.Namespace(host_action="set", name=OFFICE),
        config_path=config_path,
        config_host="",
        out=out,
        err=err,
    )
    assert code == 0
    assert hosts.read_active_host() == OFFICE
    assert not config_path.exists()
    assert f"active host:   {OFFICE} (state file)" in out.getvalue()


def test_host_show_with_no_host_at_all_is_exit_1(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    code = hosts.cmd_host(
        argparse.Namespace(host_action="show", host=None),
        config_path=tmp_path / "config.toml",
        config_host="",
        out=out,
        err=err,
    )
    assert code == 1
    assert "host: no host configured" in err.getvalue()
    assert out.getvalue() == ""


def test_host_clear_reverts_to_config_and_exits_0(tmp_path):
    config_path = tmp_path / "config.toml"
    hosts.write_active_host(OFFICE)
    out, err = io.StringIO(), io.StringIO()
    code = hosts.cmd_host(
        argparse.Namespace(host_action="clear"),
        config_path=config_path,
        config_host="CONFIG-PC",
        out=out,
        err=err,
    )
    assert code == 0
    assert hosts.read_active_host() == ""
    assert "active host:   CONFIG-PC (config)" in out.getvalue()


def test_host_show_json_emits_start_and_host_events(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    hosts.cmd_host(
        argparse.Namespace(host_action="show", host=OFFICE),
        config_path=tmp_path / "config.toml",
        config_host="",
        out=out,
        err=err,
        json_mode=True,
        version="0.3.0",
    )
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[0] == {"event": "start", "schema": 1, "version": "0.3.0", "command": "host"}
    assert lines[1]["event"] == "host"
    assert lines[1]["name"] == OFFICE
    assert lines[1]["source"] == "flag"


def test_host_unpin_style_clear_emits_a_host_event_with_null_name(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    hosts.cmd_host(
        argparse.Namespace(host_action="clear"),
        config_path=tmp_path / "config.toml",
        config_host="",
        out=out,
        err=err,
        json_mode=True,
        version="0.3.0",
    )
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[-1] == {
        "event": "host",
        "name": None,
        "source": "config",
        "cached_hosts": [],
    }


# ---------------------------------------------------------------------------
# `launch` honours the active-host resolution order
# ---------------------------------------------------------------------------


def test_launch_execs_against_the_state_files_host(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text('host = "CONFIG-PC"\n')
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")
    hosts.write_active_host("STATE-PC")

    captured = {}
    monkeypatch.setattr(moonlight, "find_binary", lambda: ["/usr/bin/moonlight"])
    monkeypatch.setattr(os, "execvp", lambda file, args: captured.update(file=file, args=args))

    exit_code = main(["launch", "Hades"])
    assert exit_code == main_module.EXIT_OK
    assert captured["args"] == ["/usr/bin/moonlight", "stream", "STATE-PC", "Hades"]


# ---------------------------------------------------------------------------
# the per-host list cache
# ---------------------------------------------------------------------------


def test_a_successful_list_writes_the_per_host_cache_with_the_slug_rule(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Desktop"])
    result = world.run(["list"])
    assert result.code == 0, result.err

    cache_file = world.cache_path.parent / "hosts" / f"{hosts.slug(HOST)}.json"
    assert cache_file.is_file()
    data = json.loads(cache_file.read_text())
    assert data == {"host": HOST, "when": data["when"], "apps": ["Elden Ring", "Desktop"]}


def test_sync_also_refreshes_the_per_host_cache(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    result = world.run(["sync"])
    assert result.code == 0, result.err
    cache_file = world.cache_path.parent / "hosts" / f"{hosts.slug(HOST)}.json"
    assert cache_file.is_file()


def test_list_cached_serves_the_cache_without_running_moonlight(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Desktop"])
    first = world.run(["list"])
    assert first.code == 0, first.err

    # A live list would now fail outright; --cached must never try it.
    install_fake_moonlight(tmp_path, monkeypatch, "", exit_code=1)
    cached = world.run(["list", "--cached"])
    assert cached.code == 0, cached.err
    assert "added    Elden Ring" in cached.out


def test_list_cached_with_no_cache_file_exits_3(fresh_world):
    result = fresh_world.run(["list", "--cached"])
    assert result.code == 3
    assert "no cached app list for host" in result.err


def test_sync_never_reads_the_per_host_cache(world, tmp_path, monkeypatch):
    """A stale cache must not change what `sync` plans -- it only writes one."""
    stale_dir = world.cache_path.parent / "hosts"
    hosts.write_host_cache(stale_dir, HOST, ["Some Stale Title"])
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    result = world.run(["sync"])
    assert result.code == 0, result.err
    assert "Some Stale Title" not in result.out
    # The live list (not the stale cache) is what got written back.
    data = json.loads((stale_dir / f"{hosts.slug(HOST)}.json").read_text())
    assert data["apps"] == ["Elden Ring"]


# ---------------------------------------------------------------------------
# `list`'s same-game-as report (spec 3.4.8, 3.12)
# ---------------------------------------------------------------------------


def test_list_reports_same_game_as_for_a_differently_named_shared_title(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Sea of Stars (GOG)"])
    cache = MatchCache(world.cache_path)
    cache.put(Match(name="Sea of Stars (GOG)", steam_appid=1234, how="steamstore:exact"))
    cache.put(Match(name="Sea of Stars", steam_appid=1234, how="steamstore:exact"))
    hosts_dir = world.cache_path.parent / "hosts"
    hosts.write_host_cache(hosts_dir, OFFICE, ["Sea of Stars", "Something Else"])

    result = world.run(["list"])
    assert result.code == 0, result.err
    assert f"same-game-as: Sea of Stars ({OFFICE})" in result.out


def test_list_does_not_report_an_identically_spelled_shared_title(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    cache = MatchCache(world.cache_path)
    cache.put(Match(name="Elden Ring", steam_appid=1245620, how="steamstore:exact"))
    hosts_dir = world.cache_path.parent / "hosts"
    hosts.write_host_cache(hosts_dir, OFFICE, ["Elden Ring"])

    result = world.run(["list"])
    assert result.code == 0, result.err
    assert "same-game-as" not in result.out
