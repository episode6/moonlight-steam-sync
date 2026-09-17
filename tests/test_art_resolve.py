"""Title resolution and the match cache (spec 3.5 step A, spec 3.9 item 2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from moonlight_steam_sync.art.http import NetworkError
from moonlight_steam_sync.art.resolve import (
    NEGATIVE_TTL,
    Match,
    MatchCache,
    Resolver,
    normalise,
    pick,
)
from moonlight_steam_sync.art.sgdb import SgdbClient, steam_appid_from_game
from moonlight_steam_sync.art.steamstore import SteamStoreClient
from tests.art_fixtures import FakeTransport, make_fetcher


def build(tmp_path, *, api_key="fixture-key", **resolver_kwargs):
    transport = FakeTransport()
    fetcher = make_fetcher(transport)
    cache = MatchCache(tmp_path / "matches.json")
    resolver = Resolver(
        cache=cache,
        sgdb=SgdbClient(api_key=api_key, fetcher=fetcher),
        store=SteamStoreClient(fetcher=fetcher),
        **resolver_kwargs,
    )
    return resolver, transport, cache


# -- normalisation and pick rules -----------------------------------------


def test_normalise_strips_trademarks_case_punctuation_and_a_trailing_year() -> None:
    assert normalise("Hades II™") == "hades ii"
    assert normalise("ELDEN RING®") == "elden ring"
    assert normalise("Half-Life: Alyx") == "half life alyx"
    assert normalise("Doom (1993)") == "doom"
    assert normalise("  Spaced   Out  ") == "spaced out"
    # A year that is part of the title, not a suffix, survives.
    assert normalise("F1 2019 Season") == "f1 2019 season"


def test_pick_prefers_an_exact_verified_match_over_an_earlier_unverified_one() -> None:
    results = [
        {"id": 1, "name": "Elden Ring", "verified": False},
        {"id": 2, "name": "Elden Ring", "verified": True},
    ]
    chosen, how = pick(results, "Elden Ring")
    assert (chosen["id"], how) == (2, "exact-verified")


def test_pick_falls_back_to_exact_then_prefix_then_nothing() -> None:
    exact, how = pick([{"id": 1, "name": "Fan Made Adventure"}], "fan made adventure")
    assert (exact["id"], how) == (1, "exact")

    fuzzy, how = pick([{"id": 3, "name": "Portal 2: Community Edition"}], "Portal 2")
    assert (fuzzy["id"], how) == (3, "fuzzy")

    nothing, how = pick([{"id": 4, "name": "Something Else"}], "Portal 2")
    assert (nothing, how) == (None, "none")


def test_pick_ignores_verified_for_the_steam_store() -> None:
    chosen, how = pick(
        [{"id": 620, "name": "Portal 2"}], "Portal 2", prefer_verified=False
    )
    assert (chosen["id"], how) == (620, "exact")


def test_steam_appid_from_game_handles_a_missing_steam_release() -> None:
    assert steam_appid_from_game({"external_platform_data": {}}) is None
    assert (
        steam_appid_from_game({"external_platform_data": {"steam": [{"id": "1245620"}]}})
        == 1245620
    )


# -- resolution ------------------------------------------------------------


def test_exact_verified_sgdb_match_carries_the_steam_appid(tmp_path) -> None:
    resolver, transport, _ = build(tmp_path)
    match = resolver.resolve("Elden Ring")
    assert (match.how, match.sgdb_id, match.steam_appid) == ("sgdb:exact-verified", 5297, 1245620)
    assert [c for c in transport.calls if "autocomplete" in c]


def test_a_trademark_glyph_does_not_prevent_an_exact_match(tmp_path) -> None:
    resolver, _, _ = build(tmp_path)
    match = resolver.resolve("Hades II™")
    assert (match.how, match.steam_appid) == ("sgdb:exact-verified", 1145350)


def test_a_non_steam_game_resolves_with_no_steam_appid(tmp_path) -> None:
    resolver, _, _ = build(tmp_path)
    match = resolver.resolve("Fan Made Adventure")
    assert match.sgdb_id == 7001
    assert match.steam_appid is None
    assert match.how == "sgdb:exact"


def test_zero_results_falls_through_to_the_store_and_then_to_no_match(tmp_path) -> None:
    resolver, transport, cache = build(tmp_path)
    match = resolver.resolve("Totally Unknown Title")
    assert match.found is False
    assert match.how == "none"
    assert any("storesearch" in call for call in transport.calls)
    # The negative result is on disk immediately (spec 3.5 A.4).
    stored = json.loads((tmp_path / "matches.json").read_text())
    assert stored["titles"]["Totally Unknown Title"]["how"] == "none"


def test_without_a_key_only_the_steam_store_is_consulted(tmp_path) -> None:
    resolver, transport, _ = build(tmp_path, api_key="")
    resolver.resolve("Totally Unknown Title")
    assert not any("steamgriddb.com" in call for call in transport.calls)
    assert any("storesearch" in call for call in transport.calls)


def test_overrides_pin_a_title_and_skip_art_entirely(tmp_path) -> None:
    resolver, transport, cache = build(
        tmp_path,
        overrides={
            "Weird Launcher Name": {"steam": 1245620},
            "Desktop": {"art": False},
        },
    )
    pinned = resolver.resolve("Weird Launcher Name")
    assert (pinned.how, pinned.steam_appid) == ("override", 1245620)
    assert transport.calls == []

    skipped = resolver.resolve("Desktop")
    assert skipped.skipped is True
    assert "Desktop" not in cache


def test_an_sgdb_pin_still_looks_up_the_steam_release(tmp_path) -> None:
    resolver, transport, _ = build(tmp_path, overrides={"Pinned": {"sgdb": 5297}})
    match = resolver.resolve("Pinned")
    assert (match.sgdb_id, match.steam_appid) == (5297, 1245620)

    # ...once. A pinned title costing one call per run would break "a second
    # pass does only the remaining work" (spec 3.9 item 1).
    before = len(transport.calls)
    again = resolver.resolve("Pinned")
    assert len(transport.calls) == before
    assert again.steam_appid == 1245620


def test_editing_an_sgdb_pin_re_queries_the_steam_release(tmp_path) -> None:
    resolver, transport, _ = build(tmp_path, overrides={"Pinned": {"sgdb": 5297}})
    resolver.resolve("Pinned")
    before = len(transport.calls)
    resolver.overrides = {"Pinned": {"sgdb": 5468}}
    match = resolver.resolve("Pinned")
    assert len(transport.calls) > before
    assert (match.sgdb_id, match.steam_appid) == (5468, 1145350)


# -- the cache -------------------------------------------------------------


def test_a_cached_positive_match_costs_no_http(tmp_path) -> None:
    resolver, transport, _ = build(tmp_path)
    resolver.resolve("Elden Ring")
    before = len(transport.calls)
    again = resolver.resolve("Elden Ring")
    assert len(transport.calls) == before
    assert again.steam_appid == 1245620
    assert again.how.startswith("sgdb:")


def test_force_ignores_the_cache(tmp_path) -> None:
    resolver, transport, _ = build(tmp_path)
    resolver.resolve("Elden Ring")
    before = len(transport.calls)
    resolver.force = True
    resolver.resolve("Elden Ring")
    assert len(transport.calls) > before


def test_a_negative_result_is_not_requeried_for_seven_days(tmp_path) -> None:
    resolver, transport, cache = build(tmp_path)
    resolver.resolve("Totally Unknown Title")
    before = len(transport.calls)
    resolver.resolve("Totally Unknown Title")
    assert len(transport.calls) == before

    # Age it past the window and it is searched again.
    stale = datetime.now(UTC) - NEGATIVE_TTL - timedelta(hours=1)
    entry = cache.get("Totally Unknown Title")
    entry.when = stale.isoformat().replace("+00:00", "Z")
    cache.put(entry)
    resolver.resolve("Totally Unknown Title")
    assert len(transport.calls) > before


def test_retry_missing_reopens_a_fresh_negative(tmp_path) -> None:
    resolver, transport, _ = build(tmp_path)
    resolver.resolve("Totally Unknown Title")
    before = len(transport.calls)
    resolver.retry_missing = True
    resolver.resolve("Totally Unknown Title")
    assert len(transport.calls) > before


def test_the_cache_file_is_replaced_atomically_and_survives_a_reload(tmp_path) -> None:
    path = tmp_path / "matches.json"
    cache = MatchCache(path)
    cache.put(Match(name="A", steam_appid=1, sgdb_id=2, how="sgdb:exact"))
    assert path.is_file()
    assert not (tmp_path / "matches.json.tmp").exists()

    cache.record_missing_slot("A", "logo")
    cache.flush()
    reloaded = MatchCache(path)
    assert reloaded.get("A").steam_appid == 1
    assert reloaded.slot_is_known_missing("A", "logo") is True
    assert reloaded.slot_is_known_missing("A", "hero") is False


def test_a_corrupt_cache_is_a_miss_not_a_crash(tmp_path) -> None:
    path = tmp_path / "matches.json"
    path.write_text("{ this is not json")
    cache = MatchCache(path)
    assert len(cache) == 0


def test_a_stale_slot_miss_is_retried(tmp_path) -> None:
    cache = MatchCache(tmp_path / "matches.json")
    cache.put(Match(name="A", steam_appid=1, how="sgdb:exact"))
    old = datetime.now(UTC) - NEGATIVE_TTL - timedelta(minutes=1)
    cache.record_missing_slot("A", "logo", when=old)
    assert cache.slot_is_known_missing("A", "logo") is False


def test_a_slot_miss_for_an_unresolved_title_is_not_recorded(tmp_path) -> None:
    # The back door onto the negative cache: a title whose own lookup failed
    # has no entry, and inventing one here would start a 7-day window off a
    # SteamGridDB 5xx or a timeout (spec 6.10).
    cache = MatchCache(tmp_path / "matches.json")
    cache.record_missing_slot("Never Resolved", "logo")
    cache.flush()
    assert "Never Resolved" not in cache
    assert not (tmp_path / "matches.json").exists()


def test_a_transient_lookup_failure_is_never_cached_as_a_miss(tmp_path) -> None:
    transport = FakeTransport()
    # Make the SteamGridDB search fail at the transport level, every time.
    url = "https://www.steamgriddb.com/api/v2/search/autocomplete/Elden%20Ring"
    transport.fail_with[url] = NetworkError("down")
    fetcher = make_fetcher(transport)
    cache = MatchCache(tmp_path / "matches.json")
    resolver = Resolver(
        cache=cache,
        sgdb=SgdbClient(api_key="k", fetcher=fetcher),
        store=None,
    )
    match = resolver.resolve("Elden Ring")
    assert match.found is False
    assert match.transient is True
    assert "Elden Ring" not in cache
    assert not (tmp_path / "matches.json").exists()
