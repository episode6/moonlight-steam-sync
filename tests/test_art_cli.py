"""The ``art`` and ``status`` subcommands (spec 3.3)."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from moonlight_steam_sync import config as config_module
from moonlight_steam_sync.__main__ import build_parser, main
from moonlight_steam_sync.art.apply import ArtTarget
from moonlight_steam_sync.art.cli import ArtServices, build_services, cmd_art, cmd_status
from moonlight_steam_sync.art.resolve import MatchCache, Resolver
from moonlight_steam_sync.art.select import Selector
from moonlight_steam_sync.art.sgdb import SgdbClient
from moonlight_steam_sync.art.steamstore import SteamStoreClient
from moonlight_steam_sync.config import Config
from tests.art_fixtures import FIXTURE_DIR, FakeTransport, make_fetcher

APPIDS = {"Elden Ring": 2800000001, "Fan Made Adventure": 2800000003}


class FakeShortcuts:
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


@pytest.fixture
def grid_dir(tmp_path: Path) -> Path:
    path = tmp_path / "grid"
    path.mkdir()
    return path


def entries(grid_dir: Path) -> list[ArtTarget]:
    return [
        ArtTarget(name=name, appid=appid, grid_dir=grid_dir)
        for name, appid in APPIDS.items()
    ]


def services(tmp_path: Path, transport: FakeTransport, **kwargs) -> ArtServices:
    fetcher = make_fetcher(transport)
    sgdb = SgdbClient(api_key="fixture-key", fetcher=fetcher)
    store = SteamStoreClient(fetcher=fetcher)
    cache = MatchCache(tmp_path / "matches.json")
    return ArtServices(
        fetcher=fetcher,
        sgdb=sgdb,
        store=store,
        cache=cache,
        resolver=Resolver(cache=cache, sgdb=sgdb, store=store, **kwargs),
        selector=Selector(fetcher=fetcher, sgdb=sgdb, store=store),
    )


def args_for(argv: list[str]):
    return build_parser().parse_args(argv)


def run_art_cmd(tmp_path, grid_dir, argv, *, transport=None, provider=None, config=None):
    transport = transport or FakeTransport()
    provider = provider or FakeShortcuts(entries(grid_dir))
    config = config or Config(sgdb_api_key="fixture-key")
    out, err = io.StringIO(), io.StringIO()
    code = cmd_art(
        args_for(argv),
        config,
        provider_factory=lambda _cfg: provider,
        services=services(tmp_path, transport),
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue(), transport, provider


def test_art_dresses_every_owned_shortcut_and_exits_zero(tmp_path, grid_dir):
    code, out, _, _, provider = run_art_cmd(tmp_path, grid_dir, ["art"])
    assert code == 0
    assert "[1/2] Elden Ring: portrait=official" in out
    assert "[2/2] Fan Made Adventure: portrait=community" in out
    assert "art slot(s) filled" in out
    assert provider.commits == 1
    assert sorted(p.suffix for p in grid_dir.iterdir()) == [".jpg"] * 5 + [".png"] * 5


def test_art_only_limits_the_run_to_one_title(tmp_path, grid_dir):
    code, out, _, _, _ = run_art_cmd(tmp_path, grid_dir, ["art", "--only", "Elden Ring"])
    assert code == 0
    assert "[1/1] Elden Ring" in out
    assert "Fan Made Adventure" not in out


def test_art_only_with_an_unknown_name_is_a_usage_error(tmp_path, grid_dir):
    code, _, err, _, _ = run_art_cmd(tmp_path, grid_dir, ["art", "--only", "Nope"])
    assert code == 1
    assert "no owned shortcut named" in err


def test_art_explain_prints_the_chain(tmp_path, grid_dir):
    code, out, _, _, _ = run_art_cmd(
        tmp_path, grid_dir, ["art", "--only", "Elden Ring", "--explain"]
    )
    assert code == 0
    assert "sgdb autocomplete" in out
    assert "try official:" in out


def test_art_reports_the_rate_limit_hard_stop_with_exit_code_four(tmp_path, grid_dir):
    provider = FakeShortcuts(
        [ArtTarget(name="Rate Limited Game", appid=2800000006, grid_dir=grid_dir)]
    )
    code, out, _, _, _ = run_art_cmd(tmp_path, grid_dir, ["art"], provider=provider)
    assert code == 4
    assert "429" in out
    assert "rerun the same command to continue" in out


def test_a_dropped_connection_mid_download_never_escapes_as_a_traceback(tmp_path, grid_dir):
    # A body that dies halfway is the likeliest failure on a Deck over Wi-Fi.
    # It must cost the slot, not the run: no bare OSError out of cmd_art, and
    # no exit 1 (which spec 3.3 reserves for usage and config errors).
    url = "https://cdn.cloudflare.steamstatic.com/steam/apps/1245620/library_600x900_2x.jpg"
    code, out, _, _, provider = run_art_cmd(
        tmp_path,
        grid_dir,
        ["art", "--only", "Elden Ring"],
        transport=FakeTransport(truncate_after={url: 1}),
    )
    assert code == 0
    assert "[1/1] Elden Ring: portrait=missing" in out
    # Everything else about that title still landed, including the icon patch.
    assert "hero=official" in out
    assert provider.icons and provider.commits == 1


def test_a_ctrl_c_exits_130_and_says_how_to_resume(tmp_path, grid_dir):
    url = "https://www.steamgriddb.com/api/v2/search/autocomplete/Elden%20Ring"
    transport = FakeTransport(fail_with={url: KeyboardInterrupt()})
    code, out, err, _, _ = run_art_cmd(
        tmp_path, grid_dir, ["art", "--only", "Elden Ring"], transport=transport
    )
    assert code == 130
    assert "everything written so far is kept" in out
    # Spec 3.9 item 5 asks for this wording on SIGINT.
    assert "resume with the same command" in err


def test_art_without_a_key_says_so_and_still_runs(tmp_path, grid_dir):
    code, _, err, transport, _ = run_art_cmd(
        tmp_path, grid_dir, ["art"], config=Config(sgdb_api_key="")
    )
    assert code == 0
    assert "no SteamGridDB API key" in err


def test_art_without_a_steam_install_fails_cleanly(tmp_path, grid_dir, monkeypatch):
    """The default provider is the real one now; no Steam tree means exit 1."""
    monkeypatch.delenv("STEAM_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nowhere"))
    out, err = io.StringIO(), io.StringIO()
    code = cmd_art(args_for(["art"]), Config(), out=out, err=err)
    assert code == 1
    assert "no Steam installation" in err.getvalue()


def test_status_lists_the_slots_each_shortcut_has(tmp_path, grid_dir):
    run_art_cmd(tmp_path, grid_dir, ["art", "--only", "Elden Ring"])
    out, err = io.StringIO(), io.StringIO()
    provider = FakeShortcuts(entries(grid_dir))
    code = cmd_status(
        args_for(["status"]), Config(), provider_factory=lambda _cfg: provider, out=out, err=err
    )
    assert code == 0
    printed = out.getvalue()
    assert f"Elden Ring [{APPIDS['Elden Ring']}]: portrait=jpg" in printed
    assert f"Fan Made Adventure [{APPIDS['Fan Made Adventure']}]: portrait=-" in printed
    assert "2 shortcut(s), 1 with every slot filled" in printed


def test_status_without_a_steam_install_fails_cleanly(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("STEAM_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "nowhere"))
    out, err = io.StringIO(), io.StringIO()
    code = cmd_status(args_for(["status"]), Config(), out=out, err=err)
    assert code == 1
    assert "no Steam installation" in err.getvalue()


def test_main_dispatches_art_and_status_through_the_injected_provider(
    tmp_path, grid_dir, capsys, monkeypatch
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("SGDB_API_KEY", "fixture-key")
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config_module, "DEFAULT_KEY_FILE", tmp_path / "sgdb-api-key")
    monkeypatch.setattr(
        "moonlight_steam_sync.art.cli.Fetcher", lambda **kwargs: make_fetcher(FakeTransport())
    )
    provider = FakeShortcuts(entries(grid_dir))
    code = main(["art", "--only", "Elden Ring"], provider_factory=lambda _cfg: provider)
    assert code == 0
    assert "[1/1] Elden Ring" in capsys.readouterr().out

    code = main(["status"], provider_factory=lambda _cfg: provider)
    assert code == 0
    assert "portrait=jpg" in capsys.readouterr().out
    # The cache landed under XDG_CACHE_HOME, not in the user's real home.
    assert (tmp_path / "cache" / "moonlight-steam-sync" / "matches.json").is_file()


def test_build_services_uses_the_configured_pacing(tmp_path) -> None:
    built = build_services(
        Config(request_interval_ms=1234, sgdb_api_key="k"), cache_path=tmp_path / "m.json"
    )
    assert built.fetcher.interval_ms == 1234
    assert built.sgdb.enabled is True
    assert FIXTURE_DIR.is_dir()
