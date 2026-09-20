"""Owned apps, hidden entries, replacements, duplicates, parking and the
client shortcut (decky spec 3.2, 3.3, 3.4.1, 3.4.2, 3.4.4, 3.4.5, 3.12;
PR-3).

Builds on the ``world`` / ``fresh_world`` fixtures and helpers from
``tests/test_sync_e2e.py`` (spec 4 preamble: that file is frozen, new
tests import from it) and the recorded artwork manifest behind
:class:`tests.art_fixtures.FakeTransport`. Fixture ids used below, all from
``tests/fixtures/art/``: Elden Ring is SteamGridDB 5297 / Steam 1245620,
Hollow Knight 5241 / 367520, Hades II 5468 / 1145350. The owned-apps files
are written the way the Decky plugin writes them (spec 3.4.1).
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from moonlight_steam_sync import hosts, moonlight, sync
from moonlight_steam_sync.__main__ import build_parser, main
from moonlight_steam_sync.art.cli import build_services, cmd_art, cmd_match, cmd_status
from moonlight_steam_sync.art.resolve import Match, MatchCache
from moonlight_steam_sync.config import default_exe, load_config
from moonlight_steam_sync.shortcuts import (
    CLIENT_APP_NAME,
    CLIENT_LAUNCH_OPTIONS,
    Shortcut,
    ShortcutsFile,
)
from tests.art_fixtures import FakeTransport, make_fetcher
from tests.fakes import STEAMID3, STREAM_SH, FakeRunner, install_fake_moonlight, moonlight_list
from tests.test_sync_e2e import (  # noqa: F401 - pytest fixtures
    HOST,
    SUFFIX,
    TEMPLATE,
    Result,
    World,
    expected_grid_files,
    fresh_world,
    host_publishes,
    isolated_config,
    make_config,
    world,
)

ELDEN_STEAM, ELDEN_SGDB = 1245620, 5297
HOLLOW_STEAM, HOLLOW_SGDB = 367520, 5241
HADES_STEAM, HADES_SGDB = 1145350, 5468

#: The display names the (fictional) Steam account owns them under.
ELDEN_OWNED = "ELDEN RING"
HOLLOW_OWNED = "Hollow Knight"
HADES_OWNED = "Hades II"

OFFICE = "OFFICE-PC"
AUTOCOMPLETE = "https://www.steamgriddb.com/api/v2/search/autocomplete/"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line]


def owned_file(tmp_path: Path, apps: dict[int, str], *, steamid3: int = STEAMID3) -> str:
    """Write an owned-apps file the way the plugin does (spec 3.4.1)."""
    path = tmp_path / "owned-apps.json"
    payload = {"version": 1, "steamid3": steamid3, "apps": {str(k): v for k, v in apps.items()}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def entry(world: World, name: str) -> Shortcut:
    return next(s for s in world.owned() if s.moonlight_name(TEMPLATE, SUFFIX) == name)


def maybe_entry(world: World, name: str) -> Shortcut | None:
    return next((s for s in world.owned() if s.moonlight_name(TEMPLATE, SUFFIX) == name), None)


def grid_of(world: World, appid: int) -> list[str]:
    return sorted(n for n in world.grid_files() if n.startswith(str(appid)))


def hidden_appid(world: World, owned_name: str) -> int:
    """The appid a ``stream`` entry named *owned_name* has (spec 3.3)."""
    return Shortcut.create(app_name=owned_name, exe=world.config.exe).appid


def cache_entry(world: World, name: str) -> dict | None:
    if not world.cache_path.is_file():
        return None
    return json.loads(world.cache_path.read_text())["titles"].get(name)


def seed_cache(world: World, *matches: Match) -> None:
    cache = MatchCache(world.cache_path)
    for match in matches:
        cache.put(match)


def run_status(world: World, argv: list[str], *, config=None) -> tuple[int, list[dict], str]:
    out, err = io.StringIO(), io.StringIO()
    code = cmd_status(
        build_parser().parse_args(argv),
        config or world.config,
        cache_path=world.cache_path,
        out=out,
        err=err,
    )
    return code, json_lines(out.getvalue()) if "--json" in argv else [], out.getvalue()


def run_match(world: World, argv: list[str], *, runner: FakeRunner | None = None) -> Result:
    transport = FakeTransport()
    runner = runner if runner is not None else FakeRunner(running=True)
    services = build_services(
        world.config, cache_path=world.cache_path, fetcher=make_fetcher(transport)
    )
    deps = sync.Deps(runner=runner, services=services, cache_path=world.cache_path)
    out, err = io.StringIO(), io.StringIO()
    code = cmd_match(build_parser().parse_args(argv), world.config, deps=deps, out=out, err=err)
    return Result(code, out.getvalue(), err.getvalue(), transport, runner)


def run_art(world: World, argv: list[str]) -> Result:
    transport = FakeTransport()
    runner = FakeRunner(running=True)
    services = build_services(
        world.config, cache_path=world.cache_path, fetcher=make_fetcher(transport)
    )
    out, err = io.StringIO(), io.StringIO()
    code = cmd_art(
        build_parser().parse_args(argv),
        world.config,
        provider_factory=lambda cfg: sync.steam_aware_provider(cfg, runner=runner),
        services=services,
        out=out,
        err=err,
    )
    return Result(code, out.getvalue(), err.getvalue(), transport, runner)


def snapshot(world: World) -> tuple:
    vdf = world.shortcuts_path.read_bytes() if world.shortcuts_path.exists() else None
    cache = world.cache_path.read_bytes() if world.cache_path.exists() else None
    return vdf, cache, world.grid_files(), world.backups()


def assert_stream_entry(shortcut: Shortcut, *, owned_name: str, moonlight_name: str) -> None:
    """The write policy for a ``stream`` title (spec 3.3)."""
    assert shortcut.app_name == owned_name
    assert shortcut.is_hidden == 1
    assert shortcut.exe == f'"{STREAM_SH}"'
    assert shortcut.launch_options == f'launch "{moonlight_name}"'
    assert shortcut.appid == Shortcut.create(app_name=owned_name, exe=STREAM_SH).appid
    assert shortcut.moonlight_name(TEMPLATE, SUFFIX) == moonlight_name


# ---------------------------------------------------------------------------
# kinds: stream vs shortcut (spec 3.2, 3.3)
# ---------------------------------------------------------------------------


def test_a_verified_match_to_an_owned_game_becomes_a_hidden_entry_named_after_it(
    world, tmp_path, monkeypatch
):
    """Spec 3.3: ``AppName`` = the owned display name (no suffix), ``IsHidden
    = 1``, launch options still carry the Moonlight name; the existing
    visible STL-era entry is replaced, not edited in place (spec 3.11)."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED, HADES_STEAM: HADES_OWNED})
    old_appid = entry(world, "Elden Ring").appid

    result = world.run(["sync", "--owned-apps", owned])

    assert result.code == 0, result.err
    assert "2 app(s) published, 0 ignored, 0 already in Steam, 1 to add, 1 to replace" in (
        result.out
    )
    assert (
        f"replace  Elden Ring: Elden Ring (streaming) [visible] -> {ELDEN_OWNED} [hidden] "
        "(kind-changed)"
    ) in result.out
    elden = entry(world, "Elden Ring")
    assert_stream_entry(elden, owned_name=ELDEN_OWNED, moonlight_name="Elden Ring")
    assert elden.appid != old_appid
    hades = entry(world, "Hades II™")
    assert_stream_entry(hades, owned_name=HADES_OWNED, moonlight_name="Hades II™")
    # Hidden entries still get all five slots, under the hidden appid.
    assert grid_of(world, elden.appid) == expected_grid_files(elden.appid)
    assert grid_of(world, hades.appid) == expected_grid_files(hades.appid)
    assert elden.icon == str((world.grid_dir / f"{elden.appid}_icon.jpg").resolve())
    assert grid_of(world, old_appid) == []
    # The unpublished, adopted Hollow Knight is untouched by all this.
    assert entry(world, "Hollow Knight").app_name == "Hollow Knight (streaming)"
    assert entry(world, "Hollow Knight").is_hidden == 0
    assert world.owned_names() == ["Elden Ring", "Hollow Knight", "Hades II™"]
    assert result.runner.shutdowns == 1 and result.runner.relaunches == 1
    assert "added 1 shortcut(s), adopted 1, ignored 0, already present 0, replaced 1" in (
        result.out
    )


def test_a_fuzzy_match_to_an_owned_game_stays_a_visible_shortcut(world, tmp_path, monkeypatch):
    """Spec 3.2 / decision 5: a fuzzy hit is fine for artwork but never a
    Stream button."""
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    seed_cache(
        world,
        Match(name="Hades II™", steam_appid=HADES_STEAM, sgdb_id=HADES_SGDB, how="sgdb:fuzzy"),
    )
    owned = owned_file(tmp_path, {HADES_STEAM: HADES_OWNED})

    result = world.run(["--json", "sync", "--owned-apps", owned])

    assert result.code == 0, result.err
    hades = entry(world, "Hades II™")
    assert hades.app_name == "Hades II™ (streaming)" and hades.is_hidden == 0
    events = json_lines(result.out)
    plan = next(e for e in events if e["event"] == "plan")
    assert plan["stream"] == 0 and plan["shortcut"] == 1
    title = next(e for e in events if e["event"] == "title" and e["name"] == "Hades II™")
    assert title["kind"] == "shortcut"
    # The fuzzy match is still used for the art (it resolves from the cache).
    assert f"{AUTOCOMPLETE}Hades%20II%E2%84%A2" not in result.calls
    assert any(f"/apps/{HADES_STEAM}/" in url for url in result.calls)


@pytest.mark.parametrize("how", ["override", "pinned", "sgdb:exact", "steamstore:exact"])
def test_every_exact_kind_of_match_earns_a_stream_entry(world, tmp_path, monkeypatch, how):
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    config = world.config
    if how == "override":
        config = make_config(tmp_path, overrides={"Hades II™": {"steam": HADES_STEAM}})
    else:
        seed_cache(world, Match(name="Hades II™", steam_appid=HADES_STEAM, how=how))
    owned = owned_file(tmp_path, {HADES_STEAM: HADES_OWNED})
    result = world.run(["sync", "--owned-apps", owned], config=config)
    assert result.code == 0, result.err
    assert_stream_entry(
        entry(world, "Hades II™"), owned_name=HADES_OWNED, moonlight_name="Hades II™"
    )


def test_without_owned_apps_nothing_is_resolved_ahead_of_the_plan_and_no_entry_is_hidden(
    world, tmp_path, monkeypatch
):
    """Spec 3.2/3.11: without the flag every title is ``shortcut`` and the
    run is v0.2.0's (the frozen e2e suite is the byte-level proof; this
    pins the plan-level facts). An owned-apps file on disk changes nothing
    unless it is passed."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™"])
    owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED, HADES_STEAM: HADES_OWNED})

    result = world.run(["--json", "sync"])

    assert result.code == 0, result.err
    assert all(s.is_hidden == 0 for s in world.owned())
    assert entry(world, "Hades II™").app_name == "Hades II™ (streaming)"
    assert entry(world, "Elden Ring").app_name == "Elden Ring (streaming)"
    events = json_lines(result.out)
    plan = next(e for e in events if e["event"] == "plan")
    assert plan["stream"] == 0 and plan["to_replace"] == 0 and plan["duplicates"] == {}
    assert not any(e["event"] == "replace" for e in events)
    assert all(e["kind"] == "shortcut" for e in events if e["event"] == "title")
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["added_by_kind"] == {"stream": 0, "shortcut": 1}
    assert summary["replaced"] == 0 and summary["removed"] == 0
    # No "resolve [i/N]" lines: resolution happened inside the art phase.
    assert "resolve [" not in result.err


def test_a_second_owned_run_is_a_no_op_with_zero_http_calls(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    assert world.run(["sync", "--owned-apps", owned]).code == 0
    data = world.shortcuts_path.read_bytes()
    files = world.grid_files()

    again = world.run(["sync", "--owned-apps", owned])

    assert again.code == 0, again.err
    assert again.calls == []
    assert world.shortcuts_path.read_bytes() == data
    assert world.grid_files() == files
    assert again.runner.shutdowns == 0 and again.runner.relaunches == 0
    assert "nothing to write" in again.out
    assert "2 already in Steam, 0 to add" in again.out and "to replace" not in again.out


def test_resolution_before_the_plan_is_cache_first_and_flushed_per_title(
    world, tmp_path, monkeypatch
):
    """Spec 3.4.2: with ``--owned-apps`` every published title is resolved
    before the plan; the art phase then finds each one cached. Under
    ``--no-art`` the lookups still happen (the plan needs them) but nothing
    is downloaded."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™", "Desktop"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})

    result = world.run(["sync", "--owned-apps", owned, "--no-art"])

    assert result.code == 0, result.err
    assert "resolve [1/2] Elden Ring: sgdb:exact-verified" in result.out
    assert "resolve [2/2] Hades II™: sgdb:exact-verified" in result.out
    assert "Desktop" not in result.out.split("MY-GAMING-PC:")[0]  # ignored: not resolved
    assert not any("/steam/apps/" in url for url in result.calls)  # lookups only
    assert sorted(world.cache_titles()) == ["Elden Ring", "Hades II™"]
    assert_stream_entry(
        entry(world, "Elden Ring"), owned_name=ELDEN_OWNED, moonlight_name="Elden Ring"
    )
    assert world.grid_files() == []

    # The art phase of the next run makes no lookups: everything is cached.
    dressed = world.run(["sync", "--owned-apps", owned])
    assert dressed.code == 0, dressed.err
    assert "resolve [" not in dressed.out
    # (Hollow Knight, adopted but unpublished, is resolved by the art phase
    # as always -- only published titles are resolved ahead of the plan.)
    assert f"{AUTOCOMPLETE}Elden%20Ring" not in dressed.calls
    assert f"{AUTOCOMPLETE}Hades%20II%E2%84%A2" not in dressed.calls
    assert len(grid_of(world, entry(world, "Elden Ring").appid)) == 5


# ---------------------------------------------------------------------------
# replacements: kind flips rename the grid files, rematches delete them
# ---------------------------------------------------------------------------


def test_removing_an_appid_from_the_owned_file_flips_the_entry_visible_by_renaming_its_art(
    world, tmp_path, monkeypatch
):
    """Spec 3.4.2: a kind flip is a replacement whose five grid files are
    renamed to the new appid before the art phase, so it downloads nothing."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    assert world.run(["sync", "--owned-apps", owned]).code == 0
    hidden = entry(world, "Elden Ring")
    assert hidden.is_hidden == 1
    hidden_files = grid_of(world, hidden.appid)
    assert len(hidden_files) == 5
    contents = {n: (world.grid_dir / n).read_bytes() for n in hidden_files}

    nobody = owned_file(tmp_path, {})
    result = world.run(["--json", "sync", "--owned-apps", nobody])

    assert result.code == 0, result.err
    assert result.calls == []
    visible = entry(world, "Elden Ring")
    assert visible.app_name == "Elden Ring (streaming)" and visible.is_hidden == 0
    assert visible.appid == world.appid_for("Elden Ring")
    assert grid_of(world, hidden.appid) == []
    assert grid_of(world, visible.appid) == expected_grid_files(visible.appid)
    for name, data in contents.items():
        renamed = world.grid_dir / name.replace(str(hidden.appid), str(visible.appid))
        assert renamed.read_bytes() == data
    assert visible.icon == str((world.grid_dir / f"{visible.appid}_icon.jpg").resolve())
    events = json_lines(result.out)
    replace = next(e for e in events if e["event"] == "replace")
    assert replace == {
        "event": "replace",
        "name": "Elden Ring",
        "old_appid": hidden.appid,
        "new_appid": visible.appid,
        "reason": "kind-changed",
    }
    assert next(e for e in events if e["event"] == "summary")["replaced"] == 1
    assert result.runner.shutdowns == 1


def test_adding_the_appid_back_flips_it_hidden_the_same_way(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert world.run(["sync"]).code == 0  # visible, dressed
    visible = entry(world, "Elden Ring")
    assert len(grid_of(world, visible.appid)) == 5
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})

    result = world.run(["sync", "--owned-apps", owned])

    assert result.code == 0, result.err
    assert result.calls == []  # renamed, not re-downloaded
    hidden = entry(world, "Elden Ring")
    assert_stream_entry(hidden, owned_name=ELDEN_OWNED, moonlight_name="Elden Ring")
    assert grid_of(world, visible.appid) == []
    assert grid_of(world, hidden.appid) == expected_grid_files(hidden.appid)
    # The replacement keeps the entry's slot and key in the file.
    assert [s.index_key for s in world.library()] == ["0", "1", "2"]
    assert world.library().shortcuts[0].app_name == ELDEN_OWNED


def test_a_changed_display_name_is_a_name_changed_replacement(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    same_name = owned_file(tmp_path, {ELDEN_STEAM: "Elden Ring"})
    assert world.run(["sync", "--owned-apps", same_name]).code == 0
    first = entry(world, "Elden Ring")
    renamed = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    result = world.run(["sync", "--owned-apps", renamed])
    assert result.code == 0, result.err
    assert result.calls == []
    assert (
        f"replace  Elden Ring: Elden Ring [hidden] -> {ELDEN_OWNED} [hidden] (name-changed)"
    ) in result.out
    second = entry(world, "Elden Ring")
    assert second.is_hidden == 1 and second.appid != first.appid
    assert grid_of(world, first.appid) == [] and len(grid_of(world, second.appid)) == 5


def test_a_stale_art_pin_makes_a_rematched_replacement_that_deletes_and_refetches(
    world, tmp_path, monkeypatch
):
    """Spec 3.4.4: ``match --defer-art`` leaves the art on disk flagged
    ``stale_art``; the next owned sync replaces the entry (``rematched``),
    deletes the old grid files instead of renaming them, fetches the new
    match's art, and clears the flag once that art phase has run."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    assert world.run(["sync", "--owned-apps", owned]).code == 0
    hidden = entry(world, "Elden Ring")
    pinned = run_match(world, ["match", "Elden Ring", "--steam", str(HOLLOW_STEAM), "--defer-art"])
    assert pinned.code == 0, pinned.err
    assert cache_entry(world, "Elden Ring")["stale_art"] is True
    assert len(grid_of(world, hidden.appid)) == 5

    result = world.run(["--json", "sync", "--owned-apps", owned])

    assert result.code == 0, result.err
    # The pin points at a game the account does not own, so the kind flips
    # too -- but the reason is the stale art, and the files are deleted.
    events = json_lines(result.out)
    replace = next(e for e in events if e["event"] == "replace")
    visible = entry(world, "Elden Ring")
    assert replace["reason"] == "rematched"
    assert replace["old_appid"] == hidden.appid and replace["new_appid"] == visible.appid
    assert visible.app_name == "Elden Ring (streaming)" and visible.is_hidden == 0
    assert grid_of(world, hidden.appid) == []
    assert grid_of(world, visible.appid) == expected_grid_files(visible.appid)
    assert not any(url.startswith(AUTOCOMPLETE) for url in result.calls)  # the pin, not a search
    assert any(f"/apps/{HOLLOW_STEAM}/" in url for url in result.calls)
    assert not any(f"/apps/{ELDEN_STEAM}/" in url for url in result.calls)
    assert "stale_art" not in cache_entry(world, "Elden Ring")
    assert cache_entry(world, "Elden Ring")["how"] == "pinned"


def test_a_rematched_replacement_keeps_the_appid_when_the_name_does_not_change(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Hollow Knight"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    assert world.run(["sync", "--owned-apps", owned]).code == 0
    hollow = entry(world, "Hollow Knight")
    assert run_match(
        world, ["match", "Hollow Knight", "--steam", str(HADES_STEAM), "--defer-art"]
    ).code == 0

    result = world.run(["sync", "--owned-apps", owned])

    assert result.code == 0, result.err
    assert (
        "replace  Hollow Knight: Hollow Knight (streaming) [visible] -> "
        "Hollow Knight (streaming) [visible] (rematched)"
    ) in result.out
    after = entry(world, "Hollow Knight")
    assert after.appid == hollow.appid
    assert grid_of(world, hollow.appid) == expected_grid_files(hollow.appid)
    # Every slot was fetched again from the new match, none was "kept".
    assert any(f"/apps/{HADES_STEAM}/" in url for url in result.calls)
    hollow_line = next(line for line in result.out.splitlines() if "] Hollow Knight:" in line)
    assert hollow_line.endswith(
        "portrait=official landscape=official hero=official logo=official icon=official"
    )
    assert "stale_art" not in cache_entry(world, "Hollow Knight")


def test_stale_art_is_not_cleared_when_the_art_phase_does_not_run(world, tmp_path, monkeypatch):
    """Spec 3.4.4: ``--no-art`` deletes the stale files (the flag asked for
    that) but fetches nothing, so the flag survives for the next run; an
    early stop never clears it either."""
    host_publishes(tmp_path, monkeypatch, ["Hollow Knight"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    assert world.run(["sync", "--owned-apps", owned]).code == 0
    hollow = entry(world, "Hollow Knight")
    assert run_match(
        world, ["match", "Hollow Knight", "--steam", str(HADES_STEAM), "--defer-art"]
    ).code == 0

    result = world.run(["sync", "--owned-apps", owned, "--no-art"])
    assert result.code == 0, result.err
    assert grid_of(world, hollow.appid) == []
    assert cache_entry(world, "Hollow Knight")["stale_art"] is True
    assert entry(world, "Hollow Knight").icon == ""

    dressed = world.run(["sync", "--owned-apps", owned])
    assert dressed.code == 0, dressed.err
    assert len(grid_of(world, hollow.appid)) == 5
    assert "stale_art" not in cache_entry(world, "Hollow Knight")


def test_art_force_clears_stale_art_too(world, tmp_path, monkeypatch):
    """Spec 3.4.4 (PR-2 consequences): ``art --force`` refilled every slot,
    so it clears the flag as well."""
    host_publishes(tmp_path, monkeypatch, ["Hollow Knight"])
    assert world.run(["sync"]).code == 0
    assert run_match(
        world, ["match", "Hollow Knight", "--steam", str(HADES_STEAM), "--defer-art"]
    ).code == 0
    assert cache_entry(world, "Hollow Knight")["stale_art"] is True
    result = run_art(world, ["art", "--force", "--only", "Hollow Knight"])
    assert result.code == 0, result.err
    assert "stale_art" not in cache_entry(world, "Hollow Knight")


def test_a_plain_sync_acts_on_a_deferred_pin_too(world, tmp_path, monkeypatch):
    """Spec 3.4.4 (consequences for PR-3): ``build_plan`` reads ``stale_art``
    whether or not ``--owned-apps`` resolved the titles ahead of it, so a
    plain ``sync`` after ``match --defer-art`` replaces the entry
    (``rematched``, same appid), deletes the old grid files, fetches the
    pinned match's art -- no search, the pin answers -- and clears the
    flag once that art phase has run."""
    host_publishes(tmp_path, monkeypatch, ["Hollow Knight"])
    assert world.run(["sync"]).code == 0
    hollow = entry(world, "Hollow Knight")
    assert grid_of(world, hollow.appid) == expected_grid_files(hollow.appid)
    assert run_match(
        world, ["match", "Hollow Knight", "--steam", str(HADES_STEAM), "--defer-art"]
    ).code == 0
    assert cache_entry(world, "Hollow Knight")["stale_art"] is True

    result = world.run(["--json", "sync"])

    assert result.code == 0, result.err
    events = json_lines(result.out)
    plan = next(e for e in events if e["event"] == "plan")
    assert plan["to_replace"] == 1 and plan["present"] == 0 and plan["to_add"] == 0
    replace = next(e for e in events if e["event"] == "replace")
    assert replace["name"] == "Hollow Knight" and replace["reason"] == "rematched"
    assert replace["old_appid"] == hollow.appid and replace["new_appid"] == hollow.appid
    after = entry(world, "Hollow Knight")
    assert after.appid == hollow.appid and after.is_hidden == 0
    assert after.app_name == "Hollow Knight (streaming)"
    assert grid_of(world, hollow.appid) == expected_grid_files(hollow.appid)
    assert not any(url.startswith(AUTOCOMPLETE) for url in result.calls)
    assert any(f"/apps/{HADES_STEAM}/" in url for url in result.calls)
    assert not any(f"/apps/{HOLLOW_STEAM}/" in url for url in result.calls)
    hollow_line = next(line for line in result.err.splitlines() if "] Hollow Knight:" in line)
    assert hollow_line.endswith(
        "portrait=official landscape=official hero=official logo=official icon=official"
    )
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["replaced"] == 1 and summary["added"] == 0
    cached = cache_entry(world, "Hollow Knight")
    assert cached["how"] == "pinned" and "stale_art" not in cached
    assert result.runner.shutdowns == 1
    # And the usual second run: every slot filled, zero calls, no restart.
    again = world.run(["sync"])
    assert again.code == 0, again.err
    assert again.calls == [] and again.runner.shutdowns == 0
    assert "stale_art" not in cache_entry(world, "Hollow Knight")


def test_a_plain_sync_acts_on_the_unpinned_placeholder(world, tmp_path, monkeypatch):
    """The ``how: "unpinned"`` placeholder ``--unpin --defer-art`` leaves is
    a cache miss for the resolver and, whatever its ``how``, a ``rematched``
    replacement for the plan (spec 3.4.4): a plain ``sync`` re-searches,
    deletes the pin's art, fetches the fresh match's and clears the flag."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert world.run(["sync"]).code == 0
    elden = entry(world, "Elden Ring")
    assert run_match(
        world, ["match", "Elden Ring", "--steam", str(HOLLOW_STEAM), "--defer-art"]
    ).code == 0
    assert run_match(world, ["match", "Elden Ring", "--unpin", "--defer-art"]).code == 0
    placeholder = cache_entry(world, "Elden Ring")
    assert placeholder["how"] == "unpinned" and placeholder["stale_art"] is True

    result = world.run(["sync"])

    assert result.code == 0, result.err
    assert (
        "replace  Elden Ring: Elden Ring (streaming) [visible] -> "
        "Elden Ring (streaming) [visible] (rematched)"
    ) in result.out
    assert f"{AUTOCOMPLETE}Elden%20Ring" in result.calls
    assert any(f"/apps/{ELDEN_STEAM}/" in url for url in result.calls)
    assert not any(f"/apps/{HOLLOW_STEAM}/" in url for url in result.calls)
    assert entry(world, "Elden Ring").appid == elden.appid
    assert grid_of(world, elden.appid) == expected_grid_files(elden.appid)
    cached = cache_entry(world, "Elden Ring")
    assert cached["how"] == "sgdb:exact-verified" and cached["steam_appid"] == ELDEN_STEAM
    assert "stale_art" not in cached


def test_a_rematched_replacement_that_changes_no_bytes_still_counts_as_replaced(
    world, tmp_path, monkeypatch
):
    """A ``rematched`` replacement keeps its appid, and when the new match's
    icon lands at the same path the entry serialises byte-for-byte as it
    was, so ``commit.written`` is false -- yet the pin's grid files were
    deleted, the new art fetched and Steam restarted for it. The summary
    counts what the commit *applied*: ``replaced 1`` on the final line and
    in ``summary.replaced``, and no "were not written" line (decky spec
    3.4.4). Unpinning a deferred pin back to the title's own match is the
    everyday way to get here."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert world.run(["sync"]).code == 0
    elden = entry(world, "Elden Ring")
    before = world.shortcuts_path.read_bytes()
    assert run_match(
        world, ["match", "Elden Ring", "--steam", str(HOLLOW_STEAM), "--defer-art"]
    ).code == 0
    assert run_match(world, ["match", "Elden Ring", "--unpin", "--defer-art"]).code == 0
    assert world.shortcuts_path.read_bytes() == before

    result = world.run(["--json", "sync"])

    assert result.code == 0, result.err
    events = json_lines(result.out)
    replace = next(e for e in events if e["event"] == "replace")
    assert replace["reason"] == "rematched"
    assert replace["old_appid"] == elden.appid and replace["new_appid"] == elden.appid
    commit = next(e for e in events if e["event"] == "commit")
    assert commit["written"] is False and commit["restarted"] is True
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["replaced"] == 1 and summary["added"] == 0
    assert world.shortcuts_path.read_bytes() == before
    assert entry(world, "Elden Ring") == elden
    assert grid_of(world, elden.appid) == expected_grid_files(elden.appid)
    assert any(f"/apps/{ELDEN_STEAM}/" in url for url in result.calls)
    assert not any(f"/apps/{HOLLOW_STEAM}/" in url for url in result.calls)
    assert result.runner.shutdowns == 1
    human = result.err.splitlines()
    final = next(line for line in human if line.startswith("added 0 shortcut(s)"))
    assert final.endswith(", replaced 1")
    assert not any("were not written" in line for line in human)
    assert "stale_art" not in cache_entry(world, "Elden Ring")


def test_a_plain_no_art_sync_deletes_stale_art_but_keeps_the_flag(
    world, tmp_path, monkeypatch
):
    """Same rule as under ``--owned-apps``: ``--no-art`` deletes the stale
    files (the flag asked for that) but fetches nothing, so the flag stays
    for the run that does; no lookups either way."""
    host_publishes(tmp_path, monkeypatch, ["Hollow Knight"])
    assert world.run(["sync"]).code == 0
    hollow = entry(world, "Hollow Knight")
    assert run_match(
        world, ["match", "Hollow Knight", "--steam", str(HADES_STEAM), "--defer-art"]
    ).code == 0

    result = world.run(["sync", "--no-art"])

    assert result.code == 0, result.err
    assert result.calls == []
    assert "1 to replace" in result.out
    assert grid_of(world, hollow.appid) == []
    assert entry(world, "Hollow Knight").icon == ""
    assert cache_entry(world, "Hollow Knight")["stale_art"] is True

    dressed = world.run(["sync"])
    assert dressed.code == 0, dressed.err
    assert grid_of(world, hollow.appid) == expected_grid_files(hollow.appid)
    assert "stale_art" not in cache_entry(world, "Hollow Knight")


def test_a_plain_sync_reads_the_cache_without_writing_it(world, tmp_path, monkeypatch):
    """Spec 3.11: the plan's cache read is a read. With no flagged entry a
    plain ``sync`` over a finished library leaves ``matches.json`` and
    ``shortcuts.vdf`` byte-identical and makes zero calls."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hollow Knight"])
    assert world.run(["sync"]).code == 0
    vdf, cache, grid, backups = snapshot(world)

    result = world.run(["sync"])

    assert result.code == 0, result.err
    assert result.calls == [] and result.runner.shutdowns == 0
    assert snapshot(world) == (vdf, cache, grid, backups)


# ---------------------------------------------------------------------------
# duplicates (spec 3.3, decision 14)
# ---------------------------------------------------------------------------


def test_two_titles_resolving_to_one_owned_game_yield_one_hidden_entry_and_a_duplicate(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Elden Ring (GOG)"])
    seed_cache(
        world,
        Match(
            name="Elden Ring (GOG)",
            steam_appid=ELDEN_STEAM,
            sgdb_id=ELDEN_SGDB,
            matched_name="Elden Ring",
            how="sgdb:exact-verified",
        ),
    )
    # A plain sync first, so the second title has a visible entry with art.
    assert world.run(["sync"]).code == 0
    gog = entry(world, "Elden Ring (GOG)")
    assert len(grid_of(world, gog.appid)) == 5
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})

    result = world.run(["--json", "sync", "--owned-apps", owned])

    assert result.code == 0, result.err
    assert maybe_entry(world, "Elden Ring (GOG)") is None
    assert grid_of(world, gog.appid) == []
    elden = entry(world, "Elden Ring")
    assert_stream_entry(elden, owned_name=ELDEN_OWNED, moonlight_name="Elden Ring")
    assert world.owned_names() == ["Elden Ring", "Hollow Knight"]
    assert "duplicate Elden Ring (GOG): same owned game as Elden Ring; no shortcut written" in (
        result.err
    )
    assert "1 to replace, 1 to remove" in result.err
    events = json_lines(result.out)
    plan = next(e for e in events if e["event"] == "plan")
    assert plan["duplicates"] == {"Elden Ring (GOG)": "Elden Ring"}
    assert plan["to_remove"] == 1 and plan["stream"] == 1 and plan["shortcut"] == 0
    assert plan["present"] == 0
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["duplicates"] == {"Elden Ring (GOG)": "Elden Ring"}
    assert summary["removed"] == 1 and summary["replaced"] == 1 and summary["added"] == 0
    assert summary["added_by_kind"] == {"stream": 0, "shortcut": 0}
    assert not any(e["event"] == "title" and e["name"] == "Elden Ring (GOG)" for e in events)

    # `list` flags it the same way.
    listed = world.run(["--json", "list", "--owned-apps", owned])
    apps = {e["name"]: e for e in json_lines(listed.out) if e["event"] == "app"}
    assert apps["Elden Ring"]["kind"] == "stream" and apps["Elden Ring"]["duplicate_of"] is None
    assert apps["Elden Ring (GOG)"]["kind"] == "duplicate"
    assert apps["Elden Ring (GOG)"]["duplicate_of"] == "Elden Ring"
    # No tile after the next sync, whatever entry it holds now: "ignored"
    # for the human list, with the winner named under it.
    assert apps["Elden Ring (GOG)"]["label"] == "ignored"
    human = world.run(["list", "--owned-apps", owned])
    assert human.code == 0, human.err
    lines = human.out.splitlines()
    assert "ignored  Elden Ring (GOG)" in lines
    at = lines.index("ignored  Elden Ring (GOG)")
    assert lines[at + 1] == "         duplicate-of: Elden Ring"


def test_pinning_the_duplicate_to_none_makes_it_a_visible_shortcut_again(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Elden Ring (GOG)"])
    seed_cache(
        world,
        Match(name="Elden Ring (GOG)", steam_appid=ELDEN_STEAM, how="sgdb:exact-verified"),
    )
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    assert world.run(["sync", "--owned-apps", owned]).code == 0
    assert maybe_entry(world, "Elden Ring (GOG)") is None
    assert run_match(world, ["match", "Elden Ring (GOG)", "--none"]).code == 0

    result = world.run(["sync", "--owned-apps", owned])

    assert result.code == 0, result.err
    gog = entry(world, "Elden Ring (GOG)")
    assert gog.app_name == "Elden Ring (GOG) (streaming)" and gog.is_hidden == 0
    assert "duplicate Elden Ring (GOG)" not in result.out
    assert_stream_entry(
        entry(world, "Elden Ring"), owned_name=ELDEN_OWNED, moonlight_name="Elden Ring"
    )


def test_the_duplicates_entry_that_is_the_winners_hidden_entry_is_left_alone(
    world, tmp_path, monkeypatch
):
    """The host order changed since the hidden entry was written: the
    title that used to win now loses, but the entry it wrote *is* the
    winner's entry (same appid), so it is kept for the winner rather than
    removed."""
    seed_cache(
        world,
        Match(name="Elden Ring (GOG)", steam_appid=ELDEN_STEAM, how="sgdb:exact-verified"),
    )
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    host_publishes(tmp_path, monkeypatch, ["Elden Ring (GOG)", "Elden Ring"])
    assert world.run(["sync", "--owned-apps", owned]).code == 0
    winner = entry(world, "Elden Ring (GOG)")
    assert winner.is_hidden == 1 and maybe_entry(world, "Elden Ring") is None

    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Elden Ring (GOG)"])
    result = world.run(["sync", "--owned-apps", owned])

    assert result.code == 0, result.err
    assert result.calls == []
    assert "to remove" not in result.out
    kept = next(s for s in world.owned() if s.appid == winner.appid)
    assert kept.is_hidden == 1 and kept.app_name == ELDEN_OWNED
    assert len(grid_of(world, winner.appid)) == 5


# ---------------------------------------------------------------------------
# the owned-apps file itself (spec 3.4.1)
# ---------------------------------------------------------------------------


def test_a_steamid3_mismatch_is_exit_1_with_nothing_touched(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    before = snapshot(world)
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED}, steamid3=STEAMID3 + 1)

    result = world.run(["sync", "--owned-apps", owned])

    assert result.code == 1
    assert (
        f"sync: owned-apps file is for Steam user {STEAMID3 + 1} but sync would write to "
        f"user {STEAMID3}"
    ) in result.err
    assert snapshot(world) == before
    assert not (tmp_path / "moonlight-argv.txt").exists()  # Moonlight was never asked
    assert result.calls == [] and result.runner.calls == []

    listed = world.run(["list", "--owned-apps", owned])
    assert listed.code == 1 and "owned-apps file is for Steam user" in listed.err


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (None, "could not be read"),
        ("{not json", "not valid JSON"),
        ('{"version": 2, "steamid3": 1, "apps": {}}', "unsupported version"),
        ('{"version": 1, "steamid3": 1, "apps": {"abc": "X"}}', "non-numeric appid"),
    ],
)
def test_a_bad_owned_apps_file_is_exit_1_before_anything_is_touched(
    world, tmp_path, monkeypatch, text, reason
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    path = tmp_path / "owned.json"
    if text is not None:
        path.write_text(text)
    before = snapshot(world)
    result = world.run(["--json", "sync", "--owned-apps", str(path)])
    assert result.code == 1
    assert reason in result.err
    events = json_lines(result.out)
    assert [e["event"] for e in events] == ["start", "error"]
    assert events[-1]["exit"] == 1 and reason in events[-1]["message"]
    assert snapshot(world) == before
    assert not (tmp_path / "moonlight-argv.txt").exists()


def test_a_launch_options_template_without_name_is_refused_with_owned_apps(
    world, tmp_path, monkeypatch
):
    """A hidden entry's ``AppName`` is the owned game's, so the launch
    options are the only way back to the Moonlight name; a template that
    cannot carry it would make every stream entry unrecoverable."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    config = make_config(tmp_path, launch_options="--quiet")
    before = snapshot(world)
    result = world.run(
        ["sync", "--owned-apps", owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})], config=config
    )
    assert result.code == 1
    assert "launch_options template with {name}" in result.err
    assert snapshot(world) == before
    # Without the flag the same template is fine, as in v0.2.0.
    assert world.run(["sync"], config=config).code == 0


# ---------------------------------------------------------------------------
# --limit counts additions and replacements together (decision 13)
# ---------------------------------------------------------------------------


def test_limit_one_with_a_replacement_and_an_addition_does_the_replacement_first(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})

    result = world.run(["sync", "--owned-apps", owned, "--limit", "1"])

    assert result.code == 0, result.err
    assert "1 to replace, 1 pending (--limit 1)" in result.out
    assert "1 still pending after --limit 1" in result.out
    assert_stream_entry(
        entry(world, "Elden Ring"), owned_name=ELDEN_OWNED, moonlight_name="Elden Ring"
    )
    assert maybe_entry(world, "Hades II™") is None
    # Hades was resolved ahead of the plan (its kind had to be known), but
    # nothing of its art was fetched.
    assert "Hades II™" in world.cache_titles()
    assert not any(f"/apps/{HADES_STEAM}/" in url for url in result.calls)

    # The other way round: the addition comes first in host order, so the
    # replacement waits and the old entry is adopted untouched this run.
    host_publishes(tmp_path, monkeypatch, ["Hades II™", "Hollow Knight"])
    hollow_before = entry(world, "Hollow Knight")
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED, HOLLOW_STEAM: HOLLOW_OWNED})
    result = world.run(["sync", "--owned-apps", owned, "--limit", "1", "--dry-run"])
    assert result.code == 0, result.err
    assert "  add      Hades II™" in result.out
    assert "  pending  Hollow Knight (beyond --limit 1)" in result.out
    assert "replace  Hollow Knight" not in result.out
    assert entry(world, "Hollow Knight") == hollow_before


def test_limit_never_splits_a_replacement(world, tmp_path, monkeypatch):
    """A replacement is one change: with ``--limit 0`` neither half happens
    and the old entry (and its art) stay exactly as they are."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert world.run(["sync"]).code == 0
    visible = entry(world, "Elden Ring")
    files = world.grid_files()
    data = world.shortcuts_path.read_bytes()
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})

    result = world.run(["sync", "--owned-apps", owned, "--limit", "0"])

    assert result.code == 0, result.err
    assert "1 pending (--limit 0)" in result.out
    assert world.shortcuts_path.read_bytes() == data
    assert world.grid_files() == files
    assert entry(world, "Elden Ring") == visible
    assert result.calls == []


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_dry_run_prints_replacements_and_touches_nothing(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™", "Desktop"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    vdf_before = world.shortcuts_path.read_bytes()

    result = world.run(
        ["--json", "sync", "--dry-run", "--owned-apps", owned, "--client-shortcut"]
    )

    assert result.code == 0, result.err
    lines = result.err.splitlines()
    assert (
        f"  replace  Elden Ring: Elden Ring (streaming) [visible] -> {ELDEN_OWNED} [hidden] "
        "(kind-changed)"
    ) in lines
    assert "  add      Hades II™ [" in result.err
    assert f"  add      {CLIENT_APP_NAME} [" in result.err and "(client shortcut)" in result.err
    assert "  ignored  Desktop" in lines
    assert "dry run: nothing written" in lines
    assert world.shortcuts_path.read_bytes() == vdf_before
    assert world.grid_files() == [] and world.backups() == []
    assert result.runner.shutdowns == 0
    events = json_lines(result.out)
    assert [e["event"] for e in events] == ["start", "plan", "replace", "summary"]
    assert events[2]["reason"] == "kind-changed" and events[2]["name"] == "Elden Ring"
    assert events[1]["to_replace"] == 1 and events[1]["stream"] == 1
    assert events[3]["replaced"] == 0 and events[3]["exit"] == 0
    # The one thing a dry run writes: the resolutions, for the real run.
    assert sorted(world.cache_titles()) == ["Elden Ring", "Hades II™", "Hollow Knight"]


# ---------------------------------------------------------------------------
# the client shortcut (spec 3.4.5)
# ---------------------------------------------------------------------------


def test_client_shortcut_adds_exactly_one_hidden_moonlight_entry_outside_the_art_phase(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])

    result = world.run(["--json", "sync", "--client-shortcut"])

    assert result.code == 0, result.err
    library = world.library()
    clients = [s for s in library if s.launch_options == CLIENT_LAUNCH_OPTIONS]
    assert len(clients) == 1
    client = clients[0]
    assert client.app_name == CLIENT_APP_NAME and client.is_hidden == 1
    assert client.exe == f'"{default_exe()}"'  # the tool itself, not config.exe
    assert client.is_client_entry(default_exe())
    assert library.client_entry(default_exe()) is not None
    # Never looked up, never dressed, never in a title event.
    assert not any("Moonlight" in url for url in result.calls)
    assert grid_of(world, client.appid) == []
    events = json_lines(result.out)
    assert not any(e["event"] == "title" and e["name"] == CLIENT_APP_NAME for e in events)
    assert "added the Moonlight client shortcut" in result.err
    # `config.exe` is a script here, so the entry is not "owned" by it...
    assert world.owned_names() == ["Elden Ring", "Hollow Knight"]

    # ...but the plan still lists it (kind client) and a second run adds
    # nothing, with or without the flag.
    again = world.run(["--json", "sync", "--client-shortcut"])
    assert again.code == 0 and again.calls == []
    assert len([s for s in world.library() if s.launch_options == CLIENT_LAUNCH_OPTIONS]) == 1
    plain = world.run(["sync"])
    assert plain.code == 0 and plain.calls == []
    assert len([s for s in world.library() if s.launch_options == CLIENT_LAUNCH_OPTIONS]) == 1
    assert "adopted 3" in plain.out  # Elden Ring, Hollow Knight, and the client entry

    # `status` reports it (client: true, no match) whoever `exe` is, and
    # `art` skips it.
    code, entries, _ = run_status(world, ["--json", "status"])
    assert code == 0
    client_entry = next(e for e in entries if e.get("event") == "entry" and e["client"])
    assert client_entry["name"] == CLIENT_APP_NAME and client_entry["appid"] == client.appid
    assert client_entry["hidden"] is True and client_entry["parked"] is False
    assert client_entry["match"] is None and client_entry["published"] is False
    art = run_art(world, ["art"])
    assert art.code == 0, art.err
    assert "Moonlight" not in art.out and not any("Moonlight" in url for url in art.calls)


def test_remove_client_removes_the_client_entry_and_nothing_else(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert world.run(["sync", "--client-shortcut"]).code == 0
    assert world.library().client_entry(default_exe()) is not None

    result = world.run(["remove", "--client"])

    assert result.code == 0, result.err
    assert world.library().client_entry(default_exe()) is None
    assert world.owned_names() == ["Elden Ring", "Hollow Knight"]
    assert "removed 1 shortcut(s) and 0 grid file(s)" in result.out
    assert result.runner.shutdowns == 1
    # Once it is gone, --client alone has nothing to do.
    again = world.run(["remove", "--client"])
    assert again.code == 0 and "nothing to remove" in again.out


def test_the_client_entry_survives_remove_all_when_exe_is_a_script(world, tmp_path, monkeypatch):
    """Spec 3.4.5: ownership is still by exe, so ``remove --all`` takes the
    client entry only when ``config.exe`` is the tool itself; ``remove
    --all --client`` (what the plugin runs) takes both."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert world.run(["sync", "--client-shortcut"]).code == 0

    result = world.run(["remove", "--all"])
    assert result.code == 0, result.err
    assert world.owned_names() == []
    assert world.library().client_entry(default_exe()) is not None
    assert [s.app_name for s in world.library()] == ["RetroArch", CLIENT_APP_NAME]

    # The name never reaches it either.
    by_name = world.run(["remove", CLIENT_APP_NAME])
    assert by_name.code == 1 and "no owned shortcut named 'Moonlight'" in by_name.err
    assert world.library().client_entry(default_exe()) is not None

    both = world.run(["remove", "--all", "--client"])
    assert both.code == 0, both.err
    assert [s.app_name for s in world.library()] == ["RetroArch"]

    # With `exe` = the tool, --all takes it (it is an owned entry then).
    tool_config = make_config(tmp_path, exe=default_exe())
    assert world.run(["sync", "--client-shortcut"], config=tool_config).code == 0
    assert world.library().client_entry(default_exe()) is not None
    assert world.run(["remove", "--all"], config=tool_config).code == 0
    assert world.library().client_entry(default_exe()) is None


def test_remove_needs_all_a_name_or_client():
    with pytest.raises(SystemExit) as raised:
        main(["remove"])
    assert raised.value.code == 2
    parser = build_parser()
    assert parser.parse_args(["remove", "--all", "--client"]).client is True
    assert parser.parse_args(["remove", "--client"]).all is False


def test_the_client_subcommand_execs_the_moonlight_gui_with_no_host(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(moonlight, "find_binary", lambda: ["/usr/bin/moonlight"])
    captured = {}
    monkeypatch.setattr(os, "execvp", lambda file, args: captured.update(file=file, args=args))
    from moonlight_steam_sync import config as config_module

    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")

    assert main(["client", "--", "--fullscreen"]) == 0
    assert captured["args"] == ["/usr/bin/moonlight", "--fullscreen"]
    assert main(["client"]) == 0
    assert captured["args"] == ["/usr/bin/moonlight"]

    assert main(["--json", "client"]) == 0
    events = json_lines(capsys.readouterr().out)
    assert [e["event"] for e in events][-2:] == ["start", "exec"]
    assert events[-1]["host"] is None  # no host configured, and none needed

    monkeypatch.setattr(moonlight, "find_binary", lambda: None)
    assert main(["--json", "client"]) == 3
    events = json_lines(capsys.readouterr().out)
    assert [e["event"] for e in events] == ["start", "error"]
    assert "moonlight CLI not found" in events[-1]["message"]


def test_doctor_reports_the_client_shortcut(world, isolated_config, tmp_path, monkeypatch, capsys):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert main(["doctor", "--client-shortcut"], deps=sync.Deps(runner=FakeRunner())) == 0
    assert "client shortcut: absent" in capsys.readouterr().out
    deps = sync.Deps(runner=FakeRunner())
    assert main(["sync", "--client-shortcut", "--no-art"], deps=deps) == 0
    client = world.library().client_entry(default_exe())
    assert client is not None
    assert main(["doctor", "--client-shortcut"], deps=sync.Deps(runner=FakeRunner())) == 0
    assert f"client shortcut: present [{client.appid}]" in capsys.readouterr().out
    assert main(["doctor"], deps=sync.Deps(runner=FakeRunner())) == 0
    assert "client shortcut:" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --park-unpublished: two hosts (spec 3.12)
# ---------------------------------------------------------------------------


@pytest.fixture
def two_hosts(world, tmp_path, monkeypatch):
    """A ``config.toml`` for the world plus the active-host state file, so
    switching hosts goes through ``host set`` (spec 3.12) and ``load_config``
    rather than a hand-built :class:`Config`."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'host = "{HOST}"\nname_suffix = "{SUFFIX}"\nexe = "{STREAM_SH}"\n'
        f"launch_options = 'launch \"{{name}}\"'\nignore = [\"Desktop\"]\n"
        "restart_steam = true\n[steamgriddb]\napi_key = \"fixture-key\"\n"
    )
    state_file = tmp_path / "state" / "active-host"

    class Switcher:
        def __init__(self) -> None:
            self.owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})

        def config(self, args=None):
            return load_config(args, config_path=config_path, state_file=state_file)

        def switch_to(self, name: str, publishes: list[str]) -> None:
            out, err = io.StringIO(), io.StringIO()
            code = hosts.cmd_host(
                build_parser().parse_args(["host", "set", name]),
                config_path=config_path,
                config_host=HOST,
                state_file=state_file,
                hosts_directory=world.cache_path.parent / "hosts",
                out=out,
                err=err,
            )
            assert code == 0, err.getvalue()
            install_fake_moonlight(tmp_path, monkeypatch, moonlight_list(publishes))

        def sync(self, *flags: str, park: bool = True) -> Result:
            argv = ["--json", "sync", "--owned-apps", self.owned, *flags]
            if park:
                argv.append("--park-unpublished")
            return world.run(argv, config=self.config())

        def status(self, host: str | None = None) -> dict[str, dict]:
            argv = ["--json", "status", "--owned-apps", self.owned]
            if host:
                argv += ["--host", host]
            args = build_parser().parse_args(argv)
            code, events, _ = run_status(world, argv, config=self.config(args))
            assert code == 0
            return {e["name"]: e for e in events if e["event"] == "entry"}

    return Switcher()


def only_is_hidden_differs(before: bytes, after: bytes) -> bool:
    """Whether two ``shortcuts.vdf`` payloads differ in ``IsHidden`` alone."""
    old = {s.appid: s for s in ShortcutsFile.from_bytes(before)}
    new = {s.appid: s for s in ShortcutsFile.from_bytes(after)}
    if old.keys() != new.keys():
        return False
    for appid, entry_before in old.items():
        entry_after = new[appid]
        for field_name in (
            "app_name", "exe", "start_dir", "icon", "launch_options", "allow_desktop_config",
            "allow_overlay", "last_play_time", "tags", "extra",
        ):
            if getattr(entry_before, field_name) != getattr(entry_after, field_name):
                return False
    return True


def test_switching_hosts_parks_and_switching_back_costs_one_write_and_no_calls(
    world, two_hosts, tmp_path, monkeypatch
):
    """Spec 3.12 / decision 17, end to end: host A publishes Elden Ring
    (owned -> stream) and Hollow Knight; host B publishes Elden Ring and
    Hades II. Sync A; switch to B; switch back to A."""
    switch = two_hosts
    switch.switch_to(HOST, ["Elden Ring", "Hollow Knight", "Desktop"])
    # The shared title is pinned before anything is synced: the pin (and
    # its art) must survive both switches.
    assert run_match(world, ["match", "Elden Ring", "--steam", str(ELDEN_STEAM)]).code == 0
    first = switch.sync()
    assert first.code == 0, first.err
    elden = entry(world, "Elden Ring")
    assert_stream_entry(elden, owned_name=ELDEN_OWNED, moonlight_name="Elden Ring")
    hollow = entry(world, "Hollow Knight")
    assert hollow.is_hidden == 0
    elden_files = grid_of(world, elden.appid)
    hollow_files = grid_of(world, hollow.appid)
    assert len(elden_files) == 5 and len(hollow_files) == 5
    assert switch.status()["Elden Ring"]["parked"] is False

    # --- switch to B -------------------------------------------------------
    switch.switch_to(OFFICE, ["Elden Ring", "Hades II™"])
    assert switch.config().host == OFFICE and switch.config().host_source == "state"
    to_b = switch.sync()
    assert to_b.code == 0, to_b.err
    events = json_lines(to_b.out)
    plan = next(e for e in events if e["event"] == "plan")
    assert plan["host"] == OFFICE
    assert plan["to_park"] == 1 and plan["to_unpark"] == 0 and plan["to_add"] == 1
    assert plan["to_replace"] == 0 and plan["parked"] == 1
    # A-only: hidden in place, art untouched, same appid.
    hollow_b = entry(world, "Hollow Knight")
    assert hollow_b.is_hidden == 1 and hollow_b.appid == hollow.appid
    assert hollow_b.app_name == "Hollow Knight (streaming)"
    assert grid_of(world, hollow.appid) == hollow_files
    # Shared: the same entry, same appid, still a stream entry, no new art.
    assert entry(world, "Elden Ring") == elden
    assert grid_of(world, elden.appid) == elden_files
    assert cache_entry(world, "Elden Ring")["how"] == "pinned"
    assert not any(f"/apps/{ELDEN_STEAM}/" in url for url in to_b.calls)
    # B-only: added.
    hades = entry(world, "Hades II™")
    assert hades.is_hidden == 0 and len(grid_of(world, hades.appid)) == 5
    assert world.owned_names() == ["Elden Ring", "Hollow Knight", "Hades II™"]
    assert to_b.runner.shutdowns == 1
    b_bytes = world.shortcuts_path.read_bytes()
    status_b = switch.status()
    assert status_b["Hollow Knight"]["parked"] is True
    assert status_b["Hollow Knight"]["published"] is False
    assert status_b["Elden Ring"]["parked"] is False and status_b["Elden Ring"]["published"]
    assert status_b["Hades II™"]["parked"] is False

    # --- and back to A -----------------------------------------------------
    switch.switch_to(HOST, ["Elden Ring", "Hollow Knight", "Desktop"])
    back = switch.sync()
    assert back.code == 0, back.err
    assert back.calls == []
    assert back.runner.shutdowns == 1 and back.runner.relaunches == 1
    events = json_lines(back.out)
    plan = next(e for e in events if e["event"] == "plan")
    assert plan["to_park"] == 1 and plan["to_unpark"] == 1 and plan["to_add"] == 0
    assert plan["to_replace"] == 0 and plan["parked"] == 1
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["added"] == 0 and summary["replaced"] == 0
    commit = next(e for e in events if e["event"] == "commit")
    assert commit["written"] is True
    assert "parked 1, unparked 1" in back.err
    a_again = world.shortcuts_path.read_bytes()
    assert only_is_hidden_differs(b_bytes, a_again)
    assert entry(world, "Hollow Knight").is_hidden == 0
    assert entry(world, "Hades II™").is_hidden == 1
    assert entry(world, "Elden Ring") == elden
    assert grid_of(world, elden.appid) == elden_files
    assert grid_of(world, hollow.appid) == hollow_files
    assert cache_entry(world, "Elden Ring")["how"] == "pinned"
    assert world.owned_names() == ["Elden Ring", "Hollow Knight", "Hades II™"]
    status_a = switch.status()
    assert status_a["Hades II™"]["parked"] is True
    assert status_a["Hades II™"]["published"] is False
    assert status_a["Hollow Knight"]["parked"] is False
    assert status_a["Hollow Knight"]["published"] is True
    # `list` for A labels it too.
    listed = world.run(["--json", "list", "--owned-apps", switch.owned], config=switch.config())
    apps = {e["name"]: e for e in json_lines(listed.out) if e["event"] == "app"}
    assert apps["Hollow Knight"]["label"] == "added" and apps["Hollow Knight"]["kind"] == "shortcut"
    assert apps["Elden Ring"]["kind"] == "stream"
    # And a third A sync is the usual no-op.
    third = switch.sync()
    assert third.code == 0 and third.calls == [] and third.runner.shutdowns == 0
    assert world.shortcuts_path.read_bytes() == a_again


def test_a_parked_stream_entry_stays_hidden_and_status_says_unpublished(
    world, two_hosts, tmp_path, monkeypatch
):
    switch = two_hosts
    switch.switch_to(HOST, ["Elden Ring"])
    assert switch.sync().code == 0
    elden = entry(world, "Elden Ring")
    assert elden.is_hidden == 1

    switch.switch_to(OFFICE, ["Hades II™"])
    result = switch.sync()
    assert result.code == 0, result.err
    plan = next(e for e in json_lines(result.out) if e["event"] == "plan")
    # Hollow Knight was parked by the first (A) sync already, and Elden
    # Ring is hidden by its kind: nothing flips, both count as parked.
    assert plan["to_park"] == 0 and plan["to_unpark"] == 0
    assert plan["parked"] == 2
    assert entry(world, "Elden Ring") == elden  # no flip, no replacement, no rename
    assert len(grid_of(world, elden.appid)) == 5
    status = switch.status()
    assert status["Elden Ring"]["hidden"] is True
    assert status["Elden Ring"]["published"] is False
    assert status["Elden Ring"]["parked"] is True
    assert status["Hollow Knight"]["parked"] is True
    assert status["Hades II™"]["parked"] is False
    # Against A's cache the same entry reads as an active stream entry.
    status_a = switch.status(HOST)
    assert status_a["Elden Ring"]["published"] is True
    assert status_a["Elden Ring"]["parked"] is False
    # With no cache for a host, nothing is claimed about a stream entry.
    status_none = switch.status("NEVER-SEEN")
    assert status_none["Elden Ring"]["parked"] is False
    assert status_none["Elden Ring"]["published"] is False
    assert status_none["Hollow Knight"]["parked"] is True


def test_without_park_unpublished_nothing_is_parked(world, two_hosts, tmp_path, monkeypatch):
    switch = two_hosts
    switch.switch_to(HOST, ["Elden Ring", "Hollow Knight"])
    assert switch.sync().code == 0
    switch.switch_to(OFFICE, ["Hades II™"])

    result = switch.sync(park=False)

    assert result.code == 0, result.err
    plan = next(e for e in json_lines(result.out) if e["event"] == "plan")
    assert plan["to_park"] == 0 and plan["to_unpark"] == 0
    assert entry(world, "Hollow Knight").is_hidden == 0
    assert "parked" not in result.err.split("added ")[-1]


def test_limit_zero_still_parks(world, two_hosts, tmp_path, monkeypatch):
    switch = two_hosts
    switch.switch_to(HOST, ["Elden Ring", "Hollow Knight"])
    assert switch.sync().code == 0
    switch.switch_to(OFFICE, ["Hades II™"])

    result = switch.sync("--limit", "0")

    assert result.code == 0, result.err
    plan = next(e for e in json_lines(result.out) if e["event"] == "plan")
    assert plan["to_park"] == 1 and plan["pending"] == 1 and plan["to_add"] == 0
    assert entry(world, "Hollow Knight").is_hidden == 1
    assert maybe_entry(world, "Hades II™") is None
    assert result.runner.shutdowns == 1
    assert "1 to park, 1 pending (--limit 0)" in result.err


def test_park_unpublished_alone_flips_is_hidden_without_owned_apps(world, tmp_path, monkeypatch):
    """The flag is independent of ``--owned-apps``: a single-host CLI user
    gets parking too, with every kind ``shortcut``."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    result = world.run(["sync", "--park-unpublished", "--no-art"])
    assert result.code == 0, result.err
    assert "1 to park" in result.out
    assert entry(world, "Hollow Knight").is_hidden == 1
    assert entry(world, "Elden Ring").is_hidden == 0

    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hollow Knight"])
    result = world.run(["sync", "--park-unpublished", "--no-art"])
    assert result.code == 0, result.err
    assert "1 to unpark" in result.out
    assert entry(world, "Hollow Knight").is_hidden == 0


def test_rehiding_an_unhidden_stream_entry_is_not_reported_as_parking(
    world, tmp_path, monkeypatch
):
    """Spec 3.11: ``IsHidden`` is edited in place, so a ``stream`` entry
    someone unhid is hidden again without a replacement -- but that is not
    parking (spec 3.12: a name the active host does *not* publish), so it
    is reported as ``to hide`` / ``hidden``, never in the ``plan`` event's
    park counts, and the ``plan`` event's keys stay the spec's."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    assert world.run(["sync", "--owned-apps", owned]).code == 0
    elden = entry(world, "Elden Ring")
    assert elden.is_hidden == 1
    library = world.library()
    next(s for s in library if s.appid == elden.appid).is_hidden = 0
    library.write()

    dry = world.run(["sync", "--owned-apps", owned, "--dry-run", "--no-art"])
    assert dry.code == 0, dry.err
    assert "1 to hide" in dry.out and "to park" not in dry.out
    assert f"  hide     Elden Ring [{elden.appid}] (stream entry, hidden by its kind)" in dry.out
    assert entry(world, "Elden Ring").is_hidden == 0

    result = world.run(["--json", "sync", "--owned-apps", owned])

    assert result.code == 0, result.err
    plan = next(e for e in json_lines(result.out) if e["event"] == "plan")
    assert plan["to_park"] == 0 and plan["to_unpark"] == 0 and plan["parked"] == 0
    assert plan["present"] == 1 and plan["to_replace"] == 0 and plan["to_add"] == 0
    assert "to_hide" not in plan
    assert "1 to hide" in result.err and ", hidden 1" in result.err
    assert "park" not in result.err
    after = entry(world, "Elden Ring")
    assert after.appid == elden.appid and after.is_hidden == 1
    assert result.calls == [] and result.runner.shutdowns == 1


def test_a_user_hidden_shortcut_is_unparked_only_with_the_flag(world, tmp_path, monkeypatch):
    """An owned entry hidden by hand reads as parked under ``--owned-apps``
    (spec 3.12) and is shown again only by ``--park-unpublished``."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    library = world.library()
    next(s for s in library if s.app_name == "Elden Ring (streaming)").is_hidden = 1
    library.write()
    owned = owned_file(tmp_path, {})

    listed = world.run(["--json", "list", "--owned-apps", owned])
    app = next(e for e in json_lines(listed.out) if e["event"] == "app")
    assert app["label"] == "parked" and app["kind"] == "parked"
    plain = world.run(["list"])
    assert plain.out.splitlines()[0] == "added    Elden Ring"  # v0.2.0's label (spec 3.11)

    kept = world.run(["sync", "--owned-apps", owned, "--no-art"])
    assert kept.code == 0 and entry(world, "Elden Ring").is_hidden == 1
    shown = world.run(["sync", "--owned-apps", owned, "--no-art", "--park-unpublished"])
    assert shown.code == 0 and entry(world, "Elden Ring").is_hidden == 0


# ---------------------------------------------------------------------------
# --json: the PR-3 fields end to end (spec 3.4.6)
# ---------------------------------------------------------------------------


def test_json_events_carry_the_kinds_replacements_and_per_kind_counts(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™", "Fan Made Adventure"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED, HADES_STEAM: HADES_OWNED})

    result = world.run(["--json", "sync", "--owned-apps", owned, "--client-shortcut"])

    assert result.code == 0, result.err
    events = json_lines(result.out)
    assert [e["event"] for e in events][:3] == ["start", "plan", "replace"]
    plan = events[1]
    assert plan == {
        "event": "plan",
        "host": HOST,
        "published": 3,
        "ignored": 0,
        "present": 0,
        "to_add": 2,
        "to_replace": 1,
        "to_park": 0,
        "to_unpark": 0,
        "to_remove": 0,
        "pending": 0,
        "limit": None,
        "stream": 2,
        "shortcut": 1,
        "parked": 0,
        "duplicates": {},
    }
    titles = {e["name"]: e for e in events if e["event"] == "title"}
    assert set(titles) == {"Elden Ring", "Hollow Knight", "Hades II™", "Fan Made Adventure"}
    assert titles["Elden Ring"]["kind"] == "stream"
    assert titles["Hades II™"]["kind"] == "stream"
    assert titles["Hollow Knight"]["kind"] == "shortcut"  # adopted, unpublished, not parked
    assert titles["Fan Made Adventure"]["kind"] == "shortcut"
    assert titles["Elden Ring"]["appid"] == hidden_appid(world, ELDEN_OWNED)
    summary = events[-1]
    assert summary["event"] == "summary"
    assert summary["added"] == 2 and summary["replaced"] == 1 and summary["removed"] == 0
    assert summary["added_by_kind"] == {"stream": 1, "shortcut": 1}
    assert summary["duplicates"] == {} and summary["exit"] == 0
    assert set(summary) == {
        "event", "added", "replaced", "removed", "filled", "missing", "unmatched",
        "duplicates", "pending", "stopped_early", "stop_reason", "exit", "added_by_kind",
    }


def test_added_by_kind_and_replaced_are_zero_when_the_write_is_refused(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    result = world.run(
        ["--json", "sync", "--owned-apps", owned],
        config=make_config(tmp_path, restart_steam=False),
    )
    assert result.code == 2
    events = json_lines(result.out)
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["added"] == 0 and summary["replaced"] == 0
    assert summary["added_by_kind"] == {"stream": 0, "shortcut": 0}
    assert events[-1]["event"] == "error" and events[-1]["exit"] == 2
    # The rename already happened (before the art phase); the old entry is
    # still in the file, and the next run plans the same replacement.
    assert entry(world, "Elden Ring").app_name == "Elden Ring (streaming)"
    again = world.run(["sync", "--owned-apps", owned], runner=FakeRunner(running=False))
    assert again.code == 0, again.err
    assert again.calls == []
    assert_stream_entry(
        entry(world, "Elden Ring"), owned_name=ELDEN_OWNED, moonlight_name="Elden Ring"
    )
    assert len(grid_of(world, entry(world, "Elden Ring").appid)) == 5


def test_a_hard_stop_during_resolution_is_exit_4_before_the_plan(
    fresh_world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Rate Limited Game"])
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED})
    result = fresh_world.run(["--json", "sync", "--owned-apps", owned])
    assert result.code == 4
    events = json_lines(result.out)
    assert [e["event"] for e in events] == ["start", "note", "error"] or [
        e["event"] for e in events
    ] == ["start", "error"]
    assert events[-1]["exit"] == 4 and "429" in events[-1]["message"]
    assert not fresh_world.shortcuts_path.exists()
    assert fresh_world.cache_titles() == ["Elden Ring"]  # flushed per title
    assert result.runner.shutdowns == 0


def test_list_with_owned_apps_reports_kinds_without_resolving(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™", "Desktop"])
    seed_cache(world, Match(name="Elden Ring", steam_appid=ELDEN_STEAM, how="sgdb:exact-verified"))
    owned = owned_file(tmp_path, {ELDEN_STEAM: ELDEN_OWNED, HADES_STEAM: HADES_OWNED})

    result = world.run(["--json", "list", "--owned-apps", owned])

    assert result.code == 0, result.err
    assert result.calls == []  # never resolves
    apps = {e["name"]: e for e in json_lines(result.out) if e["event"] == "app"}
    assert apps["Elden Ring"]["kind"] == "stream" and apps["Elden Ring"]["label"] == "added"
    assert apps["Hades II™"]["kind"] == "shortcut" and apps["Hades II™"]["label"] == "new"
    assert apps["Desktop"]["kind"] == "ignored"
    assert all(a["duplicate_of"] is None for a in apps.values())
    assert "Hades II™" not in world.cache_titles()
