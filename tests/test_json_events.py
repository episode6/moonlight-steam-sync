"""The ``--json`` event stream (spec 3.4.6; PR-1 acceptance).

Builds on the ``world`` / ``fresh_world`` fixtures, ``MANIFEST_TITLES`` and
``host_publishes`` from ``tests/test_sync_e2e.py`` (spec 4 preamble: new
tests import those rather than editing the frozen e2e file) plus the art
fixtures used elsewhere for network failure simulation.
"""

from __future__ import annotations

import io
import json

from moonlight_steam_sync.__main__ import build_parser
from moonlight_steam_sync.art.cli import build_services, cmd_search, cmd_status
from moonlight_steam_sync.config import Config
from tests.art_fixtures import BulkTransport, FakeTransport, make_fetcher
from tests.fakes import STEAMID3, FakeRunner, install_fake_moonlight
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
