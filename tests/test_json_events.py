"""The ``--json`` event stream (spec 3.4.6; PR-1 acceptance).

Builds on the ``world`` / ``fresh_world`` fixtures, ``MANIFEST_TITLES`` and
``host_publishes`` from ``tests/test_sync_e2e.py`` (spec 4 preamble: new
tests import those rather than editing the frozen e2e file) plus the art
fixtures used elsewhere for network failure simulation.
"""

from __future__ import annotations

import io
import json
import os

from moonlight_steam_sync import config as config_module
from moonlight_steam_sync import moonlight, sync
from moonlight_steam_sync.__main__ import build_parser
from moonlight_steam_sync.__main__ import main as main_module_main
from moonlight_steam_sync.art.apply import ArtTarget
from moonlight_steam_sync.art.cli import build_services, cmd_search, cmd_status
from moonlight_steam_sync.config import Config
from tests.art_fixtures import BulkTransport, FakeTransport, make_fetcher
from tests.fakes import STEAMID3, FakeRunner, install_fake_moonlight, make_steam_root
from tests.test_art_cli import FakeShortcuts, grid_dir, run_art_cmd  # noqa: F401
from tests.test_sync_e2e import (  # noqa: F401
    HOST,
    MANIFEST_TITLES,
    fresh_world,
    host_publishes,
    make_config,
    world,
)


def _json_lines(text: str) -> list[dict]:
    lines = [line for line in text.splitlines() if line]
    parsed = [json.loads(line) for line in lines]
    # spec 3.4.6: under --json, stdout carries one JSON object per line and
    # nothing else.
    for line in lines:
        json.loads(line)
    return parsed


# ---------------------------------------------------------------------------
# sync --json: every documented event, and nothing but JSON on stdout
# ---------------------------------------------------------------------------


def test_sync_json_emits_every_documented_event(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, [*MANIFEST_TITLES, "Desktop", "Steam Big Picture"])
    result = world.run(["--json", "sync"])
    assert result.code == 0, result.err

    events = _json_lines(result.out)
    names = [e["event"] for e in events]
    assert names[0] == "start"
    assert events[0] == {
        "event": "start",
        "schema": 1,
        "version": events[0]["version"],
        "command": "sync",
    }
    assert "plan" in names
    assert "title" in names
    assert "commit" in names
    assert names[-1] == "summary"

    plan = next(e for e in events if e["event"] == "plan")
    for key in (
        "host",
        "published",
        "ignored",
        "present",
        "to_add",
        "to_replace",
        "to_park",
        "to_unpark",
        "to_remove",
        "pending",
        "limit",
        "stream",
        "shortcut",
        "parked",
        "duplicates",
    ):
        assert key in plan
    assert plan["host"] == HOST

    titles = [e for e in events if e["event"] == "title"]
    assert len(titles) == 5  # 2 adopted + 3 new, all dressed this run
    for title in titles:
        for key in ("index", "total", "name", "kind", "appid", "match", "slots"):
            assert key in title
        assert title["kind"] == "shortcut"  # PR-1: no owned-apps yet

    commit = next(e for e in events if e["event"] == "commit")
    assert commit["written"] is True
    assert commit["restarted"] is True

    summary = next(e for e in events if e["event"] == "summary")
    assert summary["added"] == 3
    assert summary["exit"] == 0
    assert summary["stop_reason"] is None


def test_sync_json_human_output_moves_to_stderr(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™", "Desktop"])
    result = world.run(["--json", "sync"])
    assert result.code == 0, result.err
    assert "already present" in result.err
    for line in result.out.splitlines():
        json.loads(line)  # never a bare human line on stdout


def test_sync_without_json_is_unaffected_by_the_reporter(world, tmp_path, monkeypatch):
    """Byte-identity spot check (spec 3.11): no --json, no new output."""
    host_publishes(tmp_path, monkeypatch, [*MANIFEST_TITLES, "Desktop", "Steam Big Picture"])
    result = world.run(["sync"])
    assert result.code == 0, result.err
    assert "7 app(s) published, 2 ignored, 2 already in Steam, 3 to add" in result.out
    assert result.out.count("{") == 0  # no stray JSON


# ---------------------------------------------------------------------------
# error events carry the right exit code
# ---------------------------------------------------------------------------


def test_error_event_on_exit_1_no_host_configured(fresh_world, tmp_path):
    config = make_config(tmp_path, host="")
    result = fresh_world.run(["--json", "sync"], config=config)
    assert result.code == 1
    events = _json_lines(result.out)
    assert events[-1] == {"event": "error", "exit": 1, "message": events[-1]["message"]}
    assert "no host configured" in events[-1]["message"]


def test_error_event_on_exit_2_steam_running_and_restart_refused(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["A New Game"])
    config = make_config(tmp_path, restart_steam=False)
    result = world.run(["--json", "sync"], config=config, runner=FakeRunner(running=True))
    assert result.code == 2
    events = _json_lines(result.out)
    assert events[-1]["event"] == "error"
    assert events[-1]["exit"] == 2


def test_error_event_on_exit_3_moonlight_unreachable(world, tmp_path, monkeypatch):
    install_fake_moonlight(tmp_path, monkeypatch, "", exit_code=1)
    result = world.run(["--json", "sync"])
    assert result.code == 3
    events = _json_lines(result.out)
    assert events[-1]["event"] == "error"
    assert events[-1]["exit"] == 3


def test_error_event_on_exit_4_hard_stop_after_summary(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Only Title"])
    transport = BulkTransport(["Only Title"], rate_limit_after=0)
    result = world.run(["--json", "sync"], transport=transport)
    assert result.code == 4
    events = _json_lines(result.out)
    kinds = [e["event"] for e in events]
    assert "summary" in kinds
    assert kinds[-1] == "error"
    assert events[-1]["exit"] == 4
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["stopped_early"] is True


def test_error_event_on_exit_130_interrupted(fresh_world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Only Title"])
    transport = BulkTransport(["Only Title"], crash_after=0, crash_with=KeyboardInterrupt())
    result = fresh_world.run(["--json", "sync"], transport=transport)
    assert result.code == 130
    events = _json_lines(result.out)
    assert events[-1]["event"] == "error"
    assert events[-1]["exit"] == 130
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["stop_reason"] == "interrupted"


# ---------------------------------------------------------------------------
# status --json: entry events, and --owned-apps validation (spec 3.4.1)
# ---------------------------------------------------------------------------


def test_status_json_entry_events_have_the_documented_keys(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    args = build_parser().parse_args(["--json", "status"])
    out, err = io.StringIO(), io.StringIO()
    code = cmd_status(args, world.config, cache_path=world.cache_path, out=out, err=err)
    assert code == 0, err.getvalue()

    events = _json_lines(out.getvalue())
    assert events[0]["event"] == "start"
    assert events[-1]["event"] == "end"
    entries = [e for e in events if e["event"] == "entry"]
    assert entries  # Elden Ring and Hollow Knight are owned in the PR-2 fixture
    for entry in entries:
        for key in (
            "name",
            "app_name",
            "appid",
            "hidden",
            "parked",
            "published",
            "client",
            "match",
            "slots",
            "stale_art",
            "cached",
            "cached_when",
        ):
            assert key in entry
        assert entry["parked"] is False
        assert entry["client"] is False
        assert entry["stale_art"] is False


def test_status_json_notes_the_missing_host_cache(world, tmp_path, monkeypatch):
    """Spec 3.4.6/3.12: the missing-cache note is a ``note`` event under
    ``--json`` (and only under ``--json`` -- see the byte-identity test
    below)."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    args = build_parser().parse_args(["--json", "status"])
    out, err = io.StringIO(), io.StringIO()
    code = cmd_status(args, world.config, cache_path=world.cache_path, out=out, err=err)
    assert code == 0, err.getvalue()
    events = _json_lines(out.getvalue())
    notes = [e for e in events if e["event"] == "note"]
    assert any("no cached app list for host" in n["message"] for n in notes)
    assert "note: no cached app list for host" in err.getvalue()


def test_status_without_json_is_byte_identical_to_v0_2_0(world, tmp_path, monkeypatch):
    """Regression test for the PR-1 review finding: plain `status` (no
    `--json`, no per-host cache written yet) must print nothing at all to
    stderr, exactly as it did in v0.2.0 -- the new "no cached app list"
    note must never leak onto stderr outside `--json` (spec 3.11)."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    args = build_parser().parse_args(["status"])
    out, err = io.StringIO(), io.StringIO()
    code = cmd_status(args, world.config, cache_path=world.cache_path, out=out, err=err)
    assert code == 0
    assert err.getvalue() == ""
    assert "shortcut(s)," in out.getvalue()


def test_status_owned_apps_file_must_match_the_picked_steam_user(world):
    owned_path = world.tmp_path / "owned-apps.json"
    owned_path.write_text(json.dumps({"version": 1, "steamid3": STEAMID3 + 1, "apps": {}}))
    args = build_parser().parse_args(["status", "--owned-apps", str(owned_path)])

    out, err = io.StringIO(), io.StringIO()
    code = cmd_status(args, world.config, cache_path=world.cache_path, out=out, err=err)
    assert code == 1
    assert "owned-apps file is for Steam user" in err.getvalue()


def test_status_owned_apps_bad_file_is_exit_1_before_anything_else(tmp_path):
    owned_path = tmp_path / "missing.json"
    args = build_parser().parse_args(["status", "--owned-apps", str(owned_path)])
    out, err = io.StringIO(), io.StringIO()
    code = cmd_status(args, Config(), out=out, err=err)
    assert code == 1
    assert "status: " in err.getvalue()


# ---------------------------------------------------------------------------
# search: Steam then SGDB, de-duplicated
# ---------------------------------------------------------------------------


def test_search_json_lists_sgdb_candidates_with_a_steam_appid_lookup(tmp_path):
    config = Config(sgdb_api_key="fixture-key")
    transport = FakeTransport()
    services = build_services(
        config, cache_path=tmp_path / "matches.json", fetcher=make_fetcher(transport)
    )
    args = build_parser().parse_args(["--json", "search", "Elden Ring"])
    out, err = io.StringIO(), io.StringIO()
    code = cmd_search(args, config, services=services, out=out, err=err)
    assert code == 0, err.getvalue()

    events = _json_lines(out.getvalue())
    assert events[0]["event"] == "start"
    assert events[-1]["event"] == "end"
    candidates = [e for e in events if e["event"] == "candidate"]
    assert [c["source"] for c in candidates] == ["sgdb", "sgdb"]
    verified = next(c for c in candidates if c["id"] == 5297)
    assert verified["verified"] is True
    assert verified["steam_appid"] == 1245620
    assert verified["owned"] is False
    unverified = next(c for c in candidates if c["id"] == 9999)
    assert unverified["verified"] is False
    assert unverified["steam_appid"] is None  # the games/id lookup 404s


class _FakeSgdbClient:
    """A minimal stand-in for ``SgdbClient`` (spec 3.4.8 dedup test)."""

    def __init__(self, items: list[dict], games: dict[int, dict]) -> None:
        self._items = items
        self._games = games

    def search(self, term: str) -> list[dict]:
        return self._items

    def game(self, sgdb_id: int) -> dict:
        return self._games.get(sgdb_id, {})


class _FakeSteamStoreClient:
    """A minimal stand-in for ``SteamStoreClient`` (spec 3.4.8 dedup test)."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def search(self, term: str) -> list[dict]:
        return self._items


def test_search_lists_steam_then_sgdb_candidates_de_duplicated(tmp_path):
    """Spec 3.4.6/3.4.8: SGDB first, then Steam store, with a Steam
    candidate whose appid equals an SGDB candidate's ``steam_appid``
    dropped (SGDB wins)."""
    config = Config(sgdb_api_key="fixture-key")
    services = build_services(config, cache_path=tmp_path / "matches.json")
    services.sgdb = _FakeSgdbClient(
        items=[
            {"id": 1, "name": "Elden Ring", "verified": True},
            {"id": 2, "name": "Elden Ring Nightreign", "verified": False},
        ],
        games={
            1: {"external_platform_data": {"steam": [{"id": "1245620"}]}},
            2: {"external_platform_data": {}},
        },
    )
    services.store = _FakeSteamStoreClient(
        items=[
            # Same Steam appid as the first SGDB candidate: dropped.
            {"id": 1245620, "name": "ELDEN RING"},
            # A distinct Steam release: kept.
            {"id": 999999, "name": "Elden Ring Companion App"},
        ]
    )
    args = build_parser().parse_args(["--json", "search", "Elden Ring"])
    out, err = io.StringIO(), io.StringIO()
    code = cmd_search(args, config, services=services, out=out, err=err)
    assert code == 0, err.getvalue()

    events = _json_lines(out.getvalue())
    candidates = [e for e in events if e["event"] == "candidate"]
    assert [c["source"] for c in candidates] == ["sgdb", "sgdb", "steam"]
    assert [c["steam_appid"] for c in candidates] == [1245620, None, 999999]
    # The Steam-store hit that duplicates the first SGDB candidate never
    # appears at all.
    assert 1245620 not in [c["id"] for c in candidates if c["source"] == "steam"]


def test_search_without_a_key_uses_steam_store_only(tmp_path):
    config = Config(sgdb_api_key="")
    transport = FakeTransport()
    services = build_services(
        config, cache_path=tmp_path / "matches.json", fetcher=make_fetcher(transport)
    )
    args = build_parser().parse_args(["search", "Totally Unknown Title"])
    out, err = io.StringIO(), io.StringIO()
    code = cmd_search(args, config, services=services, out=out, err=err)
    assert code == 0, err.getvalue()
    assert "no SteamGridDB API key configured" in err.getvalue()
    assert "no candidates" in out.getvalue()


# ---------------------------------------------------------------------------
# art --json: commit/summary key sets and event ordering (repair-pass tests)
# ---------------------------------------------------------------------------

_ART_SUMMARY_KEYS = {
    "added",
    "replaced",
    "removed",
    "filled",
    "missing",
    "unmatched",
    "duplicates",
    "pending",
    "stopped_early",
    "stop_reason",
    "exit",
}
_ART_COMMIT_KEYS = {"written", "restarted", "backup", "relaunch_error"}


def test_art_json_commit_and_summary_carry_the_full_schema(tmp_path, grid_dir):
    code, out, err, _, _ = run_art_cmd(tmp_path, grid_dir, ["--json", "art"])
    assert code == 0, err

    events = _json_lines(out)
    assert events[0]["event"] == "start"
    commit = next(e for e in events if e["event"] == "commit")
    assert set(commit) - {"event"} == _ART_COMMIT_KEYS
    summary = next(e for e in events if e["event"] == "summary")
    assert set(summary) - {"event"} == _ART_SUMMARY_KEYS
    assert summary["filled"] > 0
    assert summary["added"] == summary["replaced"] == summary["removed"] == 0


def test_art_json_hard_stop_emits_summary_then_error(tmp_path, grid_dir):
    """Spec 3.4.6: on exit 4 (and 130), `summary` comes before `error`, the
    last line on stdout."""
    provider = FakeShortcuts(
        [ArtTarget(name="Rate Limited Game", appid=2800000006, grid_dir=grid_dir)]
    )
    code, out, err, _, _ = run_art_cmd(
        tmp_path, grid_dir, ["--json", "art"], provider=provider
    )
    assert code == 4, err

    events = _json_lines(out)
    names = [e["event"] for e in events]
    assert names[-1] == "error"
    assert names[-2] == "summary"
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["stopped_early"] is True
    assert summary["exit"] == 4


def test_art_json_ctrl_c_emits_summary_then_error(tmp_path, grid_dir):
    """Same ordering rule as the hard-stop case, for a real SIGINT (spec
    3.9.5, 3.4.6)."""
    url = "https://www.steamgriddb.com/api/v2/search/autocomplete/Elden%20Ring"
    transport = FakeTransport(fail_with={url: KeyboardInterrupt()})
    code, out, err, _, _ = run_art_cmd(
        tmp_path, grid_dir, ["--json", "art", "--only", "Elden Ring"], transport=transport
    )
    assert code == 130

    events = _json_lines(out)
    names = [e["event"] for e in events]
    assert names[-1] == "error"
    assert names[-2] == "summary"
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["stopped_early"] is True
    assert summary["stop_reason"] == "interrupted"
    error = events[-1]
    assert error["exit"] == 130
    assert "resume with the same command" in error["message"]


# ---------------------------------------------------------------------------
# doctor --json: report moves to stderr, stdout is start then end
# ---------------------------------------------------------------------------


def test_doctor_json_moves_the_report_to_stderr(tmp_path, monkeypatch, capsys):
    key_file = tmp_path / "sgdb-api-key"
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", key_file)
    monkeypatch.delenv("SGDB_API_KEY", raising=False)

    exit_code = main_module_main(["--json", "doctor"])
    assert exit_code == 0
    out, err = capsys.readouterr()

    events = _json_lines(out)
    assert events[0] == {
        "event": "start",
        "schema": 1,
        "version": events[0]["version"],
        "command": "doctor",
    }
    assert events[-1]["event"] == "end"
    assert events[-1]["count"] > 0
    assert "python:" in err
    assert "steam dir:" in err
    # Nothing but the two JSON lines on stdout.
    assert "python:" not in out


# ---------------------------------------------------------------------------
# launch --json: start then exec; error on every failure path
# ---------------------------------------------------------------------------


def test_launch_json_emits_start_then_exec(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text('host = "MY-GAMING-PC"\n')
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")
    monkeypatch.setattr(moonlight, "find_binary", lambda: ["/usr/bin/moonlight"])
    monkeypatch.setattr(os, "execvp", lambda file, args: None)

    exit_code = main_module_main(["--json", "launch", "Elden Ring"])
    assert exit_code == 0
    out = capsys.readouterr().out

    events = _json_lines(out)
    assert events[0]["event"] == "start"
    assert events[1] == {"event": "exec", "host": "MY-GAMING-PC", "name": "Elden Ring"}


def test_launch_json_no_host_configured_is_an_error_event(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")

    exit_code = main_module_main(["--json", "launch", "Elden Ring"])
    assert exit_code == 1
    out = capsys.readouterr().out

    events = _json_lines(out)
    assert events[0]["event"] == "start"
    assert events[-1]["event"] == "error"
    assert events[-1]["exit"] == 1
    assert "no host configured" in events[-1]["message"]


def test_launch_json_moonlight_unreachable_is_an_error_event(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.toml"
    config_path.write_text('host = "MY-GAMING-PC"\n')
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")
    monkeypatch.setattr(moonlight, "find_binary", lambda: None)

    exit_code = main_module_main(["--json", "launch", "Elden Ring"])
    assert exit_code == 3
    out = capsys.readouterr().out

    events = _json_lines(out)
    assert events[-1]["event"] == "error"
    assert events[-1]["exit"] == 3


# ---------------------------------------------------------------------------
# a bare KeyboardInterrupt (not caught inside sync's own handler) still ends
# in an `error` event -- main()'s top-level fallback (repair-pass test)
# ---------------------------------------------------------------------------


def test_bare_keyboard_interrupt_during_list_emits_an_error_event(tmp_path, monkeypatch, capsys):
    """A Ctrl-C during ``moonlight list`` (a subprocess call) escapes
    ``cmd_list`` entirely and used to reach ``main()``'s fallback handler
    with no ``error`` event at all under ``--json`` (spec 3.4.6: every
    non-zero exit ends in one)."""
    make_steam_root(tmp_path / "steam")
    monkeypatch.setenv("STEAM_ROOT", str(tmp_path / "steam"))
    config_path = tmp_path / "config.toml"
    config_path.write_text('host = "MY-GAMING-PC"\n')
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")

    def raising_list_apps(_host):
        raise KeyboardInterrupt

    deps = sync.Deps(list_apps=raising_list_apps, cache_path=tmp_path / "matches.json")
    exit_code = main_module_main(["--json", "list"], deps=deps)
    assert exit_code == 130
    out, err = capsys.readouterr()

    events = _json_lines(out)
    assert events[-1]["event"] == "error"
    assert events[-1]["exit"] == 130
    assert "resume with the same command" in events[-1]["message"]
    assert "resume with the same command" in err
