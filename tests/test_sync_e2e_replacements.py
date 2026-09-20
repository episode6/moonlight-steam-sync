"""Resumability of replacements (decky spec 3.4.2, spec 3.9; PR-3).

A replacement renames the old entry's grid files to the new appid *before*
the art phase and *before* the one ``shortcuts.vdf`` write, so a run that
dies between two replacements -- or halfway through one -- leaves some
files under the new appid and some under the old, with the vdf untouched.
The rerun must plan the same replacements and treat a rename whose source
is gone and whose target exists as done (never overwrite, never lose a
file), then finish with zero HTTP calls.

Lives in its own file rather than ``tests/test_sync_e2e.py``, which is
frozen (spec 4 preamble); the ``world`` fixture, the fake Steam tree and
the crash machinery are imported from there and from
``tests/art_fixtures``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from moonlight_steam_sync.shortcuts import Shortcut
from tests.art_fixtures import FakeTransport, SimulatedCrash
from tests.fakes import STEAMID3, STREAM_SH
from tests.test_sync_e2e import (  # noqa: F401 - pytest fixtures
    SUFFIX,
    TEMPLATE,
    World,
    expected_grid_files,
    host_publishes,
    world,
)

ELDEN_STEAM, HOLLOW_STEAM = 1245620, 367520
OWNED = {ELDEN_STEAM: "ELDEN RING", HOLLOW_STEAM: "Hollow Knight"}
STEAM_OF = {"Elden Ring": ELDEN_STEAM, "Hollow Knight": HOLLOW_STEAM}
NAMES = list(STEAM_OF)


def owned_file(tmp_path: Path) -> str:
    path = tmp_path / "owned-apps.json"
    payload = {"version": 1, "steamid3": STEAMID3, "apps": {str(k): v for k, v in OWNED.items()}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def hidden_appid(owned_name: str) -> int:
    return Shortcut.create(app_name=owned_name, exe=STREAM_SH).appid


def files_under(world: World, appid: int) -> list[str]:
    return sorted(n for n in world.grid_files() if n.startswith(str(appid)))


class GridRenames:
    """Counts ``os.replace`` calls whose target is in ``grid/`` and, when
    asked, dies on the N-th one the way ``kill -9`` would (a
    ``SimulatedCrash`` is a ``BaseException``, so nothing catches it)."""

    def __init__(self, monkeypatch, grid_dir: Path, *, crash_on: int | None = None) -> None:
        self.grid_dir = grid_dir
        self.crash_on = crash_on
        self.count = 0
        self.renamed: list[tuple[str, str]] = []
        original = os.replace

        def replace(src, dst, *args, **kwargs):
            if Path(dst).parent == self.grid_dir and not str(dst).endswith(".vdf"):
                self.count += 1
                if self.crash_on is not None and self.count == self.crash_on:
                    raise SimulatedCrash(f"killed on grid rename {self.count}")
                self.renamed.append((Path(src).name, Path(dst).name))
            return original(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", replace)


@pytest.fixture
def dressed_world(world, tmp_path, monkeypatch) -> tuple[World, dict[str, int], str]:
    """The two adopted STL-era entries, visible and fully dressed, with an
    owned-apps file that will flip both to ``stream`` (10 renames, no
    downloads)."""
    host_publishes(tmp_path, monkeypatch, NAMES)
    result = world.run(["sync"])
    assert result.code == 0, result.err
    old = {name: world.appid_for(name) for name in NAMES}
    for appid in old.values():
        assert files_under(world, appid) == expected_grid_files(appid)
    return world, old, owned_file(tmp_path)


def assert_flipped_and_complete(world: World, old: dict[str, int], result) -> None:
    assert result.code == 0, result.err
    assert result.calls == []  # everything was on disk already
    for name in NAMES:
        entry = next(s for s in world.owned() if s.moonlight_name(TEMPLATE, SUFFIX) == name)
        assert entry.is_hidden == 1 and entry.app_name == OWNED[STEAM_OF[name]]
        new = hidden_appid(entry.app_name)
        assert entry.appid == new
        assert files_under(world, new) == expected_grid_files(new)
        assert files_under(world, old[name]) == []
        assert entry.icon == str((world.grid_dir / f"{new}_icon.jpg").resolve())
    assert len(world.grid_files()) == 10
    assert not any(n.endswith(".part") for n in world.grid_files())
    assert [s.index_key for s in world.library()] == ["0", "1", "2"]
    assert result.runner.shutdowns == 1 and result.runner.relaunches == 1


@pytest.mark.parametrize("crash_on", [1, 3, 5, 6, 8, 10])
def test_a_crash_between_or_inside_two_replacements_costs_nothing_on_rerun(
    dressed_world, monkeypatch, crash_on
):
    """Decky spec 3.4.2 / spec 3.9: die on the N-th grid rename (N = 6 is
    exactly between the two replacements: the first's five files moved,
    the second's none). The vdf is untouched; the rerun redoes only the
    renames that did not happen and makes no HTTP call."""
    world, old, owned = dressed_world
    vdf_before = world.shortcuts_path.read_bytes()
    backups_before = world.backups()
    crashing = GridRenames(monkeypatch, world.grid_dir, crash_on=crash_on)

    with pytest.raises(SimulatedCrash):
        world.run(["sync", "--owned-apps", owned])

    done = crash_on - 1
    assert crashing.count == crash_on
    assert world.shortcuts_path.read_bytes() == vdf_before  # the vdf: untouched
    assert world.backups() == backups_before
    new = {name: hidden_appid(OWNED[STEAM_OF[name]]) for name in NAMES}
    moved = sum(len(files_under(world, new[name])) for name in NAMES)
    left = sum(len(files_under(world, old[name])) for name in NAMES)
    assert (moved, left) == (done, 10 - done)
    assert len(world.grid_files()) == 10  # nothing lost, nothing duplicated
    # The old entries are still what is in the file, so nothing is hidden yet.
    assert all(s.is_hidden == 0 for s in world.owned())

    resumed = GridRenames(monkeypatch, world.grid_dir)
    result = world.run(["sync", "--owned-apps", owned])

    assert resumed.count == 10 - done  # a done rename is skipped, not redone
    assert_flipped_and_complete(world, old, result)


def test_a_crash_in_the_art_phase_after_both_replacements_redoes_only_that_slot(
    dressed_world, monkeypatch
):
    """The renames are done and durable by the time the art phase starts:
    a transport that dies on the one download the phase needs leaves the
    vdf untouched, and the rerun makes exactly that one call."""
    world, old, owned = dressed_world
    (world.grid_dir / f"{old['Hollow Knight']}_hero.jpg").unlink()
    vdf_before = world.shortcuts_path.read_bytes()
    hero_url = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{HOLLOW_STEAM}/library_hero.jpg"
    dying = FakeTransport(fail_with={hero_url: SimulatedCrash("network died")})

    with pytest.raises(SimulatedCrash):
        world.run(["sync", "--owned-apps", owned], transport=dying)

    assert dying.calls == [hero_url]
    assert world.shortcuts_path.read_bytes() == vdf_before
    new_hollow = hidden_appid(OWNED[HOLLOW_STEAM])
    new_elden = hidden_appid(OWNED[ELDEN_STEAM])
    assert len(files_under(world, new_elden)) == 5
    assert len(files_under(world, new_hollow)) == 4
    assert files_under(world, old["Elden Ring"]) == []
    assert files_under(world, old["Hollow Knight"]) == []

    result = world.run(["sync", "--owned-apps", owned])
    assert result.code == 0, result.err
    assert result.calls == [hero_url]
    assert len(world.grid_files()) == 10
    assert all(s.is_hidden == 1 for s in world.owned())
    assert files_under(world, new_hollow) == expected_grid_files(new_hollow)


def test_a_refused_write_after_the_renames_is_finished_by_the_next_run(
    dressed_world, tmp_path
):
    """Exit 2 lands after the renames (they precede the art phase): the old
    entries stay in the file with their art now under the new appids, and
    the next run -- Steam down -- plans the same replacements, finds every
    rename done, and writes."""
    from tests.fakes import FakeRunner
    from tests.test_sync_e2e import make_config

    world, old, owned = dressed_world
    vdf_before = world.shortcuts_path.read_bytes()
    no_restart = make_config(tmp_path, restart_steam=False)

    refused = world.run(["sync", "--owned-apps", owned], config=no_restart)
    assert refused.code == 2
    assert world.shortcuts_path.read_bytes() == vdf_before
    assert sum(len(files_under(world, appid)) for appid in old.values()) == 0

    result = world.run(
        ["sync", "--owned-apps", owned], runner=FakeRunner(running=False), config=no_restart
    )
    assert result.code == 0, result.err
    assert result.calls == []
    assert result.runner.shutdowns == 0 and result.runner.relaunches == 0
    assert all(s.is_hidden == 1 for s in world.owned())
    assert len(world.grid_files()) == 10
