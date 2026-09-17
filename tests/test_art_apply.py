"""The artwork phase end to end over the recorded fixtures (spec 3.5, 3.9).

Six titles, chosen to exercise the cases spec PR-4 names; see
``tests/fixtures/art/make_synthetic.py`` for what each one stands for and for
the TODO that says how to replace the synthetic recordings with real ones.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from moonlight_steam_sync.art.apply import (
    ArtTarget,
    apply_title,
    icon_path_for,
    run_art,
    slot_report,
)
from moonlight_steam_sync.art.resolve import MatchCache, Resolver
from moonlight_steam_sync.art.select import Selector
from moonlight_steam_sync.art.sgdb import SgdbClient
from moonlight_steam_sync.art.steamstore import SteamStoreClient
from tests.art_fixtures import FIXTURE_DIR, FakeTransport, make_fetcher

# Shortcut appids: arbitrary here, but in a real run they are
# crc32(Exe + AppName) | 0x80000000 (spec 2.1) and come from the shortcut
# layer through ArtTarget.
APPIDS = {
    "Elden Ring": 2800000001,
    "Hades II™": 2800000002,
    "Fan Made Adventure": 2800000003,
    "Old Console Classic": 2800000004,
    "Totally Unknown Title": 2800000005,
    "Rate Limited Game": 2800000006,
}


@pytest.fixture
def grid_dir(tmp_path: Path) -> Path:
    path = tmp_path / "userdata" / "123456789" / "config" / "grid"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def boxart(tmp_path: Path) -> Path:
    path = tmp_path / "boxart" / "42.png"
    path.parent.mkdir(parents=True)
    path.write_bytes((FIXTURE_DIR / "images" / "tiny.png").read_bytes())
    return path


def targets(grid_dir: Path, boxart: Path | None = None, names=None) -> list[ArtTarget]:
    names = names or list(APPIDS)
    return [
        ArtTarget(
            name=name,
            appid=APPIDS[name],
            grid_dir=grid_dir,
            boxart_path=boxart if name == "Totally Unknown Title" else None,
        )
        for name in names
    ]


class FakeShortcuts:
    """The seam onto the shortcut layer (PR-2 supplies the real one)."""

    def __init__(self, entries: list[ArtTarget]) -> None:
        self._targets = entries
        self.icons: dict[int, Path] = {}
        self.commits = 0

    def targets(self) -> list[ArtTarget]:
        return list(self._targets)

    def set_icon(self, appid: int, icon_path: Path) -> None:
        self.icons[appid] = icon_path

    def commit(self) -> None:
        self.commits += 1


def build(tmp_path: Path, transport: FakeTransport | None = None, **kwargs):
    transport = transport or FakeTransport()
    fetcher = make_fetcher(transport)
    sgdb = SgdbClient(api_key="fixture-key", fetcher=fetcher)
    store = SteamStoreClient(fetcher=fetcher)
    cache = MatchCache(tmp_path / "matches.json")
    resolver = Resolver(cache=cache, sgdb=sgdb, store=store, **kwargs)
    selector = Selector(fetcher=fetcher, sgdb=sgdb, store=store)
    return resolver, selector, transport, cache


def sources(result) -> dict[str, str]:
    return {key: outcome.source for key, outcome in result.slots.items()}


def names_on_disk(grid_dir: Path) -> list[str]:
    return sorted(p.name for p in grid_dir.iterdir())


# -- per-title behaviour ---------------------------------------------------


def test_a_fully_official_title_takes_every_slot_from_the_steam_cdn(tmp_path, grid_dir):
    resolver, selector, transport, _ = build(tmp_path)
    result = apply_title(targets(grid_dir, names=["Elden Ring"])[0], resolver, selector)

    assert sources(result) == dict.fromkeys(
        ["portrait", "landscape", "hero", "logo", "icon"], "official"
    )
    appid = APPIDS["Elden Ring"]
    assert names_on_disk(grid_dir) == [
        f"{appid}.jpg",
        f"{appid}_hero.jpg",
        f"{appid}_icon.jpg",
        f"{appid}_logo.png",
        f"{appid}p.jpg",
    ]
    assert result.slots["logo"].url.endswith("/steam/apps/1245620/logo.png")
    # Not one SteamGridDB *asset* call: the CDN answered every slot.
    assert not any("/grids/game/" in c or "/heroes/game/" in c for c in transport.calls)


def test_a_steam_game_with_no_cdn_logo_falls_through_to_community_art(tmp_path, grid_dir):
    resolver, selector, _, _ = build(tmp_path)
    result = apply_title(
        targets(grid_dir, names=["Old Console Classic"])[0], resolver, selector
    )
    assert sources(result) == {
        "portrait": "official",
        "landscape": "official",
        "hero": "official",
        "logo": "community",
        "icon": "official",
    }
    assert result.slots["logo"].path.name == f"{APPIDS['Old Console Classic']}_logo.png"


def test_a_non_steam_game_is_dressed_entirely_from_community_art(tmp_path, grid_dir):
    resolver, selector, _, _ = build(tmp_path)
    result = apply_title(
        targets(grid_dir, names=["Fan Made Adventure"])[0], resolver, selector
    )
    assert set(sources(result).values()) == {"community"}
    assert result.slots["portrait"].url.endswith("fan-portrait-best.png")
    assert result.slots["logo"].url.endswith("fan-logo-white.png")


def test_an_unmatched_title_keeps_the_host_box_art_and_records_the_rest_as_missing(
    tmp_path, grid_dir, boxart
):
    resolver, selector, _, cache = build(tmp_path)
    result = apply_title(
        targets(grid_dir, boxart, names=["Totally Unknown Title"])[0], resolver, selector
    )
    assert sources(result) == {
        "portrait": "host",
        "landscape": "missing",
        "hero": "missing",
        "logo": "missing",
        "icon": "missing",
    }
    stored = json.loads((tmp_path / "matches.json").read_text())
    missing = stored["titles"]["Totally Unknown Title"]["missing_slots"]
    assert sorted(missing) == ["hero", "icon", "landscape", "logo"]


def test_repeated_429s_stop_the_run_with_everything_so_far_kept(tmp_path, grid_dir):
    resolver, selector, transport, _ = build(tmp_path)
    summary = run_art(
        targets(grid_dir, names=["Elden Ring", "Rate Limited Game"]), resolver, selector
    )
    assert summary.titles == 1  # Elden Ring finished
    assert summary.stopped_early is True
    assert "429" in summary.stop_reason
    assert any(name.startswith(str(APPIDS["Elden Ring"])) for name in names_on_disk(grid_dir))
    # Nothing was written for the title that blew up.
    assert not any(
        name.startswith(str(APPIDS["Rate Limited Game"])) for name in names_on_disk(grid_dir)
    )


# -- the resumability contract --------------------------------------------


def test_the_cache_is_on_disk_after_every_title(tmp_path, grid_dir, boxart):
    resolver, selector, _, _ = build(tmp_path)
    cache_path = tmp_path / "matches.json"
    seen: list[list[str]] = []

    def snapshot(result):
        payload = json.loads(cache_path.read_text())
        seen.append(sorted(payload["titles"]))

    run_art(
        targets(grid_dir, boxart, names=list(APPIDS)[:5]),
        resolver,
        selector,
        on_title=snapshot,
    )
    assert seen == [
        ["Elden Ring"],
        ["Elden Ring", "Hades II™"],
        ["Elden Ring", "Fan Made Adventure", "Hades II™"],
        ["Elden Ring", "Fan Made Adventure", "Hades II™", "Old Console Classic"],
        [
            "Elden Ring",
            "Fan Made Adventure",
            "Hades II™",
            "Old Console Classic",
            "Totally Unknown Title",
        ],
    ]


def test_a_second_pass_over_the_same_titles_makes_zero_http_calls(tmp_path, grid_dir, boxart):
    entries = targets(grid_dir, boxart, names=list(APPIDS)[:5])
    resolver, selector, first_transport, _ = build(tmp_path)
    run_art(entries, resolver, selector)
    assert first_transport.calls

    second_transport = FakeTransport()
    resolver2, selector2, _, _ = build(tmp_path, second_transport)
    summary = run_art(entries, resolver2, selector2)
    assert second_transport.calls == []
    assert summary.missing == 4  # the unmatched title's four empty slots
    kept = [
        outcome.source
        for result in summary.results
        for outcome in result.slots.values()
        if outcome.filled
    ]
    assert set(kept) == {"kept"}


def test_an_existing_slot_file_is_never_redownloaded(tmp_path, grid_dir):
    appid = APPIDS["Elden Ring"]
    handmade = grid_dir / f"{appid}p.png"
    handmade.write_bytes(b"\x89PNG\r\n\x1a\nhand-picked by the user")
    resolver, selector, transport, _ = build(tmp_path)
    result = apply_title(targets(grid_dir, names=["Elden Ring"])[0], resolver, selector)

    assert result.slots["portrait"].source == "kept"
    assert handmade.read_bytes().endswith(b"hand-picked by the user")
    assert not any("library_600x900" in call for call in transport.calls)


def test_force_redownloads_even_an_existing_slot(tmp_path, grid_dir):
    appid = APPIDS["Elden Ring"]
    (grid_dir / f"{appid}p.png").write_bytes(b"\x89PNG\r\n\x1a\nold")
    resolver, selector, transport, _ = build(tmp_path, force=True)
    result = apply_title(
        targets(grid_dir, names=["Elden Ring"])[0], resolver, selector, force=True
    )
    assert result.slots["portrait"].source == "official"
    # The new file is a .jpg from the CDN; the stale .png is left alone, which
    # is why `--force` is documented as "re-fetch", not "clean".
    assert (grid_dir / f"{appid}p.jpg").is_file()


def test_a_cached_slot_miss_is_not_requeried_but_retry_missing_reopens_it(
    tmp_path, grid_dir, boxart
):
    entry = targets(grid_dir, boxart, names=["Totally Unknown Title"])[0]
    resolver, selector, transport, _ = build(tmp_path)
    apply_title(entry, resolver, selector)
    first = len(transport.calls)

    apply_title(entry, resolver, selector)
    assert len(transport.calls) == first

    resolver.retry_missing = True
    apply_title(entry, resolver, selector)
    assert len(transport.calls) > first


def test_an_interrupted_download_leaves_a_part_and_the_rerun_completes(tmp_path, grid_dir):
    url = "https://cdn.cloudflare.steamstatic.com/steam/apps/1245620/library_600x900_2x.jpg"
    resolver, selector, _, _ = build(tmp_path, FakeTransport(truncate_after={url: 1}))
    entry = targets(grid_dir, names=["Elden Ring"])[0]
    with pytest.raises(ConnectionResetError):
        apply_title(entry, resolver, selector)
    assert names_on_disk(grid_dir) == [f"{APPIDS['Elden Ring']}p.part"]

    resolver2, selector2, _, _ = build(tmp_path)
    result = apply_title(entry, resolver2, selector2)
    assert result.slots["portrait"].source == "official"
    assert f"{APPIDS['Elden Ring']}p.jpg" in names_on_disk(grid_dir)
    assert f"{APPIDS['Elden Ring']}p.part" not in names_on_disk(grid_dir)


def test_webp_is_never_requested_or_written_anywhere_in_a_full_run(
    tmp_path, grid_dir, boxart
):
    resolver, selector, transport, _ = build(tmp_path)
    run_art(targets(grid_dir, boxart, names=list(APPIDS)[:5]), resolver, selector)
    assert not any("webp" in call.lower() for call in transport.calls)
    assert not any(path.suffix == ".webp" for path in grid_dir.iterdir())
    assert not any(path.suffix == ".part" for path in grid_dir.iterdir())


# -- reporting and the shortcut seam --------------------------------------


def test_progress_lines_and_the_summary_name_the_unmatched_titles(
    tmp_path, grid_dir, boxart, capsys
):
    resolver, selector, _, _ = build(tmp_path)
    summary = run_art(
        targets(grid_dir, boxart, names=["Elden Ring", "Totally Unknown Title"]),
        resolver,
        selector,
        out=sys.stdout,
    )
    printed = capsys.readouterr().out
    assert "[1/2] Elden Ring: portrait=official" in printed
    assert "[2/2] Totally Unknown Title: portrait=host" in printed
    assert summary.unmatched == ["Totally Unknown Title"]
    assert any("no match for 1 title" in line for line in summary.lines())


def test_explain_prints_the_match_chain(tmp_path, grid_dir, capsys):
    resolver, selector, _, _ = build(tmp_path)
    run_art(
        targets(grid_dir, names=["Elden Ring"]),
        resolver,
        selector,
        explain=True,
        out=sys.stdout,
    )
    printed = capsys.readouterr().out
    assert "sgdb autocomplete" in printed
    assert "platformdata=steam" in printed
    assert "library_600x900_2x.jpg" in printed


def test_the_icon_path_is_handed_back_to_the_shortcut_layer(tmp_path, grid_dir):
    entries = targets(grid_dir, names=["Elden Ring"])
    provider = FakeShortcuts(entries)
    resolver, selector, _, _ = build(tmp_path)
    run_art(entries, resolver, selector, provider=provider)

    appid = APPIDS["Elden Ring"]
    assert provider.icons[appid] == (grid_dir / f"{appid}_icon.jpg").resolve()
    assert provider.icons[appid].is_absolute()
    assert provider.commits == 1


def test_an_override_can_skip_a_title_entirely(tmp_path, grid_dir):
    resolver, selector, transport, _ = build(tmp_path, overrides={"Elden Ring": {"art": False}})
    result = apply_title(targets(grid_dir, names=["Elden Ring"])[0], resolver, selector)
    assert result.skipped is True
    assert result.progress_line(1, 1).endswith("skipped (overrides)")
    assert transport.calls == []
    assert names_on_disk(grid_dir) == []


def test_icon_path_for_picks_up_a_slot_that_was_already_on_disk(tmp_path, grid_dir):
    appid = APPIDS["Fan Made Adventure"]
    (grid_dir / f"{appid}_icon.png").write_bytes(b"\x89PNG\r\n\x1a\nx")
    entry = targets(grid_dir, names=["Fan Made Adventure"])[0]
    resolver, selector, _, _ = build(tmp_path)
    result = apply_title(entry, resolver, selector)
    assert result.slots["icon"].source == "kept"
    assert icon_path_for(entry, result) == (grid_dir / f"{appid}_icon.png").resolve()


def test_slot_report_reads_the_extensions_off_disk(tmp_path, grid_dir):
    appid = 4242
    (grid_dir / f"{appid}p.jpg").write_bytes(b"x")
    (grid_dir / f"{appid}_logo.png").write_bytes(b"x")
    assert slot_report(grid_dir, appid) == {
        "portrait": "jpg",
        "landscape": "-",
        "hero": "-",
        "logo": "png",
        "icon": "-",
    }
