"""Default host apps: kind ``host-app`` and ``--hide-host-apps`` (decky spec 3.14).

Every Sunshine / Apollo host publishes ``Desktop`` and ``Steam Big
Picture``. Under the flag they are written as hidden shortcuts that keep
their ordinary ``AppName`` -- and so their appid, art and controller layout
-- because the Decky plugin launches them from panel buttons instead of
library tiles. Builds on the helpers of ``tests/test_sync_owned.py`` and the
``world`` fixtures of ``tests/test_sync_e2e.py``.
"""

from __future__ import annotations

import dataclasses

import pytest

from moonlight_steam_sync import moonlight, sync
from moonlight_steam_sync.art.resolve import Match
from moonlight_steam_sync.reporting import is_default_host_app
from moonlight_steam_sync.shortcuts import Shortcut
from tests.fakes import STREAM_SH
from tests.test_sync_e2e import (  # noqa: F401 - pytest fixtures
    SUFFIX,
    World,
    fresh_world,
    host_publishes,
    isolated_config,
    make_config,
    world,
)
from tests.test_sync_owned import (
    ELDEN_OWNED,
    ELDEN_STEAM,
    entry,
    json_lines,
    maybe_entry,
    owned_file,
    run_art,
    run_status,
    seed_cache,
    snapshot,
)

DESKTOP, BIG_PICTURE = "Desktop", "Steam Big Picture"
FLAG = "--hide-host-apps"


@pytest.fixture
def desk(fresh_world, tmp_path) -> World:
    """``fresh_world`` without the e2e config's ``ignore`` list, which names
    these very two apps."""
    return dataclasses.replace(fresh_world, config=make_config(tmp_path, ignore=[]))


@pytest.fixture
def seeded(world, tmp_path) -> World:
    """``world`` (Elden Ring and Hollow Knight adoptable), nothing ignored."""
    return dataclasses.replace(world, config=make_config(tmp_path, ignore=[]))


def shortcut_appid(name: str) -> int:
    """The appid of the ordinary ``shortcut`` entry for *name*."""
    return Shortcut.create(app_name=f"{name}{SUFFIX}", exe=STREAM_SH).appid


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Desktop", True),
        ("Steam Big Picture", True),
        ("  desktop ", True),
        ("STEAM BIG PICTURE", True),
        ("Desktop Dungeons", False),
        ("Steam", False),
        ("", False),
    ],
)
def test_the_name_rule_is_the_plugins(name, expected):
    """Trimmed, case-insensitive, equal to one of the two names -- the same
    comparison the plugin's panel buttons make (spec 3.14)."""
    assert is_default_host_app(name) is expected


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def test_without_the_flag_the_two_are_ordinary_visible_shortcuts(
    desk, tmp_path, monkeypatch
):
    """Off by default (decision 36): a plain CLI user has no panel buttons,
    so the tiles are their only launcher. No ``host-app`` key or kind leaks
    into the JSON either."""
    host_publishes(tmp_path, monkeypatch, [DESKTOP, BIG_PICTURE])

    result = desk.run(["--json", "sync", "--no-art"])

    assert result.code == 0, result.err
    assert all(s.is_hidden == 0 for s in desk.owned())
    events = json_lines(result.out)
    plan = next(e for e in events if e["event"] == "plan")
    assert "host_app" not in plan and plan["shortcut"] == 2
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["added_by_kind"] == {"stream": 0, "shortcut": 2}


def test_new_host_apps_are_written_hidden_under_their_ordinary_name(
    desk, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, [DESKTOP, "Fan Made Adventure", "steam big picture"])

    result = desk.run(["--json", "sync", "--no-art", FLAG])

    assert result.code == 0, result.err
    desktop = entry(desk, DESKTOP)
    assert desktop.app_name == f"{DESKTOP}{SUFFIX}" and desktop.is_hidden == 1
    assert desktop.appid == shortcut_appid(DESKTOP)
    assert desktop.launch_options == f'launch "{DESKTOP}"'
    assert entry(desk, "steam big picture").is_hidden == 1
    assert entry(desk, "Fan Made Adventure").is_hidden == 0
    events = json_lines(result.out)
    plan = next(e for e in events if e["event"] == "plan")
    assert plan["host_app"] == 2 and plan["shortcut"] == 1 and plan["stream"] == 0
    assert plan["to_add"] == 3 and plan["to_park"] == 0
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["added_by_kind"] == {"stream": 0, "shortcut": 1, "host-app": 2}


def test_an_existing_tile_is_hidden_in_place_never_replaced(desk, tmp_path, monkeypatch):
    """Spec 3.14 / decision 37: same ``AppName``, same appid, so the flip is
    ``to hide`` -- no ``replace`` event, no grid rename, no HTTP call -- and
    the tile's art and controller layout (keyed by appid) survive."""
    host_publishes(tmp_path, monkeypatch, [DESKTOP, BIG_PICTURE])
    assert desk.run(["sync"]).code == 0
    before = entry(desk, DESKTOP)
    assert before.is_hidden == 0
    grid_before = desk.grid_files()

    dry = desk.run(["sync", "--dry-run", "--no-art", FLAG])
    assert dry.code == 0, dry.err
    assert "2 already in Steam, 0 to add, 2 to hide" in dry.out
    assert f"  hide     {DESKTOP} [{before.appid}] (host app, hidden by its kind)" in dry.out
    assert entry(desk, DESKTOP).is_hidden == 0

    result = desk.run(["--json", "sync", FLAG, "--limit", "0"])

    assert result.code == 0, result.err
    events = json_lines(result.out)
    assert not any(e["event"] == "replace" for e in events)
    plan = next(e for e in events if e["event"] == "plan")
    assert plan["to_replace"] == 0 and plan["to_add"] == 0 and plan["pending"] == 0
    assert plan["to_park"] == 0 and plan["parked"] == 0
    assert ", hidden 2" in result.err
    after = entry(desk, DESKTOP)
    assert after.appid == before.appid and after.is_hidden == 1
    assert after.app_name == before.app_name and after.icon == before.icon
    assert entry(desk, BIG_PICTURE).is_hidden == 1
    assert desk.grid_files() == grid_before
    assert result.runner.shutdowns == 1
    titles = {e["name"]: e["kind"] for e in events if e["event"] == "title"}
    assert titles == {DESKTOP: "host-app", BIG_PICTURE: "host-app"}

    # Done is done: the rerun writes nothing and restarts nothing.
    state = snapshot(desk)
    again = desk.run(["sync", FLAG])
    assert again.code == 0 and again.runner.shutdowns == 0
    assert snapshot(desk) == state


def test_ignored_wins_over_host_app(desk, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, [DESKTOP, BIG_PICTURE])
    ignore = tmp_path / "ignore.json"
    ignore.write_text('["Desktop"]', encoding="utf-8")

    result = desk.run(["sync", "--no-art", FLAG, "--ignore-file", str(ignore)])

    assert result.code == 0, result.err
    assert maybe_entry(desk, DESKTOP) is None
    assert entry(desk, BIG_PICTURE).is_hidden == 1


def test_host_app_wins_over_stream_and_never_takes_the_owned_game(seeded, tmp_path, monkeypatch):
    """Spec 3.14 precedence: decided on the name, before ``stream``. A pin
    of ``Desktop`` to an owned game changes its art, not its kind, and the
    title that really is that game still gets the Stream button (it is not
    a ``duplicate`` of ``Desktop``)."""
    host_publishes(tmp_path, monkeypatch, [DESKTOP, "Elden Ring"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    seed_cache(
        seeded,
        Match(name=DESKTOP, steam_appid=ELDEN_STEAM, matched_name=ELDEN_OWNED, how="pinned"),
    )

    result = seeded.run(["--json", "sync", "--no-art", "--owned-apps", owned, FLAG])

    assert result.code == 0, result.err
    desktop = entry(seeded, DESKTOP)
    assert desktop.app_name == f"{DESKTOP}{SUFFIX}" and desktop.is_hidden == 1
    elden = entry(seeded, "Elden Ring")
    assert elden.app_name == ELDEN_OWNED and elden.is_hidden == 1
    plan = next(e for e in json_lines(result.out) if e["event"] == "plan")
    assert plan["host_app"] == 1 and plan["stream"] == 1 and plan["duplicates"] == {}


def test_parking_and_unparking_leave_a_host_app_hidden(desk, tmp_path, monkeypatch):
    """Spec 3.12 / 3.14: unpublished -> parked like any entry (already
    hidden, so no flip); published again -> "unparked into its kind", which
    for a host app is hidden. Dropping the flag is what shows the tile."""
    argv = ["sync", "--no-art", "--park-unpublished"]
    host_publishes(tmp_path, monkeypatch, [DESKTOP, "Fan Made Adventure"])
    assert desk.run([*argv, FLAG]).code == 0

    host_publishes(tmp_path, monkeypatch, ["Fan Made Adventure"])
    away = desk.run(["--json", *argv, FLAG])
    plan = next(e for e in json_lines(away.out) if e["event"] == "plan")
    assert plan["to_park"] == 0 and plan["parked"] == 1
    assert entry(desk, DESKTOP).is_hidden == 1

    host_publishes(tmp_path, monkeypatch, [DESKTOP, "Fan Made Adventure"])
    back = desk.run(["--json", *argv, FLAG])
    plan = next(e for e in json_lines(back.out) if e["event"] == "plan")
    assert plan["to_unpark"] == 0 and plan["parked"] == 0 and plan["host_app"] == 1
    assert entry(desk, DESKTOP).is_hidden == 1

    shown = desk.run(argv)
    assert shown.code == 0 and "1 to unpark" in shown.out
    assert entry(desk, DESKTOP).is_hidden == 0


def test_a_plain_sync_leaves_a_hidden_host_app_alone(desk, tmp_path, monkeypatch):
    """Spec 3.11: without a flag that says what the kinds are, ``IsHidden``
    is never touched."""
    host_publishes(tmp_path, monkeypatch, [DESKTOP])
    assert desk.run(["sync", "--no-art", FLAG]).code == 0
    state = snapshot(desk)

    result = desk.run(["sync", "--no-art"])

    assert result.code == 0 and snapshot(desk) == state


def test_remove_all_removes_hidden_host_apps(desk, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, [DESKTOP, BIG_PICTURE])
    assert desk.run(["sync", "--no-art", FLAG]).code == 0

    result = desk.run(["remove", "--all"])

    assert result.code == 0, result.err
    assert desk.owned() == []


# ---------------------------------------------------------------------------
# list, status, art
# ---------------------------------------------------------------------------


def test_list_reports_the_kind_only_under_the_flag(desk, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, [DESKTOP, "Fan Made Adventure"])
    assert desk.run(["sync", "--no-art", FLAG]).code == 0

    flagged = desk.run(["--json", "list", FLAG])
    apps = {e["name"]: e for e in json_lines(flagged.out) if e["event"] == "app"}
    assert apps[DESKTOP]["kind"] == "host-app" and apps[DESKTOP]["label"] == "added"
    assert apps["Fan Made Adventure"]["kind"] == "shortcut"

    plain = desk.run(["--json", "list"])
    apps = {e["name"]: e for e in json_lines(plain.out) if e["event"] == "app"}
    assert apps[DESKTOP]["kind"] == "shortcut"


def test_status_does_not_call_a_hidden_host_app_parked(desk, tmp_path, monkeypatch):
    """The plugin's panel buttons need ``published`` and not ``parked``
    (spec 3.14). Without the flag ``status`` is v0.3.1's: a hidden entry
    under its own name reads as parked, and there is no ``host_app`` key."""
    host_publishes(tmp_path, monkeypatch, [DESKTOP, "Fan Made Adventure"])
    assert desk.run(["sync", "--no-art", FLAG]).code == 0

    code, events, _ = run_status(desk, ["--json", "status", FLAG])
    assert code == 0
    entries = {e["name"]: e for e in events if e["event"] == "entry"}
    desktop = entries[DESKTOP]
    assert desktop["hidden"] and desktop["published"] and not desktop["parked"]
    assert desktop["host_app"] is True
    assert desktop["appid"] == shortcut_appid(DESKTOP)
    assert entries["Fan Made Adventure"]["host_app"] is False

    # The active host stops publishing it: parked, like a stream entry.
    host_publishes(tmp_path, monkeypatch, ["Fan Made Adventure"])
    assert desk.run(["list"]).code == 0
    _, events, _ = run_status(desk, ["--json", "status", FLAG])
    desktop = next(e for e in events if e.get("name") == DESKTOP)
    assert desktop["parked"] and not desktop["published"] and desktop["host_app"]

    host_publishes(tmp_path, monkeypatch, [DESKTOP, "Fan Made Adventure"])
    assert desk.run(["list"]).code == 0
    _, events, _ = run_status(desk, ["--json", "status"])
    desktop = next(e for e in events if e.get("name") == DESKTOP)
    assert desktop["parked"] and "host_app" not in desktop


def test_art_calls_a_hidden_host_app_by_its_kind_not_stream(desk, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, [DESKTOP, "Fan Made Adventure"])
    assert desk.run(["sync", "--no-art", FLAG]).code == 0

    result = run_art(desk, ["--json", "art"])

    assert result.code == 0, result.err
    kinds = {e["name"]: e["kind"] for e in json_lines(result.out) if e["event"] == "title"}
    assert kinds == {DESKTOP: "host-app", "Fan Made Adventure": "shortcut"}


def test_build_plan_hides_an_entry_adopted_under_another_name(desk, tmp_path, monkeypatch):
    """Without ``--owned-apps`` an owned entry is adopted under any
    ``AppName`` (v0.2.0); the flag hides that entry in place just the same."""
    host_publishes(tmp_path, monkeypatch, [DESKTOP])
    assert desk.run(["sync", "--no-art"]).code == 0
    library = desk.library()
    renamed = next(iter(library.owned(desk.config.exe)))
    renamed.app_name = "My PC"
    library.write()

    plan = sync.build_plan(
        desk.config,
        [moonlight.App(name=DESKTOP)],
        desk.library(),
        hide_host_apps=True,
    )

    assert [s.app_name for s in plan.to_hide] == ["My PC"]
    assert not plan.to_replace and not plan.to_add
    assert plan.kinds == {DESKTOP: sync.KIND_HOST_APP}
