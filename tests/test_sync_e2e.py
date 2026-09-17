"""``sync``, ``remove``, ``ignore`` and ``list`` end to end (spec PR-5, 3.6, 3.7, 3.9).

A temporary ``$STEAM_ROOT`` with a real-shaped ``userdata/<id>/config``, a
fake ``moonlight`` on ``PATH`` that prints a ``list --csv`` capture, a fake
HTTP layer, and a fake Steam process. Nothing here touches the network, a
real Steam install, or the developer's own config.

The tests the spec names are marked in their docstrings: (a) sync twice,
(b) resumability on a 500-title library, (c) Steam running with
``restart_steam = false``, (d) ``--limit``, (e) unmatched titles.

TODO(real-data): the fixtures underneath are SYNTHETIC -- the 3-entry
``shortcuts_synthetic.vdf`` (PR-2), the artwork ``manifest.json`` (PR-4) and
``moonlight_list_large_synthetic.csv`` (this PR). Each one's README and
generator says how to capture the real thing; every test here asks for its
fixture by role, so a real capture is a file drop, never a test rewrite.
"""

from __future__ import annotations

import io
import json
import signal
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

from moonlight_steam_sync import config as config_module
from moonlight_steam_sync import sync, vdf
from moonlight_steam_sync.__main__ import build_parser, main
from moonlight_steam_sync.art.cli import build_services
from moonlight_steam_sync.art.select import SLOTS, existing_slot_file
from moonlight_steam_sync.config import Config
from moonlight_steam_sync.shortcuts import ShortcutsFile
from tests.art_fixtures import (
    CALLS_PER_TITLE,
    FIXTURE_DIR,
    BulkTransport,
    FakeTransport,
    SimulatedCrash,
    make_fetcher,
)
from tests.fakes import (
    STEAMID3,
    STREAM_SH,
    FakeRunner,
    csv_row,
    fake_moonlight_argv,
    install_fake_moonlight,
    make_steam_root,
    moonlight_csv,
    point_steam_root_at,
)

oracle = pytest.importorskip("vdf", reason="the vdf package is a dev dependency (test oracle)")

HOST = "MY-GAMING-PC"
SUFFIX = " (streaming)"
#: The launch-options template the PR-2 fixture's STL-era entries were written with.
TEMPLATE = 'launch "{name}"'

#: The six-title art manifest (PR-4) knows these; Hollow Knight is the
#: adoptable entry in the PR-2 shortcuts fixture.
MANIFEST_TITLES = [
    "Elden Ring",
    "Hollow Knight",
    "Hades II™",
    "Fan Made Adventure",
    "Totally Unknown Title",
]


# ---------------------------------------------------------------------------
# the world
# ---------------------------------------------------------------------------


@dataclass
class World:
    tmp_path: Path
    root: Path
    config: Config
    cache_path: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "userdata" / str(STEAMID3) / "config"

    @property
    def shortcuts_path(self) -> Path:
        return self.config_dir / "shortcuts.vdf"

    @property
    def grid_dir(self) -> Path:
        return self.config_dir / "grid"

    def backups(self) -> list[Path]:
        return sorted(self.config_dir.glob("shortcuts.vdf.bak-*"))

    def library(self) -> ShortcutsFile:
        return ShortcutsFile.read(self.shortcuts_path)

    def owned(self):
        return self.library().owned(self.config.exe)

    def owned_names(self) -> list[str]:
        return [s.moonlight_name(TEMPLATE, SUFFIX) for s in self.owned()]

    def grid_files(self) -> list[str]:
        if not self.grid_dir.is_dir():
            return []
        return sorted(p.name for p in self.grid_dir.iterdir())

    def cache_titles(self) -> list[str]:
        if not self.cache_path.is_file():
            return []
        return sorted(json.loads(self.cache_path.read_text())["titles"])

    def appid_for(self, name: str) -> int:
        return sync.new_shortcut(self.config, name).appid

    def run(
        self,
        argv: list[str],
        *,
        transport=None,
        runner: FakeRunner | None = None,
        config: Config | None = None,
    ):
        """Run one orchestration command against this world."""
        transport = transport if transport is not None else FakeTransport()
        runner = runner if runner is not None else FakeRunner(running=True)
        config = config or self.config
        services = build_services(
            config, cache_path=self.cache_path, fetcher=make_fetcher(transport)
        )
        deps = sync.Deps(runner=runner, services=services, cache_path=self.cache_path)
        args = build_parser().parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        command = {
            "sync": sync.cmd_sync,
            "list": sync.cmd_list,
            "ignore": sync.cmd_ignore,
            "remove": sync.cmd_remove,
        }[args.command]
        code = command(args, config, deps=deps, out=out, err=err)
        return Result(code, out.getvalue(), err.getvalue(), transport, runner)


@dataclass
class Result:
    code: int
    out: str
    err: str
    transport: object
    runner: FakeRunner

    @property
    def calls(self) -> list[str]:
        return list(self.transport.calls)


def make_config(tmp_path: Path, **overrides) -> Config:
    values = dict(
        host=HOST,
        name_suffix=SUFFIX,
        exe=STREAM_SH,
        launch_options=TEMPLATE,
        ignore=["Desktop", "Steam Big Picture"],
        restart_steam=True,
        sgdb_api_key="fixture-key",
        config_path=tmp_path / "config.toml",
    )
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def world(tmp_path, monkeypatch, synthetic_shortcuts_vdf) -> World:
    """A Steam tree holding the PR-2 fixture: two adoptable STL-era entries
    (Elden Ring, Hollow Knight) and one foreign entry (RetroArch)."""
    root = make_steam_root(tmp_path / "steam", shortcuts=synthetic_shortcuts_vdf)
    point_steam_root_at(monkeypatch, root)
    return World(tmp_path, root, make_config(tmp_path), tmp_path / "cache" / "matches.json")


@pytest.fixture
def fresh_world(tmp_path, monkeypatch) -> World:
    """A fresh Steam user: no shortcuts.vdf at all (spec 3.6)."""
    root = make_steam_root(tmp_path / "steam")
    point_steam_root_at(monkeypatch, root)
    return World(tmp_path, root, make_config(tmp_path), tmp_path / "cache" / "matches.json")


@pytest.fixture
def boxart(tmp_path: Path) -> Path:
    path = tmp_path / "moonlight-cache" / "boxart" / "abc-host-uuid" / "1.png"
    path.parent.mkdir(parents=True)
    path.write_bytes((FIXTURE_DIR / "images" / "tiny.png").read_bytes())
    return path


def host_publishes(tmp_path, monkeypatch, names, *, boxart: dict[str, Path] | None = None):
    boxart = boxart or {}
    rows = [
        csv_row(name, i, boxart=str(boxart[name]) if name in boxart else None)
        for i, name in enumerate(names, start=1)
    ]
    install_fake_moonlight(tmp_path, monkeypatch, moonlight_csv(rows))


def expected_grid_files(appid: int, *, logo_png: bool = True) -> list[str]:
    return sorted(
        [
            f"{appid}.jpg",
            f"{appid}_hero.jpg",
            f"{appid}_icon.jpg",
            f"{appid}_logo.{'png' if logo_png else 'jpg'}",
            f"{appid}p.jpg",
        ]
    )


# ---------------------------------------------------------------------------
# (a) sync twice
# ---------------------------------------------------------------------------


def test_a_sync_twice_second_run_is_a_no_op(world, tmp_path, monkeypatch, boxart):
    """Spec PR-5 (a): the second run makes zero HTTP calls and no vdf write,
    the backup exists, the file round-trips through the vdf oracle, and the
    grid files have the right names."""
    host_publishes(
        tmp_path,
        monkeypatch,
        [*MANIFEST_TITLES, "Desktop", "Steam Big Picture"],
        boxart={"Totally Unknown Title": boxart},
    )
    before = oracle.binary_loads(world.shortcuts_path.read_bytes())

    first = world.run(["sync"])
    assert first.code == 0, first.err
    assert fake_moonlight_argv(tmp_path) == ["list", HOST, "--csv"]
    assert "7 app(s) published, 2 ignored, 2 already in Steam, 3 to add" in first.out
    # One restart, in the right order: shutdown, then write, then relaunch.
    assert first.runner.shutdowns == 1 and first.runner.relaunches == 1
    assert "Steam restarted" in first.out

    # The library: the three foreign/adopted entries kept, three added, no dupes.
    data = world.shortcuts_path.read_bytes()
    after = oracle.binary_loads(data)
    assert oracle.binary_dumps(after) == data  # round-trips through the oracle
    assert vdf.loads(data) == after
    assert after["shortcuts"]["2"] == before["shortcuts"]["2"]  # RetroArch untouched
    assert world.owned_names() == [
        "Elden Ring",
        "Hollow Knight",
        "Hades II™",
        "Fan Made Adventure",
        "Totally Unknown Title",
    ]
    appids = [s.appid for s in world.library()]
    assert len(appids) == len(set(appids)) == 6
    assert [s.index_key for s in world.library()] == ["0", "1", "2", "3", "4", "5"]
    assert len(world.backups()) == 1

    # New entries follow the write policy (spec 3.6).
    hades = next(s for s in world.owned() if s.app_name == "Hades II™ (streaming)")
    assert hades.exe == f'"{STREAM_SH}"'
    assert hades.launch_options == 'launch "Hades II™"'
    assert hades.allow_desktop_config == 1 and hades.allow_overlay == 1
    assert hades.icon == str((world.grid_dir / f"{hades.appid}_icon.jpg").resolve())

    # Grid files carry the *shortcut's* appid, for adopted and added alike.
    elden = next(s for s in world.owned() if s.app_name == "Elden Ring (streaming)")
    assert all(name in world.grid_files() for name in expected_grid_files(elden.appid))
    assert all(name in world.grid_files() for name in expected_grid_files(hades.appid))
    unknown = world.appid_for("Totally Unknown Title")
    assert world.grid_files().count(f"{unknown}p.png") == 1  # host box art, portrait only
    assert not any(n.endswith(".part") for n in world.grid_files())
    # The adopted entry's dangling icon path (a device path that does not
    # exist here) was repointed at the file that now does exist.
    hollow = next(s for s in world.owned() if s.app_name == "Hollow Knight (streaming)")
    assert hollow.icon == str((world.grid_dir / f"{hollow.appid}_icon.jpg").resolve())

    second = world.run(["sync"])
    assert second.code == 0, second.err
    assert second.calls == []
    assert world.shortcuts_path.read_bytes() == data
    assert len(world.backups()) == 1
    assert second.runner.shutdowns == 0 and second.runner.relaunches == 0
    assert "nothing to write" in second.out
    assert "0 already in Steam" not in second.out
    assert "5 already in Steam, 0 to add" in second.out


def test_a_deleted_shortcut_comes_back_unless_ignored(world, tmp_path, monkeypatch):
    """Spec 3.7: the library is the source of truth, there is no state file."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hollow Knight"])
    library = world.library()
    library.remove(next(s for s in library if s.app_name == "Elden Ring (streaming)"))
    library.write()

    assert world.run(["sync"]).code == 0
    assert "Elden Ring" in world.owned_names()

    library = world.library()
    library.remove(next(s for s in library if s.app_name == "Elden Ring (streaming)"))
    library.write()
    ignoring = make_config(tmp_path, ignore=["Elden Ring"])
    result = world.run(["sync"], config=ignoring)
    assert result.code == 0
    assert "Elden Ring" not in world.owned_names()
    assert "1 ignored" in result.out


def test_an_adopted_entry_the_host_no_longer_publishes_is_still_dressed(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    result = world.run(["sync"])
    assert result.code == 0
    assert "Hollow Knight" in world.owned_names()
    hollow = next(s for s in world.owned() if s.app_name == "Hollow Knight (streaming)")
    assert all(name in world.grid_files() for name in expected_grid_files(hollow.appid))
    assert "[2/2] Hollow Knight: portrait=official" in result.out


# ---------------------------------------------------------------------------
# (b) resumability on a 500-title library
# ---------------------------------------------------------------------------


def remaining_calls(world: World, transport: BulkTransport, names: list[str]) -> list[str]:
    """The calls a re-run must make, derived from what is on disk (spec 3.9.1):
    a title in ``matches.json`` costs no lookups, a slot with a file costs no
    download, and the icon slot costs its hash lookup plus the download."""
    cached = set(world.cache_titles())
    calls: list[str] = []
    for name in names:
        urls = transport.title_calls(name)
        appid = world.appid_for(name)
        if name not in cached:
            calls.extend(urls[:2])
        for slot, url in zip(SLOTS[:4], urls[2:6], strict=True):
            if existing_slot_file(world.grid_dir, appid, slot) is None:
                calls.append(url)
        if existing_slot_file(world.grid_dir, appid, SLOTS[4]) is None:
            calls.extend(urls[6:8])
    return calls


def durable_state_after(n_calls: int) -> tuple[int, int]:
    """(cache entries, grid files) that must exist after exactly ``n_calls``
    answered calls into a fresh all-official library."""
    done, r = divmod(n_calls, CALLS_PER_TITLE)
    cache_entries = done + (1 if r >= 2 else 0)
    grid_files = 5 * done + (0 if r < 3 else min(r - 2, 4))
    return cache_entries, grid_files


def assert_library_complete(world: World, names: list[str], result: Result) -> None:
    assert result.code == 0, result.err
    assert world.owned_names() == names
    appids = [s.appid for s in world.library()]
    assert len(appids) == len(set(appids)) == len(names)
    assert len(world.grid_files()) == 5 * len(names)
    assert not any(n.endswith(".part") for n in world.grid_files())
    assert world.cache_titles() == sorted(names)
    assert result.runner.shutdowns == 1 and result.runner.relaunches == 1


@pytest.fixture
def large_host(tmp_path, monkeypatch, large_library_csv) -> list[str]:
    """The 500-title host, served by the fake ``moonlight`` (fixture by role)."""
    install_fake_moonlight(tmp_path, monkeypatch, large_library_csv)
    from moonlight_steam_sync.moonlight import _parse_csv

    names = [app.name for app in _parse_csv(large_library_csv)]
    assert len(names) >= 500
    return names


@pytest.mark.parametrize("crash_after", [0, 1, 2, 3, 6, 7, 8, 9, 2003, 3999])
def test_b_a_crash_after_n_calls_costs_nothing_already_done(
    fresh_world, large_host, crash_after
):
    """Spec PR-5 (b) / 3.9: a fake HTTP layer that dies after the Nth call.
    The vdf is untouched, N-worth of files and cache entries exist, and a
    re-run completes with exactly the remaining calls and no duplicates."""
    world, names = fresh_world, large_host
    crashing = BulkTransport(names, crash_after=crash_after)

    with pytest.raises(SimulatedCrash):
        world.run(["sync"], transport=crashing)

    assert len(crashing.calls) == crash_after + 1
    assert not world.shortcuts_path.exists()  # the vdf: untouched
    assert world.backups() == []
    cache_entries, grid_files = durable_state_after(crash_after)
    assert len(world.cache_titles()) == cache_entries
    assert len(world.grid_files()) == grid_files

    resumed = BulkTransport(names)
    expected = remaining_calls(world, resumed, names)
    result = world.run(["sync"], transport=resumed)
    assert result.calls == expected
    assert len(crashing.calls) - 1 + len(result.calls) <= CALLS_PER_TITLE * len(names) + 2
    assert_library_complete(world, names, result)


@pytest.mark.parametrize("truncate_call", [3, 8, 3995])
def test_b_a_download_that_dies_mid_body_leaves_only_a_part_file(
    fresh_world, large_host, truncate_call
):
    """Spec 3.9 item 3: a killed download leaves ``.part``, never a truncated
    image that would count as done; the re-run downloads that slot again."""
    world, names = fresh_world, large_host
    dying = BulkTransport(names, truncate_call=truncate_call)
    with pytest.raises(SimulatedCrash):
        world.run(["sync"], transport=dying)

    parts = [n for n in world.grid_files() if n.endswith(".part")]
    assert len(parts) == 1
    cache_entries, grid_files = durable_state_after(truncate_call - 1)
    assert len(world.grid_files()) - len(parts) == grid_files
    assert len(world.cache_titles()) == cache_entries
    assert not world.shortcuts_path.exists()

    resumed = BulkTransport(names)
    expected = remaining_calls(world, resumed, names)
    assert dying.calls[-1] in expected  # the dead download is redone
    result = world.run(["sync"], transport=resumed)
    assert result.calls == expected
    assert_library_complete(world, names, result)


@pytest.mark.parametrize("interrupt_after", [5, 800])
def test_b_ctrl_c_exits_130_and_the_same_command_finishes_the_job(
    fresh_world, large_host, interrupt_after
):
    """Spec 3.9 item 5: on SIGINT flush the cache, say how to resume, exit
    130; the vdf is untouched and the re-run does only the remainder."""
    world, names = fresh_world, large_host
    interrupted = BulkTransport(
        names, crash_after=interrupt_after, crash_with=KeyboardInterrupt()
    )
    result = world.run(["sync"], transport=interrupted)
    assert result.code == 130
    assert "resume with the same command" in result.err
    assert not world.shortcuts_path.exists()
    assert result.runner.shutdowns == 0
    cache_entries, grid_files = durable_state_after(interrupt_after)
    assert len(world.cache_titles()) == cache_entries
    assert len(world.grid_files()) == grid_files

    resumed = BulkTransport(names)
    expected = remaining_calls(world, resumed, names)
    result = world.run(["sync"], transport=resumed)
    assert result.calls == expected
    assert_library_complete(world, names, result)


def test_b_a_429_storm_exits_4_with_everything_so_far_kept(fresh_world, large_host):
    """Spec 3.9 item 6: five consecutive 429s stop the run (exit 4) rather
    than burn the key; nothing done is lost and the re-run finishes."""
    world, names = fresh_world, large_host
    storm = BulkTransport(names, rate_limit_after=160)
    result = world.run(["sync"], transport=storm)
    assert result.code == 4
    assert "429" in result.out
    assert "rerun the same command to continue" in result.out
    assert not world.shortcuts_path.exists()
    assert result.runner.shutdowns == 0
    assert len(world.cache_titles()) == 20 and len(world.grid_files()) == 100

    resumed = BulkTransport(names)
    expected = remaining_calls(world, resumed, names)
    assert len(expected) == CALLS_PER_TITLE * (len(names) - 20)
    result = world.run(["sync"], transport=resumed)
    assert result.calls == expected
    assert_library_complete(world, names, result)


def test_b_the_match_cache_is_on_disk_after_every_title(fresh_world, large_host):
    """Spec 3.9 item 2, at the orchestration level: crash right after the
    first title's last call and its cache entry is already there."""
    world, names = fresh_world, large_host
    crashing = BulkTransport(names, crash_after=CALLS_PER_TITLE)
    with pytest.raises(SimulatedCrash):
        world.run(["sync"], transport=crashing)
    assert world.cache_titles() == [names[0]]
    assert len(world.grid_files()) == 5


# ---------------------------------------------------------------------------
# (c) Steam running and restart_steam = false
# ---------------------------------------------------------------------------


def test_c_refuses_to_write_under_a_live_steam_when_it_may_not_restart_it(
    world, tmp_path, monkeypatch
):
    """Spec PR-5 (c): exit 2, nothing written, art still on disk."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™"])
    before = world.shortcuts_path.read_bytes()
    no_restart = make_config(tmp_path, restart_steam=False)

    result = world.run(["sync"], runner=FakeRunner(running=True), config=no_restart)
    assert result.code == 2
    assert "restart_steam is false" in result.err
    assert world.shortcuts_path.read_bytes() == before
    assert world.backups() == []
    assert result.runner.shutdowns == 0 and result.runner.relaunches == 0
    hades = world.appid_for("Hades II™")
    assert all(name in world.grid_files() for name in expected_grid_files(hades))

    # Once Steam is down the same command writes, without touching Steam and
    # without a single HTTP call: the art phase was durable.
    result = world.run(["sync"], runner=FakeRunner(running=False), config=no_restart)
    assert result.code == 0, result.err
    assert result.calls == []
    assert "Hades II™" in world.owned_names()
    assert result.runner.shutdowns == 0 and result.runner.relaunches == 0
    assert "Steam restarted" not in result.out


def test_c_steam_that_will_not_exit_is_exit_2_and_nothing_is_written(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    before = world.shortcuts_path.read_bytes()
    stubborn = FakeRunner(running=True, ignores_shutdown=True)
    result = world.run(["sync"], runner=stubborn)
    assert result.code == 2
    assert "did not exit within 30 s" in result.err
    assert world.shortcuts_path.read_bytes() == before
    assert stubborn.shutdowns == 1 and stubborn.relaunches == 0
    assert stubborn.slept >= 30


def test_steam_that_was_not_running_is_written_to_and_not_started(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    result = world.run(["sync"], runner=FakeRunner(running=False))
    assert result.code == 0
    assert "Hades II™" in world.owned_names()
    assert result.runner.shutdowns == 0 and result.runner.relaunches == 0
    assert "restart Steam to see the new artwork" in result.out


def test_new_art_alone_restarts_steam_once_but_an_unchanged_library_does_not(
    world, tmp_path, monkeypatch
):
    """Spec 3.9 item 8: one restart per run; and none when nothing changed."""
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert world.run(["sync"]).code == 0
    elden = next(s for s in world.owned() if s.app_name == "Elden Ring (streaming)")
    (world.grid_dir / f"{elden.appid}_hero.jpg").unlink()
    data = world.shortcuts_path.read_bytes()

    result = world.run(["sync"])
    assert result.code == 0
    assert result.calls == [
        "https://cdn.cloudflare.steamstatic.com/steam/apps/1245620/library_hero.jpg"
    ]
    assert world.shortcuts_path.read_bytes() == data  # nothing to write...
    assert result.runner.shutdowns == 1 and result.runner.relaunches == 1  # ...but a restart


# ---------------------------------------------------------------------------
# (d) --limit
# ---------------------------------------------------------------------------


def test_d_limit_five_adds_five_and_reports_the_rest_pending(fresh_world, large_host):
    """Spec PR-5 (d): ``--limit 5`` on 500 new titles adds 5, reports 495 pending."""
    world, names = fresh_world, large_host
    result = world.run(["sync", "--limit", "5"], transport=BulkTransport(names))
    assert result.code == 0, result.err
    assert world.owned_names() == names[:5]
    assert len(result.calls) == 5 * CALLS_PER_TITLE
    assert "495 pending (--limit 5)" in result.out
    assert "495 still pending after --limit 5" in result.out
    assert "[5/5]" in result.out and "[6/" not in result.out

    # The next batch is the next five in host-list order; no duplicates.
    result = world.run(["sync", "--limit", "5"], transport=BulkTransport(names))
    assert result.code == 0
    assert world.owned_names() == names[:10]
    assert len(result.calls) == 5 * CALLS_PER_TITLE
    assert "490 still pending" in result.out


def test_d_limit_zero_adds_nothing_but_still_dresses_adopted_entries(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™"])
    result = world.run(["sync", "--limit", "0"])
    assert result.code == 0
    assert "Hades II™" not in world.owned_names()
    assert "1 pending (--limit 0)" in result.out
    assert "[1/2] Elden Ring" in result.out


# ---------------------------------------------------------------------------
# (e) unmatched titles
# ---------------------------------------------------------------------------


def test_e_unmatched_titles_exit_zero_and_are_named_in_the_summary(
    fresh_world, tmp_path, monkeypatch
):
    """Spec PR-5 (e) / spec 7: no art for a non-Steam game is accepted."""
    world = fresh_world
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Totally Unknown Title"])
    result = world.run(["sync"])
    assert result.code == 0, result.err
    assert "no match for 1 title(s): Totally Unknown Title" in result.out
    assert "accepted outcome" in result.out
    assert world.owned_names() == ["Elden Ring", "Totally Unknown Title"]
    # The unmatched title is still a shortcut, just an undressed one.
    unknown = world.appid_for("Totally Unknown Title")
    assert not any(n.startswith(str(unknown)) for n in world.grid_files())

    # And it is remembered: the next run does not search for it again.
    result = world.run(["sync"])
    assert result.calls == []


# ---------------------------------------------------------------------------
# --dry-run and --no-art
# ---------------------------------------------------------------------------


def test_dry_run_prints_the_plan_with_per_slot_urls_and_writes_nothing_under_steam(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hades II™", "Desktop"])
    before = world.shortcuts_path.read_bytes()
    result = world.run(["sync", "--dry-run"])
    assert result.code == 0, result.err
    assert "add      Hades II™" in result.out
    assert "adopted  Elden Ring" in result.out
    assert "adopted  Hollow Knight" in result.out
    assert "ignored  Desktop" in result.out
    assert "portrait: official: https://cdn.cloudflare.steamstatic.com/steam/apps/1145350/" in (
        result.out
    )
    assert "dry run: nothing written" in result.out
    assert world.shortcuts_path.read_bytes() == before
    assert world.grid_files() == []
    assert result.runner.shutdowns == 0
    # The one thing a dry run does write: the match cache, so the real run
    # reuses every resolution.
    assert world.cache_titles() == ["Elden Ring", "Hades II™", "Hollow Knight"]
    assert not any("/steam/apps/" in c for c in result.calls)  # no downloads


def test_dry_run_without_art_makes_no_http_calls(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    result = world.run(["sync", "--dry-run", "--no-art"])
    assert result.code == 0
    assert result.calls == []
    assert "add      Hades II™" in result.out


def test_no_art_writes_shortcuts_and_points_icon_at_art_already_on_disk(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    hades = world.appid_for("Hades II™")
    world.grid_dir.mkdir()
    handmade = world.grid_dir / f"{hades}_icon.png"
    handmade.write_bytes(b"\x89PNG\r\n\x1a\nhand-picked")

    result = world.run(["sync", "--no-art"])
    assert result.code == 0, result.err
    assert result.calls == []
    entry = next(s for s in world.owned() if s.appid == hades)
    assert entry.icon == str(handmade.resolve())
    assert result.runner.shutdowns == 1


# ---------------------------------------------------------------------------
# failures before anything is touched
# ---------------------------------------------------------------------------


def test_moonlight_failing_is_exit_3_and_nothing_is_touched(world, tmp_path, monkeypatch):
    install_fake_moonlight(tmp_path, monkeypatch, "", exit_code=1)
    before = world.shortcuts_path.read_bytes()
    result = world.run(["sync"])
    assert result.code == 3
    assert "moonlight list" in result.err
    assert world.shortcuts_path.read_bytes() == before
    assert result.calls == [] and result.runner.shutdowns == 0


def test_no_host_is_exit_1(world, tmp_path, monkeypatch):
    result = world.run(["sync"], config=make_config(tmp_path, host=""))
    assert result.code == 1
    assert "no host configured" in result.err


def test_a_corrupt_vdf_aborts_before_moonlight_is_even_asked(world, tmp_path, monkeypatch):
    world.shortcuts_path.write_bytes(b"\x00shortcuts\x00\x01garbage")
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    result = world.run(["sync"])
    assert result.code == 1
    assert "nothing was touched" in result.err
    assert not (tmp_path / "moonlight-argv.txt").exists()
    assert world.backups() == [] and result.runner.shutdowns == 0


def test_a_negative_limit_is_a_usage_error(world):
    assert world.run(["sync", "--limit", "-1"]).code == 1


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_by_name_deletes_the_entry_and_its_grid_files(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Hollow Knight"])
    assert world.run(["sync"]).code == 0
    elden = next(s for s in world.owned() if s.app_name == "Elden Ring (streaming)")
    (world.grid_dir / f"{elden.appid}p.part").write_bytes(b"half")
    before = oracle.binary_loads(world.shortcuts_path.read_bytes())

    result = world.run(["remove", "Elden Ring"])
    assert result.code == 0, result.err
    assert world.owned_names() == ["Hollow Knight"]
    assert not any(n.startswith(str(elden.appid)) for n in world.grid_files())
    assert "removed 1 shortcut(s) and 6 grid file(s)" in result.out
    assert result.runner.shutdowns == 1 and result.runner.relaunches == 1
    after = oracle.binary_loads(world.shortcuts_path.read_bytes())
    assert after["shortcuts"]["2"] == before["shortcuts"]["2"]  # RetroArch untouched
    assert "1" in after["shortcuts"] and "0" not in after["shortcuts"]  # keys kept
    assert len(world.backups()) == 2


def test_remove_all_leaves_foreign_entries_alone(world, tmp_path, monkeypatch):
    result = world.run(["remove", "--all"])
    assert result.code == 0
    assert world.owned_names() == []
    assert [s.app_name for s in world.library()] == ["RetroArch"]


def test_remove_an_unknown_name_touches_nothing(world):
    before = world.shortcuts_path.read_bytes()
    result = world.run(["remove", "Elden Ring", "Nope"])
    assert result.code == 1
    assert "'Nope'" in result.err and "nothing was touched" in result.err
    assert world.shortcuts_path.read_bytes() == before
    assert world.owned_names() == ["Elden Ring", "Hollow Knight"]


def test_remove_under_a_live_steam_it_may_not_restart_keeps_the_art(
    world, tmp_path, monkeypatch
):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring"])
    assert world.run(["sync"]).code == 0
    files = world.grid_files()
    result = world.run(["remove", "Elden Ring"], config=make_config(tmp_path, restart_steam=False))
    assert result.code == 2
    assert world.grid_files() == files
    assert "Elden Ring" in world.owned_names()


def test_remove_nothing_to_remove(fresh_world):
    result = fresh_world.run(["remove", "--all"])
    assert result.code == 0 and "nothing to remove" in result.out
    assert not fresh_world.shortcuts_path.exists()


# ---------------------------------------------------------------------------
# ignore and list
# ---------------------------------------------------------------------------


def test_ignore_all_prints_valid_toml_of_every_not_yet_added_app(world, tmp_path, monkeypatch):
    host_publishes(
        tmp_path, monkeypatch, ["Elden Ring", 'Odd "Name"', "Hades II™", "Desktop"]
    )
    result = world.run(["ignore", "--all"])
    assert result.code == 0, result.err
    parsed = tomllib.loads(result.out)
    assert parsed["ignore"] == ["Desktop", "Steam Big Picture", 'Odd "Name"', "Hades II™"]
    assert "never writes it" in result.err
    assert world.grid_files() == [] and result.calls == []


def test_ignore_by_name_appends_to_the_configured_list(world):
    result = world.run(["ignore", "Hades II™", "Desktop"])
    assert result.code == 0
    assert tomllib.loads(result.out)["ignore"] == ["Desktop", "Steam Big Picture", "Hades II™"]


def test_ignore_with_nothing_configured_prints_an_empty_array(fresh_world, tmp_path):
    result = fresh_world.run(["ignore", "X"], config=make_config(tmp_path, ignore=[]))
    assert tomllib.loads(result.out)["ignore"] == ["X"]


def test_list_annotates_added_ignored_and_new(world, tmp_path, monkeypatch):
    host_publishes(tmp_path, monkeypatch, ["Elden Ring", "Desktop", "Hades II™"])
    result = world.run(["list"])
    assert result.code == 0, result.err
    assert result.out.splitlines()[:3] == [
        "added    Elden Ring",
        "ignored  Desktop",
        "new      Hades II™",
    ]
    assert "3 app(s) published, 1 ignored, 1 already in Steam, 1 to add" in result.out
    assert result.calls == []


# ---------------------------------------------------------------------------
# the art command shares the write path
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("SGDB_API_KEY", "fixture-key")
    monkeypatch.setattr(
        "moonlight_steam_sync.art.cli.Fetcher", lambda **kwargs: make_fetcher(FakeTransport())
    )
    config_path.write_text(
        f'host = "{HOST}"\nname_suffix = "{SUFFIX}"\nexe = "{STREAM_SH}"\n'
        f"launch_options = 'launch \"{{name}}\"'\n"
    )
    return config_path


def test_art_restarts_steam_once_for_its_icon_patches(world, isolated_config, capsys):
    runner = FakeRunner(running=True)
    code = main(["art", "--only", "Elden Ring"], deps=sync.Deps(runner=runner))
    assert code == 0, capsys.readouterr().err
    elden = next(s for s in world.owned() if s.app_name == "Elden Ring (streaming)")
    assert elden.icon.endswith(f"{elden.appid}_icon.jpg")
    assert runner.shutdowns == 1 and runner.relaunches == 1


def test_art_refuses_to_write_under_a_live_steam_it_may_not_restart(
    world, isolated_config, capsys
):
    isolated_config.write_text(isolated_config.read_text() + "restart_steam = false\n")
    before = world.shortcuts_path.read_bytes()
    runner = FakeRunner(running=True)
    code = main(["art", "--only", "Elden Ring"], deps=sync.Deps(runner=runner))
    assert code == 2
    assert "restart_steam is false" in capsys.readouterr().err
    assert world.shortcuts_path.read_bytes() == before
    assert runner.shutdowns == 0
    assert world.grid_files()  # the art is on disk regardless


def test_main_dispatches_sync_and_turns_a_stray_ctrl_c_into_130(
    world, isolated_config, capsys
):
    def interrupted(host: str):
        raise KeyboardInterrupt

    code = main(["sync"], deps=sync.Deps(list_apps=interrupted, runner=FakeRunner()))
    assert code == 130
    assert "resume with the same command" in capsys.readouterr().err


def test_main_dispatches_sync_end_to_end(world, isolated_config, tmp_path, monkeypatch, capsys):
    host_publishes(tmp_path, monkeypatch, ["Hades II™"])
    runner = FakeRunner(running=True)
    code = main(["sync"], deps=sync.Deps(runner=runner))
    assert code == 0, capsys.readouterr().err
    assert "Hades II™" in world.owned_names()
    assert runner.shutdowns == 1


def test_sigint_is_deferred_across_the_write_window():
    """Spec 3.9 item 4: everything after the art phase is the Steam relaunch;
    a Ctrl-C there is held until Steam is back up."""
    previous = signal.getsignal(signal.SIGINT)
    with sync.sigint_deferred():
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    assert signal.getsignal(signal.SIGINT) is previous
