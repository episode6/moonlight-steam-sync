"""Slot filenames, source order, the magic-byte sniff and the .part dance (spec 3.5/3.9)."""

from __future__ import annotations

import pytest

from moonlight_steam_sync.art.http import NetworkError
from moonlight_steam_sync.art.resolve import Match
from moonlight_steam_sync.art.select import (
    HERO,
    ICON,
    LANDSCAPE,
    LOGO,
    PORTRAIT,
    SLOTS,
    Selector,
    existing_slot_file,
    slot_basename,
    sniff_extension,
)
from moonlight_steam_sync.art.sgdb import SgdbClient
from moonlight_steam_sync.art.steamstore import SteamStoreClient
from tests.art_fixtures import FIXTURE_DIR, FakeTransport, make_fetcher

APPID = 2864321987

PORTRAIT_2X = "https://cdn.cloudflare.steamstatic.com/steam/apps/1245620/library_600x900_2x.jpg"


def make_selector(transport: FakeTransport | None = None, **kwargs):
    transport = transport or FakeTransport()
    fetcher = make_fetcher(transport)
    selector = Selector(
        fetcher=fetcher,
        sgdb=SgdbClient(api_key="fixture-key", fetcher=fetcher),
        store=SteamStoreClient(fetcher=fetcher),
        **kwargs,
    )
    return selector, transport


def test_slot_filenames_match_what_steam_reads() -> None:
    assert slot_basename(APPID, PORTRAIT) == f"{APPID}p"
    assert slot_basename(APPID, LANDSCAPE) == f"{APPID}"
    assert slot_basename(APPID, HERO) == f"{APPID}_hero"
    assert slot_basename(APPID, LOGO) == f"{APPID}_logo"
    assert slot_basename(APPID, ICON) == f"{APPID}_icon"
    assert [slot.key for slot in SLOTS] == ["portrait", "landscape", "hero", "logo", "icon"]


def test_sniff_only_accepts_png_and_jpeg() -> None:
    assert sniff_extension(b"\x89PNG\r\n\x1a\n....") == ".png"
    assert sniff_extension(b"\xff\xd8\xff\xe0") == ".jpg"
    assert sniff_extension((FIXTURE_DIR / "images" / "tiny.webp").read_bytes()) is None
    assert sniff_extension(b"<html>") is None
    assert sniff_extension(b"") is None


def test_existing_slot_file_finds_any_honoured_extension_and_ignores_parts(tmp_path) -> None:
    (tmp_path / f"{APPID}p.jpg").write_bytes(b"x")
    (tmp_path / f"{APPID}_hero.part").write_bytes(b"x")
    (tmp_path / f"{APPID}.json").write_text("{}")
    assert existing_slot_file(tmp_path, APPID, PORTRAIT).name == f"{APPID}p.jpg"
    assert existing_slot_file(tmp_path, APPID, HERO) is None
    # The optional logo-position JSON is not artwork and must not count as the
    # landscape slot.
    assert existing_slot_file(tmp_path, APPID, LANDSCAPE) is None


def test_the_landscape_slot_is_not_satisfied_by_the_portrait_file(tmp_path) -> None:
    (tmp_path / f"{APPID}p.png").write_bytes(b"x")
    assert existing_slot_file(tmp_path, APPID, LANDSCAPE) is None


def test_official_sources_are_tried_in_cdn_order(tmp_path) -> None:
    selector, transport = make_selector()
    match = Match(name="Elden Ring", steam_appid=1245620, sgdb_id=5297)
    fill = selector.fill(PORTRAIT, match, appid=APPID, grid_dir=tmp_path)
    assert fill.source == "official"
    assert fill.url.endswith("library_600x900_2x.jpg")
    assert fill.path.name == f"{APPID}p.jpg"


def test_a_404_on_the_2x_portrait_falls_through_to_the_1x(tmp_path) -> None:
    selector, transport = make_selector()
    match = Match(name="Hades II", steam_appid=1145350, sgdb_id=5468)
    fill = selector.fill(PORTRAIT, match, appid=APPID, grid_dir=tmp_path)
    assert fill.url.endswith("library_600x900.jpg")
    assert [c for c in transport.calls if "library_600x900" in c] == [
        "https://cdn.cloudflare.steamstatic.com/steam/apps/1145350/library_600x900_2x.jpg",
        "https://cdn.cloudflare.steamstatic.com/steam/apps/1145350/library_600x900.jpg",
    ]


def test_community_assets_are_sorted_by_score_before_the_first_is_taken(tmp_path) -> None:
    selector, _ = make_selector()
    match = Match(name="Fan Made Adventure", sgdb_id=7001)
    fill = selector.fill(PORTRAIT, match, appid=APPID, grid_dir=tmp_path)
    assert fill.source == "community"
    # The fixture lists the score-42 asset first; the score-97 one must win.
    assert fill.url.endswith("fan-portrait-best.png")


def test_community_fallback_can_be_switched_off(tmp_path) -> None:
    selector, transport = make_selector(community_fallback=False)
    match = Match(name="Fan Made Adventure", sgdb_id=7001)
    assert selector.fill(PORTRAIT, match, appid=APPID, grid_dir=tmp_path) is None
    assert transport.calls == []


def test_bytes_that_are_not_png_or_jpeg_are_rejected_and_the_next_source_wins(
    tmp_path,
) -> None:
    selector, _ = make_selector()
    match = Match(name="Fan Made Adventure", sgdb_id=7001)
    fill = selector.fill(LOGO, match, appid=APPID, grid_dir=tmp_path)
    # The `official` logo is a .png URL serving WebP bytes; it must be dropped
    # and the `white` style used instead.
    assert fill.url.endswith("fan-logo-white.png")
    assert not (tmp_path / f"{APPID}_logo.part").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{APPID}_logo.png"]


def test_webp_is_never_requested(tmp_path) -> None:
    selector, transport = make_selector()
    match = Match(name="Fan Made Adventure", sgdb_id=7001)
    for slot in SLOTS:
        selector.fill(slot, match, appid=APPID, grid_dir=tmp_path)
    assert not any("webp" in call.lower() for call in transport.calls)
    assert not any(path.suffix == ".webp" for path in tmp_path.iterdir())


def test_requesting_webp_from_the_client_is_a_programming_error() -> None:
    selector, _ = make_selector()
    with pytest.raises(ValueError, match="WebP"):
        selector.sgdb.assets("grids", 7001, mimes="image/webp")


def test_an_exception_mid_download_leaves_only_a_part_file(tmp_path) -> None:
    selector, _ = make_selector(FakeTransport(truncate_after={PORTRAIT_2X: 1}))
    # A connection that dies mid-body is a structured NetworkError, not the
    # bare OSError that used to walk past every handler in the stack.
    with pytest.raises(NetworkError):
        selector.download(PORTRAIT_2X, tmp_path / f"{APPID}p")
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{APPID}p.part"]


def test_a_mid_download_drop_is_a_failed_attempt_not_a_crash(tmp_path) -> None:
    selector, _ = make_selector(FakeTransport(truncate_after={PORTRAIT_2X: 1}))
    match = Match(name="Elden Ring", steam_appid=1245620)
    # fill() survives it, flags the attempt as failed (so the slot is never
    # cached as a genuine miss) and falls through to the next source.
    assert selector.fill(PORTRAIT, match, appid=APPID, grid_dir=tmp_path) is None
    assert selector.last_attempt_failed is True
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{APPID}p.part"]


def test_force_replaces_a_slot_file_that_has_a_different_extension(tmp_path) -> None:
    stale = tmp_path / f"{APPID}p.png"
    stale.write_bytes(b"\x89PNG\r\n\x1a\nold")
    selector, _ = make_selector()
    match = Match(name="Elden Ring", steam_appid=1245620)
    fill = selector.fill(PORTRAIT, match, appid=APPID, grid_dir=tmp_path)
    assert fill.path.name == f"{APPID}p.jpg"
    # One file per slot: the old .png is gone, so Steam has no choice to make
    # and the next run cannot report the stale file as "kept".
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{APPID}p.jpg"]


def test_a_stale_part_is_overwritten_by_the_next_run(tmp_path) -> None:
    (tmp_path / f"{APPID}p.part").write_bytes(b"garbage from a killed run")
    selector, _ = make_selector()
    match = Match(name="Elden Ring", steam_appid=1245620)
    fill = selector.fill(PORTRAIT, match, appid=APPID, grid_dir=tmp_path)
    assert fill.path.name == f"{APPID}p.jpg"
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"{APPID}p.jpg"]


def test_host_box_art_is_the_last_resort_for_the_portrait_only(tmp_path) -> None:
    boxart = tmp_path / "boxart.png"
    boxart.write_bytes((FIXTURE_DIR / "images" / "tiny.png").read_bytes())
    selector, transport = make_selector()
    match = Match(name="Totally Unknown Title")
    fill = selector.fill(
        PORTRAIT, match, appid=APPID, grid_dir=tmp_path, boxart_path=boxart
    )
    assert fill.source == "host"
    assert fill.path.name == f"{APPID}p.png"
    assert transport.calls == []
    assert selector.fill(HERO, match, appid=APPID, grid_dir=tmp_path, boxart_path=boxart) is None


def test_a_host_box_art_placeholder_that_is_not_an_image_is_rejected(tmp_path) -> None:
    boxart = tmp_path / "no_app_image.txt"
    boxart.write_text("not an image")
    selector, _ = make_selector()
    fill = selector.fill(
        PORTRAIT,
        Match(name="Totally Unknown Title"),
        appid=APPID,
        grid_dir=tmp_path,
        boxart_path=boxart,
    )
    assert fill is None
    assert list(tmp_path.glob("*.part")) == []


def test_the_icon_slot_needs_the_getapps_hash(tmp_path) -> None:
    selector, transport = make_selector()
    match = Match(name="Elden Ring", steam_appid=1245620)
    fill = selector.fill(ICON, match, appid=APPID, grid_dir=tmp_path)
    assert fill.source == "official"
    assert fill.url.endswith("/1c1c1c1c1c1c1c1c1c1c.jpg")
    assert any("GetApps" in call for call in transport.calls)


def test_landscape_falls_from_920x430_to_460x215(tmp_path) -> None:
    selector, transport = make_selector()
    match = Match(name="Fan Made Adventure", sgdb_id=7001)
    fill = selector.fill(LANDSCAPE, match, appid=APPID, grid_dir=tmp_path)
    assert fill.url.endswith("fan-landscape.png")
    queried = [c for c in transport.calls if "/grids/game/7001" in c]
    assert "dimensions=920x430" in queried[0]
    assert "dimensions=460x215" in queried[1]
