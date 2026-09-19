"""``match``, pinned cache entries and ``--ignore-file`` (decky spec 3.4.3,
3.4.4, 3.4.8; PR-2).

Builds on the ``world`` / ``fresh_world`` fixtures and helpers from
``tests/test_sync_e2e.py`` (spec 4 preamble: that file is frozen, new tests
import from it) and the recorded artwork manifest behind
:class:`tests.art_fixtures.FakeTransport`. Fixture ids used below, all from
``tests/fixtures/art/``: Elden Ring is SteamGridDB 5297 / Steam 1245620,
Hollow Knight 5241 / 367520, Hades II 5468 / 1145350.
"""

from __future__ import annotations

import io
import json
import tomllib
from pathlib import Path

import pytest

from moonlight_steam_sync import config as config_module
from moonlight_steam_sync import sync
from moonlight_steam_sync.__main__ import build_parser, main
from moonlight_steam_sync.art.cli import build_services, cmd_art, cmd_match, cmd_status
from moonlight_steam_sync.art.resolve import CACHE_VERSION, Match, MatchCache, Resolver
from moonlight_steam_sync.art.sgdb import SgdbClient
from moonlight_steam_sync.art.steamstore import SteamStoreClient
from moonlight_steam_sync.config import ConfigError, load_config, load_ignore_file
from moonlight_steam_sync.shortcuts import ShortcutsFile
from tests.art_fixtures import FakeTransport, make_fetcher
from tests.fakes import FakeRunner
from tests.test_sync_e2e import (  # noqa: F401 - pytest fixtures
    SUFFIX,
    TEMPLATE,
    Result,
    World,
    fresh_world,
    host_publishes,
    isolated_config,
    make_config,
    world,
)

ELDEN_STEAM, ELDEN_SGDB = 1245620, 5297
HOLLOW_STEAM, HOLLOW_SGDB = 367520, 5241
HADES_STEAM, HADES_SGDB = 1145350, 5468

AUTOCOMPLETE = "https://www.steamgriddb.com/api/v2/search/autocomplete/"


def json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line]


def cache_entry(world: World, name: str) -> dict | None:
    if not world.cache_path.is_file():
        return None
    return json.loads(world.cache_path.read_text())["titles"].get(name)


def run_match(
    world: World,
    argv: list[str],
    *,
    transport: FakeTransport | None = None,
    runner: FakeRunner | None = None,
    config=None,
) -> Result:
    transport = transport if transport is not None else FakeTransport()
    runner = runner if runner is not None else FakeRunner(running=True)
    config = config or world.config
    services = build_services(config, cache_path=world.cache_path, fetcher=make_fetcher(transport))
    deps = sync.Deps(runner=runner, services=services, cache_path=world.cache_path)
    out, err = io.StringIO(), io.StringIO()
    code = cmd_match(build_parser().parse_args(argv), config, deps=deps, out=out, err=err)
    return Result(code, out.getvalue(), err.getvalue(), transport, runner)


def run_art(
    world: World,
    argv: list[str],
    *,
    transport: FakeTransport | None = None,
    runner: FakeRunner | None = None,
    config=None,
) -> Result:
    transport = transport if transport is not None else FakeTransport()
    runner = runner if runner is not None else FakeRunner(running=True)
    config = config or world.config
    services = build_services(config, cache_path=world.cache_path, fetcher=make_fetcher(transport))
    out, err = io.StringIO(), io.StringIO()
    code = cmd_art(
        build_parser().parse_args(argv),
        config,
        provider_factory=lambda cfg: sync.steam_aware_provider(cfg, runner=runner),
        services=services,
        out=out,
        err=err,
    )
    return Result(code, out.getvalue(), err.getvalue(), transport, runner)


def synced(world: World, tmp_path: Path, monkeypatch, names: list[str]) -> None:
    """The world after one ordinary ``sync`` of ``names``."""
    host_publishes(tmp_path, monkeypatch, names)
    result = world.run(["sync"])
    assert result.code == 0, result.err


def shortcut(world: World, name: str):
    return next(s for s in world.owned() if s.moonlight_name(TEMPLATE, SUFFIX) == name)


def grid_files_of(world: World, appid: int) -> list[str]:
    return [n for n in world.grid_files() if n.startswith(str(appid))]


def snapshot(world: World) -> tuple:
    vdf = world.shortcuts_path.read_bytes() if world.shortcuts_path.exists() else None
    cache = world.cache_path.read_bytes() if world.cache_path.exists() else None
    return vdf, cache, world.grid_files(), world.backups()


# ---------------------------------------------------------------------------
# the cache: Match.stale_art and MatchCache.pin/unpin/mark/clear
# ---------------------------------------------------------------------------


def test_an_entry_without_the_new_fields_serialises_exactly_as_before(tmp_path):
    """CACHE_VERSION stays 1 and an unpinned, non-stale entry has no new key
    (decky spec 3.4.4, 3.11: matches.json bytes unchanged without pins)."""
    assert CACHE_VERSION == 1
    old = {
        "steam_appid": ELDEN_STEAM,
        "sgdb_id": ELDEN_SGDB,
        "matched_name": "Elden Ring",
        "how": "sgdb:exact-verified",
        "when": "2026-09-01T00:00:00Z",
        "missing_slots": {"logo": "2026-09-01T00:00:00Z"},
    }
    match = Match.from_json("Elden Ring", old)
    assert match.stale_art is False
    assert match.to_json() == old


def test_pin_replaces_the_entry_and_is_on_disk_at_once(tmp_path):
    path = tmp_path / "matches.json"
    cache = MatchCache(path)
    cache.put(Match(name="Hades II", steam_appid=1, how="sgdb:fuzzy", missing_slots={"logo": "x"}))
    cache.mark_stale_art("Hades II")

    cache.pin(Match(name="Hades II", steam_appid=HADES_STEAM, sgdb_id=HADES_SGDB,
                    matched_name="Hades II", how="whatever"))

    on_disk = json.loads(path.read_text())
    assert on_disk["version"] == 1
    entry = on_disk["titles"]["Hades II"]
    assert entry["how"] == "pinned"
    assert (entry["steam_appid"], entry["sgdb_id"]) == (HADES_STEAM, HADES_SGDB)
    assert entry["missing_slots"] == {}  # the old match's misses are gone
    assert "stale_art" not in entry  # so is the old flag
    assert MatchCache(path).get("Hades II").pinned


def test_unpin_deletes_the_entry_and_is_a_no_op_without_one(tmp_path):
    path = tmp_path / "matches.json"
    cache = MatchCache(path)
    cache.pin(Match(name="Hades II", steam_appid=HADES_STEAM))
    assert cache.unpin("Hades II") is True
    assert json.loads(path.read_text())["titles"] == {}
    assert cache.unpin("Hades II") is False
    assert MatchCache(path).get("Hades II") is None


def test_stale_art_is_persisted_only_while_set(tmp_path):
    path = tmp_path / "matches.json"
    cache = MatchCache(path)
    assert cache.mark_stale_art("Nobody") is False  # no entry, nothing invented
    assert not path.exists()

    cache.pin(Match(name="Hades II", steam_appid=HADES_STEAM))
    assert cache.mark_stale_art("Hades II") is True
    assert json.loads(path.read_text())["titles"]["Hades II"]["stale_art"] is True
    assert MatchCache(path).get("Hades II").stale_art is True

    cache.clear_stale_art("Hades II")
    assert "stale_art" not in json.loads(path.read_text())["titles"]["Hades II"]
    assert MatchCache(path).get("Hades II").stale_art is False


def test_put_carries_stale_art_forward_but_pin_starts_clean(tmp_path):
    """``stale_art`` describes the grid files, not the match, so a
    re-resolution written over a flagged entry keeps the flag; only a pin
    (the user's fresh choice) or ``clear_stale_art`` drops it."""
    path = tmp_path / "matches.json"
    cache = MatchCache(path)
    cache.put(Match(name="Hades II", steam_appid=1, how="sgdb:fuzzy"))
    cache.mark_stale_art("Hades II")

    cache.put(Match(name="Hades II", steam_appid=HADES_STEAM, how="sgdb:exact-verified"))
    entry = json.loads(path.read_text())["titles"]["Hades II"]
    assert entry["how"] == "sgdb:exact-verified" and entry["stale_art"] is True
    assert MatchCache(path).get("Hades II").stale_art is True

    cache.pin(Match(name="Hades II", steam_appid=HADES_STEAM))
    assert "stale_art" not in json.loads(path.read_text())["titles"]["Hades II"]

    cache.mark_stale_art("Hades II")
    cache.put(Match(name="Hades II", steam_appid=HADES_STEAM, how="override"), keep_stale_art=False)
    assert "stale_art" not in json.loads(path.read_text())["titles"]["Hades II"]


def test_unpin_with_stale_art_leaves_a_placeholder_that_only_carries_the_flag(tmp_path):
    path = tmp_path / "matches.json"
    cache = MatchCache(path)
    cache.pin(Match(name="Hades II", steam_appid=HADES_STEAM, sgdb_id=HADES_SGDB,
                    matched_name="Hades II"))

    assert cache.unpin("Hades II", stale_art=True) is True

    entry = json.loads(path.read_text())["titles"]["Hades II"]
    assert entry["how"] == "unpinned" and entry["stale_art"] is True
    assert (entry["steam_appid"], entry["sgdb_id"], entry["matched_name"]) == (None, None, None)
    reloaded = MatchCache(path).get("Hades II")
    assert reloaded.unpinned and reloaded.stale_art and not reloaded.found and not reloaded.pinned
    # A second unpin (with or without the flag) is a no-op on the placeholder
    # apart from the return value, which now says "there was an entry".
    assert cache.unpin("Hades II", stale_art=True) is True
    assert json.loads(path.read_text())["titles"]["Hades II"]["how"] == "unpinned"
    # The placeholder is invented even for a title with no entry, because
    # the flag is about the files on disk, not about a prior entry.
    assert cache.unpin("Nobody", stale_art=True) is False
    assert json.loads(path.read_text())["titles"]["Nobody"]["stale_art"] is True


# ---------------------------------------------------------------------------
# the resolver: a pin comes before force and retry_missing; config beats it
# ---------------------------------------------------------------------------


def resolver_for(tmp_path, transport, **kwargs) -> Resolver:
    fetcher = make_fetcher(transport)
    return Resolver(
        cache=MatchCache(tmp_path / "matches.json"),
        sgdb=SgdbClient(api_key="fixture-key", fetcher=fetcher),
        store=SteamStoreClient(fetcher=fetcher),
        **kwargs,
    )


@pytest.mark.parametrize("flags", [{}, {"force": True}, {"retry_missing": True},
                                   {"force": True, "retry_missing": True}])
def test_a_pin_is_returned_without_a_lookup_even_under_force_and_retry_missing(tmp_path, flags):
    transport = FakeTransport()
    resolver = resolver_for(tmp_path, transport, **flags)
    resolver.cache.pin(Match(name="Elden Ring", steam_appid=HOLLOW_STEAM))

    match = resolver.resolve("Elden Ring")

    assert transport.calls == []
    assert match.how == "pinned" and match.steam_appid == HOLLOW_STEAM
    assert match.explain and match.explain[0].startswith("pinned:")


def test_a_none_pin_is_never_re_searched_even_with_retry_missing(tmp_path):
    transport = FakeTransport()
    resolver = resolver_for(tmp_path, transport, retry_missing=True)
    resolver.cache.pin(Match(name="Elden Ring"))
    match = resolver.resolve("Elden Ring")
    assert transport.calls == []
    assert match.how == "pinned" and not match.found


def test_an_unpinned_placeholder_is_a_cache_miss_whose_flag_survives_the_search(tmp_path):
    """The placeholder ``match --unpin --defer-art`` leaves is re-searched
    like a missing entry (no 7-day negative window, whatever its ``when``),
    and the resolution written over it keeps ``stale_art`` for ``sync``."""
    transport = FakeTransport()
    resolver = resolver_for(tmp_path, transport)
    resolver.cache.pin(Match(name="Elden Ring", steam_appid=HOLLOW_STEAM))
    resolver.cache.unpin("Elden Ring", stale_art=True)

    match = resolver.resolve("Elden Ring")

    assert f"{AUTOCOMPLETE}Elden%20Ring" in transport.calls
    assert match.how == "sgdb:exact-verified" and match.steam_appid == ELDEN_STEAM
    assert match.stale_art is True
    on_disk = json.loads((tmp_path / "matches.json").read_text())["titles"]["Elden Ring"]
    assert on_disk["how"] == "sgdb:exact-verified" and on_disk["stale_art"] is True
    # ...and a second resolve is the cache hit it always was, flag intact.
    calls = len(transport.calls)
    again = resolver.resolve("Elden Ring")
    assert len(transport.calls) == calls and again.how == "sgdb:exact-verified"
    assert again.stale_art is True


def test_a_placeholder_survives_a_transient_lookup_failure(tmp_path, monkeypatch):
    """A failed search writes nothing (spec 6.10), so the placeholder -- and
    its flag -- are still there for the next run."""
    transport = FakeTransport()
    resolver = resolver_for(tmp_path, transport)
    resolver.cache.unpin("Elden Ring", stale_art=True)
    monkeypatch.setattr(resolver.sgdb, "search", lambda name: (_ for _ in ()).throw(
        __import__("moonlight_steam_sync.art.http", fromlist=["HttpError"]).HttpError("boom")
    ))
    monkeypatch.setattr(resolver.store, "search", lambda name: (_ for _ in ()).throw(
        __import__("moonlight_steam_sync.art.http", fromlist=["HttpError"]).HttpError("boom")
    ))

    match = resolver.resolve("Elden Ring")

    assert match.transient and not match.found
    entry = json.loads((tmp_path / "matches.json").read_text())["titles"]["Elden Ring"]
    assert entry["how"] == "unpinned" and entry["stale_art"] is True


def test_an_override_in_config_beats_a_pin(tmp_path):
    transport = FakeTransport()
    resolver = resolver_for(
        tmp_path, transport, overrides={"Elden Ring": {"steam": HOLLOW_STEAM}}
    )
    resolver.cache.pin(Match(name="Elden Ring", steam_appid=HADES_STEAM))
    match = resolver.resolve("Elden Ring")
    assert match.how == "override"
    assert match.steam_appid == HOLLOW_STEAM


def test_an_override_beats_a_pin_end_to_end(world, tmp_path, monkeypatch):
    """``sync`` with both a pin and an ``[overrides]`` entry fetches the
    override's art, not the pin's (config > cache)."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert run_match(world, ["match", "Elden Ring", "--steam", str(HADES_STEAM),
                             "--defer-art"]).code == 0
    config = make_config(tmp_path, overrides={"Elden Ring": {"steam": HOLLOW_STEAM}})
    result = world.run(["sync"], config=config)
    assert result.code == 0, result.err
    assert any(f"/apps/{HOLLOW_STEAM}/" in url for url in result.calls)
    assert not any(f"/apps/{HADES_STEAM}/" in url for url in result.calls)


# ---------------------------------------------------------------------------
# PR-2 acceptance: `art --force` takes a pinned title's lookups from the pin
# ---------------------------------------------------------------------------


def test_art_force_takes_a_pinned_titles_lookups_from_the_pin_not_the_search(
    world, tmp_path, monkeypatch
):
    synced(world, tmp_path, monkeypatch, ["Elden Ring", "Hollow Knight"])
    pinned = run_match(
        world, ["match", "Elden Ring", "--sgdb", str(HOLLOW_SGDB), "--defer-art"]
    )
    assert pinned.code == 0, pinned.err

    result = run_art(world, ["art", "--force"])

    assert result.code == 0, result.err
    # The pinned title: no search at all, every official slot from the pin.
    assert f"{AUTOCOMPLETE}Elden%20Ring" not in result.calls
    assert not any(f"/apps/{ELDEN_STEAM}/" in url for url in result.calls)
    assert any(f"/apps/{HOLLOW_STEAM}/library_600x900_2x.jpg" in url for url in result.calls)
    # --force still re-resolves every *unpinned* title from scratch.
    assert f"{AUTOCOMPLETE}Hollow%20Knight" in result.calls
    # ...and the pin survived it.
    entry = cache_entry(world, "Elden Ring")
    assert entry["how"] == "pinned"
    assert (entry["steam_appid"], entry["sgdb_id"]) == (HOLLOW_STEAM, HOLLOW_SGDB)


def test_a_pin_survives_sync_retry_missing(world, tmp_path, monkeypatch):
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    assert run_match(world, ["match", "Elden Ring", "--none"]).code == 0

    first = world.run(["sync", "--retry-missing"])
    second = world.run(["sync", "--retry-missing"])

    for result in (first, second):
        assert result.code == 0, result.err
        assert not any("Elden" in url for url in result.calls)
    entry = cache_entry(world, "Elden Ring")
    assert entry["how"] == "pinned"
    assert entry["steam_appid"] is None and entry["sgdb_id"] is None


# ---------------------------------------------------------------------------
# match: the pins it writes
# ---------------------------------------------------------------------------


def test_match_steam_refinds_the_sgdb_id_whose_steam_release_is_that_appid(
    fresh_world, tmp_path, monkeypatch
):
    result = run_match(
        fresh_world, ["match", "Hades II™", "--steam", str(HADES_STEAM), "--force-name"]
    )
    assert result.code == 0, result.err
    assert result.out.splitlines() == [
        f'pinned "Hades II™": steam {HADES_STEAM}, sgdb {HADES_SGDB}',
        f'"Hades II™" = {{ steam = {HADES_STEAM} }}',
    ]
    assert result.calls == [
        f"{AUTOCOMPLETE}Hades%20II%E2%84%A2",
        f"https://www.steamgriddb.com/api/v2/games/id/{HADES_SGDB}?platformdata=steam",
    ]
    entry = cache_entry(fresh_world, "Hades II™")
    assert entry == {
        "steam_appid": HADES_STEAM,
        "sgdb_id": HADES_SGDB,
        "matched_name": "Hades II",
        "how": "pinned",
        "when": entry["when"],
        "missing_slots": {},
    }


def test_match_steam_with_no_sgdb_result_for_that_appid_pins_sgdb_none(fresh_world):
    result = run_match(
        fresh_world, ["match", "Elden Ring", "--steam", str(HOLLOW_STEAM), "--force-name"]
    )
    assert result.code == 0, result.err
    assert result.out.splitlines()[0] == f'pinned "Elden Ring": steam {HOLLOW_STEAM}, sgdb none'
    entry = cache_entry(fresh_world, "Elden Ring")
    assert (entry["steam_appid"], entry["sgdb_id"]) == (HOLLOW_STEAM, None)


def test_match_steam_without_a_key_makes_no_calls(fresh_world, tmp_path):
    config = make_config(tmp_path, sgdb_api_key="")
    result = run_match(
        fresh_world,
        ["match", "Hades II™", "--steam", str(HADES_STEAM), "--force-name"],
        config=config,
    )
    assert result.code == 0, result.err
    assert result.calls == []
    entry = cache_entry(fresh_world, "Hades II™")
    assert (entry["steam_appid"], entry["sgdb_id"], entry["matched_name"]) == (
        HADES_STEAM,
        None,
        "Hades II™",
    )


def test_match_sgdb_fills_the_steam_appid_from_games_id(fresh_world):
    result = run_match(
        fresh_world, ["match", "Hollow Knight", "--sgdb", str(HOLLOW_SGDB), "--force-name"]
    )
    assert result.code == 0, result.err
    assert result.out.splitlines() == [
        f'pinned "Hollow Knight": steam {HOLLOW_STEAM}, sgdb {HOLLOW_SGDB}',
        f'"Hollow Knight" = {{ sgdb = {HOLLOW_SGDB} }}',
    ]
    entry = cache_entry(fresh_world, "Hollow Knight")
    assert (entry["steam_appid"], entry["sgdb_id"]) == (HOLLOW_STEAM, HOLLOW_SGDB)


def test_match_sgdb_for_a_game_with_no_steam_release_pins_steam_none(fresh_world):
    result = run_match(
        fresh_world, ["match", "Fan Made Adventure", "--sgdb", "7001", "--force-name"]
    )
    assert result.code == 0, result.err
    assert result.out.splitlines()[0] == 'pinned "Fan Made Adventure": steam none, sgdb 7001'


def test_match_none_pins_no_match_and_prints_the_empty_override(fresh_world):
    result = run_match(fresh_world, ["match", "Elden Ring", "--none", "--force-name"])
    assert result.code == 0, result.err
    assert result.calls == []
    assert result.out.splitlines() == ['pinned "Elden Ring": no match', '"Elden Ring" = {}']
    # The printed line really is the config form of "no match".
    assert tomllib.loads(f"[overrides]\n{result.out.splitlines()[1]}\n")["overrides"] == {
        "Elden Ring": {}
    }
    entry = cache_entry(fresh_world, "Elden Ring")
    assert entry["how"] == "pinned" and entry["steam_appid"] is None and entry["sgdb_id"] is None


def test_the_override_line_is_valid_toml_for_an_awkward_name(fresh_world):
    name = 'Odd "Name" \\ Ü'
    result = run_match(fresh_world, ["match", name, "--steam", "42", "--force-name"],
                       config=make_config(fresh_world.tmp_path, sgdb_api_key=""))
    assert result.code == 0, result.err
    line = result.out.splitlines()[1]
    assert tomllib.loads(f"[overrides]\n{line}\n")["overrides"] == {name: {"steam": 42}}


def test_a_none_pin_makes_no_lookups_at_all_on_the_next_sync(fresh_world, tmp_path, monkeypatch):
    """Spec PR-2: ``--none`` yields no art lookups -- not even the
    SteamGridDB search by name the title would otherwise get."""
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    assert run_match(fresh_world, ["match", "Hades II™", "--none", "--force-name"]).code == 0

    result = fresh_world.run(["sync"])

    assert result.code == 0, result.err
    assert result.calls == []
    assert fresh_world.owned_names() == ["Hades II™"]  # the tile is still added
    assert fresh_world.grid_files() == []


def test_unpin_deletes_the_entry_and_the_next_sync_re_resolves(world, tmp_path, monkeypatch):
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    elden = shortcut(world, "Elden Ring")
    assert run_match(world, ["match", "Elden Ring", "--steam", str(HOLLOW_STEAM)]).code == 0
    pinned_sync = world.run(["sync"])
    assert any(f"/apps/{HOLLOW_STEAM}/" in url for url in pinned_sync.calls)

    result = run_match(world, ["match", "Elden Ring", "--unpin"])
    assert result.code == 0, result.err
    lines = result.out.splitlines()
    assert lines[0] == "shutting down Steam (Ctrl-C is deferred until it is back up)"
    assert lines[1:] == [
        'unpinned "Elden Ring"; the next run re-resolves it',
        f"wrote {world.shortcuts_path}; backup {world.backups()[-1].name}; Steam restarted",
        "deleted 5 grid file(s)",
    ]
    assert cache_entry(world, "Elden Ring") is None
    assert grid_files_of(world, elden.appid) == []  # the pin's art went with it

    again = world.run(["sync"])
    assert again.code == 0, again.err
    assert f"{AUTOCOMPLETE}Elden%20Ring" in again.calls
    assert any(f"/apps/{ELDEN_STEAM}/" in url for url in again.calls)
    entry = cache_entry(world, "Elden Ring")
    assert entry["how"] == "sgdb:exact-verified" and entry["steam_appid"] == ELDEN_STEAM
    assert len(grid_files_of(world, elden.appid)) == 5


def test_unpin_of_a_title_with_no_entry_is_fine(world):
    result = run_match(world, ["match", "Hollow Knight", "--unpin"])
    assert result.code == 0, result.err
    assert "unpinned" in result.out


def test_unpin_defer_art_keeps_the_art_stale_across_the_re_resolution(
    world, tmp_path, monkeypatch
):
    """``match --unpin --defer-art`` is what the plugin runs for unpin
    (decky spec 3.7). The entry is gone as a match, but the grid files on
    disk still belong to the pin, so a placeholder carries ``stale_art``
    and the next ``sync``'s re-resolution inherits it (spec 3.4.4)."""
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    elden = shortcut(world, "Elden Ring")
    assert run_match(
        world, ["match", "Elden Ring", "--steam", str(HOLLOW_STEAM), "--defer-art"]
    ).code == 0
    vdf, _cache, grid, backups = snapshot(world)
    runner = FakeRunner(running=True)

    result = run_match(world, ["match", "Elden Ring", "--unpin", "--defer-art"], runner=runner)

    assert result.code == 0, result.err
    assert result.out.splitlines() == [
        'unpinned "Elden Ring"; the next run re-resolves it',
        'art for "Elden Ring" will be re-fetched on the next sync',
    ]
    assert runner.calls == [] and runner.spawned == []
    assert world.shortcuts_path.read_bytes() == vdf
    assert world.grid_files() == grid and world.backups() == backups
    entry = cache_entry(world, "Elden Ring")
    assert entry["how"] == "unpinned" and entry["stale_art"] is True
    assert entry["steam_appid"] is None and entry["sgdb_id"] is None

    # The placeholder is "no match" on the wire, with the flag set.
    out, err = io.StringIO(), io.StringIO()
    code = cmd_status(
        build_parser().parse_args(["--json", "status"]),
        world.config, cache_path=world.cache_path, out=out, err=err,
    )
    assert code == 0, err.getvalue()
    status_entry = next(e for e in json_lines(out.getvalue()) if e.get("name") == "Elden Ring")
    assert status_entry["match"] is None and status_entry["stale_art"] is True
    listed = world.run(["--json", "list"])
    app = next(e for e in json_lines(listed.out) if e["event"] == "app")
    assert app["match"] is None and app["fuzzy"] is False

    # The next sync re-resolves (a real search, not the pin) and the fresh
    # entry still carries the flag; PR-2 leaves acting on it to the
    # owned-apps PR, so the art on disk is kept and nothing is written.
    again = world.run(["sync"])
    assert again.code == 0, again.err
    assert f"{AUTOCOMPLETE}Elden%20Ring" in again.calls
    assert not any(f"/apps/{HOLLOW_STEAM}/" in url for url in again.calls)
    entry = cache_entry(world, "Elden Ring")
    assert entry["how"] == "sgdb:exact-verified" and entry["steam_appid"] == ELDEN_STEAM
    assert entry["stale_art"] is True
    assert world.shortcuts_path.read_bytes() == vdf
    assert len(grid_files_of(world, elden.appid)) == 5
    # A second sync is the usual cache hit: zero calls, flag still there.
    third = world.run(["sync"])
    assert third.code == 0 and third.calls == []
    assert cache_entry(world, "Elden Ring")["stale_art"] is True


def test_unpin_defer_art_without_a_shortcut_deletes_the_entry_outright(
    fresh_world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert run_match(
        fresh_world, ["match", "Elden Ring", "--none", "--force-name"]
    ).code == 0
    result = run_match(
        fresh_world, ["match", "Elden Ring", "--unpin", "--defer-art", "--force-name"]
    )
    assert result.code == 0, result.err
    assert result.out.splitlines() == ['unpinned "Elden Ring"; the next run re-resolves it']
    assert cache_entry(fresh_world, "Elden Ring") is None


# ---------------------------------------------------------------------------
# match: what happens to the art of an owned shortcut
# ---------------------------------------------------------------------------


def test_the_immediate_path_deletes_the_grid_files_and_clears_icon_with_one_restart(
    world, tmp_path, monkeypatch
):
    synced(world, tmp_path, monkeypatch, ["Elden Ring", "Hollow Knight"])
    elden = shortcut(world, "Elden Ring")
    hollow_before = shortcut(world, "Hollow Knight")
    assert elden.icon and len(grid_files_of(world, elden.appid)) == 5
    backups = len(world.backups())

    result = run_match(world, ["match", "Elden Ring", "--steam", str(HOLLOW_STEAM)])

    assert result.code == 0, result.err
    lines = result.out.splitlines()
    assert lines[-4:] == [
        f'pinned "Elden Ring": steam {HOLLOW_STEAM}, sgdb none',
        f'"Elden Ring" = {{ steam = {HOLLOW_STEAM} }}',
        f"wrote {world.shortcuts_path}; backup {world.backups()[-1].name}; Steam restarted",
        "deleted 5 grid file(s)",
    ]
    assert result.runner.shutdowns == 1 and result.runner.relaunches == 1
    assert len(world.backups()) == backups + 1
    assert shortcut(world, "Elden Ring").icon == ""
    assert grid_files_of(world, elden.appid) == []
    # Nothing else moved: the other entry, its art, and the entry's appid.
    assert shortcut(world, "Elden Ring").appid == elden.appid
    assert shortcut(world, "Hollow Knight").icon == hollow_before.icon
    assert len(grid_files_of(world, hollow_before.appid)) == 5
    assert "stale_art" not in cache_entry(world, "Elden Ring")

    # The next sync fetches the new match's art into the emptied slots.
    again = world.run(["sync"])
    assert again.code == 0, again.err
    assert f"{AUTOCOMPLETE}Elden%20Ring" not in again.calls
    assert any(f"/apps/{HOLLOW_STEAM}/" in url for url in again.calls)
    assert len(grid_files_of(world, elden.appid)) == 5
    assert shortcut(world, "Elden Ring").icon.endswith(f"{elden.appid}_icon.jpg")


def test_the_immediate_path_refuses_under_a_live_steam_and_touches_nothing(
    world, tmp_path, monkeypatch
):
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    before = snapshot(world)
    config = make_config(tmp_path, restart_steam=False)

    result = run_match(
        world, ["match", "Elden Ring", "--steam", str(HOLLOW_STEAM)], config=config
    )

    assert result.code == 2
    assert result.err.startswith("match: Steam is running and restart_steam is false")
    assert snapshot(world) == before  # vdf, matches.json, grid/ and backups
    assert result.runner.shutdowns == 0 and result.runner.relaunches == 0
    assert result.out == ""


def test_the_immediate_path_with_steam_down_writes_without_starting_it(
    world, tmp_path, monkeypatch
):
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    config = make_config(tmp_path, restart_steam=False)
    result = run_match(
        world,
        ["match", "Elden Ring", "--none"],
        config=config,
        runner=FakeRunner(running=False),
    )
    assert result.code == 0, result.err
    assert result.runner.spawned == []
    assert shortcut(world, "Elden Ring").icon == ""


def test_defer_art_writes_only_the_cache(world, tmp_path, monkeypatch):
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    vdf, _cache, grid, backups = snapshot(world)
    runner = FakeRunner(running=True)

    result = run_match(
        world, ["match", "Elden Ring", "--steam", str(HOLLOW_STEAM), "--defer-art"],
        runner=runner,
    )

    assert result.code == 0, result.err
    assert result.out.splitlines() == [
        f'pinned "Elden Ring": steam {HOLLOW_STEAM}, sgdb none',
        f'"Elden Ring" = {{ steam = {HOLLOW_STEAM} }}',
        'art for "Elden Ring" will be re-fetched on the next sync',
    ]
    assert runner.calls == [] and runner.spawned == []  # Steam not even asked
    assert world.shortcuts_path.read_bytes() == vdf
    assert world.grid_files() == grid and world.backups() == backups
    entry = cache_entry(world, "Elden Ring")
    assert entry["how"] == "pinned" and entry["stale_art"] is True


def test_defer_art_on_a_title_without_a_shortcut_marks_nothing_stale(fresh_world):
    result = run_match(
        fresh_world, ["match", "Elden Ring", "--none", "--defer-art", "--force-name"]
    )
    assert result.code == 0, result.err
    assert "re-fetched" not in result.out
    assert "stale_art" not in cache_entry(fresh_world, "Elden Ring")


def test_a_title_without_a_shortcut_never_touches_steam(fresh_world):
    runner = FakeRunner(running=True)
    result = run_match(
        fresh_world, ["match", "Elden Ring", "--none", "--force-name"], runner=runner
    )
    assert result.code == 0, result.err
    assert runner.calls == []
    assert not fresh_world.shortcuts_path.exists()


# ---------------------------------------------------------------------------
# match: which names it accepts
# ---------------------------------------------------------------------------


def test_an_unknown_name_is_exit_1_and_touches_nothing(world):
    before = snapshot(world)
    result = run_match(world, ["match", "Nope", "--steam", str(HADES_STEAM)])
    assert result.code == 1
    assert "'Nope'" in result.err and "--force-name" in result.err
    assert result.calls == []
    assert snapshot(world) == before
    assert not world.cache_path.exists()


def test_a_name_the_host_published_is_accepted_without_a_shortcut(
    fresh_world, tmp_path, monkeypatch
):
    """The host's cached app list (written by ``list``/``sync``) is what
    "published" means here; ``match`` itself never runs ``moonlight``."""
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    assert fresh_world.run(["list"]).code == 0
    (tmp_path / "moonlight-argv.txt").unlink()

    result = run_match(fresh_world, ["match", "Hades II™", "--steam", str(HADES_STEAM)])

    assert result.code == 0, result.err
    assert not (tmp_path / "moonlight-argv.txt").exists()  # moonlight not run
    assert cache_entry(fresh_world, "Hades II™")["how"] == "pinned"


def test_a_name_only_another_hosts_cache_has_is_unknown(fresh_world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    other = make_config(tmp_path, host="OFFICE-PC")
    assert fresh_world.run(["list"], config=other).code == 0
    result = run_match(fresh_world, ["match", "Hades II™", "--none"])
    assert result.code == 1
    assert "no cached app list for host MY-GAMING-PC" in result.err


def test_force_name_pins_a_title_nobody_publishes_yet(world):
    result = run_match(world, ["match", "Future Game", "--none", "--force-name"])
    assert result.code == 0, result.err
    assert cache_entry(world, "Future Game")["how"] == "pinned"


def test_a_non_positive_id_is_exit_1(world):
    result = run_match(world, ["match", "Elden Ring", "--steam", "0"])
    assert result.code == 1 and "--steam" in result.err
    assert not world.cache_path.exists()


def test_exactly_one_of_the_four_modes_is_required():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["match", "X"])
    with pytest.raises(SystemExit):
        parser.parse_args(["match", "X", "--none", "--unpin"])


# ---------------------------------------------------------------------------
# match --json: the `pinned` event (spec 3.4.6)
# ---------------------------------------------------------------------------


def test_match_json_emits_start_then_pinned_then_the_art_note(world, tmp_path, monkeypatch):
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    result = run_match(
        world,
        ["--json", "match", "Elden Ring", "--sgdb", str(HOLLOW_SGDB), "--defer-art"],
    )
    assert result.code == 0, result.err
    events = json_lines(result.out)
    assert [e["event"] for e in events] == ["start", "pinned", "note"]
    assert events[0]["command"] == "match"
    assert events[1] == {
        "event": "pinned",
        "name": "Elden Ring",
        "steam_appid": HOLLOW_STEAM,
        "sgdb_id": HOLLOW_SGDB,
        "override_line": f'"Elden Ring" = {{ sgdb = {HOLLOW_SGDB} }}',
    }
    assert events[2]["message"] == 'art for "Elden Ring" will be re-fetched on the next sync'
    assert f'pinned "Elden Ring": steam {HOLLOW_STEAM}, sgdb {HOLLOW_SGDB}' in result.err


def test_match_json_none_and_unpin(world):
    none = json_lines(run_match(world, ["--json", "match", "Elden Ring", "--none",
                                        "--defer-art"]).out)
    assert none[1] == {
        "event": "pinned",
        "name": "Elden Ring",
        "steam_appid": None,
        "sgdb_id": None,
        "override_line": '"Elden Ring" = {}',
    }
    unpinned = run_match(world, ["--json", "match", "Elden Ring", "--unpin", "--defer-art"])
    events = json_lines(unpinned.out)
    # Elden Ring has a shortcut in `world`, so the deferred unpin also notes
    # that its art is stale (the same note a deferred pin emits).
    assert [e["event"] for e in events] == ["start", "pinned", "note"]
    assert events[1] == {
        "event": "pinned",
        "name": "Elden Ring",
        "steam_appid": None,
        "sgdb_id": None,
        "override_line": None,
    }
    assert events[2]["message"] == 'art for "Elden Ring" will be re-fetched on the next sync'


def test_match_json_errors_are_the_last_line(world, tmp_path, monkeypatch):
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    unknown = json_lines(run_match(world, ["--json", "match", "Nope", "--none"]).out)
    assert [e["event"] for e in unknown] == ["start", "error"]
    assert unknown[-1]["exit"] == 1 and unknown[-1]["message"].startswith("match: ")

    refused = run_match(
        world,
        ["--json", "match", "Elden Ring", "--none"],
        config=make_config(tmp_path, restart_steam=False),
    )
    # Elden Ring's icon points at its grid file, so there is a write to refuse.
    events = json_lines(refused.out)
    assert refused.code == 2
    assert [e["event"] for e in events] == ["start", "error"]
    assert events[-1]["exit"] == 2


def test_main_dispatches_match(world, isolated_config, capsys):
    code = main(
        ["--json", "match", "Hollow Knight", "--none", "--defer-art"],
        deps=sync.Deps(runner=FakeRunner()),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert [e["event"] for e in json_lines(out)] == ["start", "pinned", "note"]


# ---------------------------------------------------------------------------
# status: entry.stale_art is real now
# ---------------------------------------------------------------------------


def test_status_json_reports_stale_art_and_the_pin(world, tmp_path, monkeypatch):
    synced(world, tmp_path, monkeypatch, ["Elden Ring", "Hollow Knight"])
    assert run_match(
        world, ["match", "Elden Ring", "--steam", str(HOLLOW_STEAM), "--defer-art"]
    ).code == 0

    out, err = io.StringIO(), io.StringIO()
    args = build_parser().parse_args(["--json", "status"])
    code = cmd_status(args, world.config, cache_path=world.cache_path, out=out, err=err)

    assert code == 0, err.getvalue()
    entries = {e["name"]: e for e in json_lines(out.getvalue()) if e["event"] == "entry"}
    assert entries["Elden Ring"]["stale_art"] is True
    assert entries["Elden Ring"]["match"]["how"] == "pinned"
    assert entries["Hollow Knight"]["stale_art"] is False


# ---------------------------------------------------------------------------
# --ignore-file (spec 3.4.3)
# ---------------------------------------------------------------------------


def ignore_file(tmp_path: Path, names) -> Path:
    path = tmp_path / "ignore.json"
    path.write_text(json.dumps(names))
    return path


def test_list_ignores_the_union_of_config_and_the_ignore_file(world, tmp_path, monkeypatch):
    host_publishes(
        tmp_path, monkeypatch, ["Elden Ring", "Desktop", "Hades II™", "Fan Made Adventure"]
    )
    path = ignore_file(tmp_path, ["Hades II™"])

    result = world.run(["list", "--ignore-file", str(path)])

    assert result.code == 0, result.err
    assert result.out.splitlines()[:4] == [
        "added    Elden Ring",
        "ignored  Desktop",
        "ignored  Hades II™",
        "new      Fan Made Adventure",
    ]
    assert "4 app(s) published, 2 ignored, 1 already in Steam, 1 to add" in result.out

    as_json = world.run(["--json", "list", "--ignore-file", str(path)])
    apps = {e["name"]: e for e in json_lines(as_json.out) if e["event"] == "app"}
    assert apps["Hades II™"]["label"] == "ignored" and apps["Hades II™"]["kind"] == "ignored"
    assert apps["Desktop"]["kind"] == "ignored"


def test_sync_never_adds_a_name_from_the_ignore_file(fresh_world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Hades II™", "Desktop", "Fan Made Adventure"])
    path = ignore_file(tmp_path, ["Fan Made Adventure"])
    result = fresh_world.run(["sync", "--ignore-file", str(path)])
    assert result.code == 0, result.err
    assert "3 app(s) published, 2 ignored, 0 already in Steam, 1 to add" in result.out
    assert fresh_world.owned_names() == ["Hades II™"]
    assert not any("Fan" in url for url in result.calls)


def test_ignore_all_does_not_offer_names_the_ignore_file_already_ignores(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™", "Fan Made Adventure"])
    path = ignore_file(tmp_path, ["Hades II™"])
    result = world.run(["ignore", "--all", "--ignore-file", str(path)])
    assert result.code == 0, result.err
    # config.toml's list plus the genuinely new names -- the block is for
    # config.toml, and the file's names are ignored already.
    assert tomllib.loads(result.out)["ignore"] == [
        "Desktop",
        "Steam Big Picture",
        "Fan Made Adventure",
    ]


def test_an_empty_ignore_file_changes_nothing(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Desktop", "Hades II™"])
    plain = world.run(["list"])
    with_file = world.run(["list", "--ignore-file", str(ignore_file(tmp_path, []))])
    assert with_file.out == plain.out and with_file.code == plain.code == 0


@pytest.mark.parametrize(
    "content, reason",
    [
        (None, "could not be read"),
        ("{not json", "is not valid JSON"),
        ('{"names": ["Desktop"]}', "is not a JSON list of names"),
        ('["Desktop", 3]', "has a non-string entry"),
    ],
)
@pytest.mark.parametrize("command", [["sync"], ["list"], ["ignore", "--all"], ["ignore", "X"]])
def test_a_bad_ignore_file_is_exit_1_before_anything_is_touched(
    world, tmp_path, monkeypatch, content, reason, command
):
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    path = tmp_path / "ignore.json"
    if content is not None:
        path.write_text(content)
    # A corrupt library would be the error if it were opened first.
    world.shortcuts_path.write_bytes(b"\x00garbage")
    before = snapshot(world)

    result = world.run([*command, "--ignore-file", str(path)])

    assert result.code == 1
    assert result.err.startswith(f"{command[0]}: ignore file {path} {reason}")
    assert result.out == ""
    assert result.calls == []
    assert not (tmp_path / "moonlight-argv.txt").exists()  # Moonlight never asked
    assert snapshot(world) == before
    assert not (world.cache_path.parent / "hosts").exists()


def test_a_bad_ignore_file_under_json_is_start_then_error(world, tmp_path):
    result = world.run(["--json", "sync", "--ignore-file", str(tmp_path / "missing.json")])
    events = json_lines(result.out)
    assert [e["event"] for e in events] == ["start", "error"]
    assert events[-1]["exit"] == 1
    assert events[-1]["message"].startswith("sync: ignore file ")


def test_load_ignore_file_and_the_config_field(tmp_path):
    path = ignore_file(tmp_path, ["Desktop", "Steam Big Picture"])
    assert load_ignore_file(path) == ["Desktop", "Steam Big Picture"]
    with pytest.raises(ConfigError):
        load_ignore_file(tmp_path / "nope.json")
    args = build_parser().parse_args(["sync", "--ignore-file", str(path)])
    cfg = load_config(args, config_path=tmp_path / "config.toml", env={},
                      key_file=tmp_path / "key")
    assert cfg.ignore_file_path == path
    assert cfg.ignore == []  # the file is never merged into config.ignore itself


def test_doctor_reports_the_ignore_file_and_never_fails_over_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")
    monkeypatch.delenv("SGDB_API_KEY", raising=False)
    good = ignore_file(tmp_path, ["Desktop", "Steam Big Picture", "Playnite"])
    assert main(["doctor", "--ignore-file", str(good)]) == 0
    assert f"ignore file: {good} (3 names)" in capsys.readouterr().out

    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert main(["doctor", "--ignore-file", str(bad)]) == 0
    assert f"ignore file: {bad} (invalid: ignore file {bad} is not a JSON list of names)" in (
        capsys.readouterr().out
    )

    assert main(["doctor"]) == 0
    assert "ignore file:" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# nothing changes without the new flags (spec 3.11)
# ---------------------------------------------------------------------------


def test_matches_json_written_by_a_plain_sync_has_no_new_keys(world, tmp_path, monkeypatch):
    synced(world, tmp_path, monkeypatch, ["Elden Ring", "Totally Unknown Title"])
    titles = json.loads(world.cache_path.read_text())["titles"]
    assert titles
    for entry in titles.values():
        assert set(entry) == {"steam_appid", "sgdb_id", "matched_name", "how", "when",
                              "missing_slots"}
        assert entry["how"] != "pinned"


def test_shortcuts_file_is_unchanged_by_a_plain_sync_after_a_deferred_pin(
    world, tmp_path, monkeypatch
):
    """PR-2 records ``stale_art`` but does not act on it (that is PR-3's
    ``rematched`` replacement): a second sync after a deferred pin keeps
    the art that is on disk and writes nothing."""
    synced(world, tmp_path, monkeypatch, ["Elden Ring"])
    assert run_match(world, ["match", "Elden Ring", "--none", "--defer-art"]).code == 0
    before = world.shortcuts_path.read_bytes()
    result = world.run(["sync"])
    assert result.code == 0, result.err
    assert result.calls == []
    assert world.shortcuts_path.read_bytes() == before
    assert ShortcutsFile.read(world.shortcuts_path).owned(world.config.exe)
