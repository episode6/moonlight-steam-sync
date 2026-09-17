"""Tests for moonlight.py (spec 2.1, 2.3, 3.4): binary discovery, `list
--csv` parsing, and `stream`'s exec.

TODO (fixtures): ``tests/fixtures/moonlight_list_sample.csv`` is a synthetic
capture, hand-built from the documented CSV shape, not a real one -- see
``tests/fixtures/README.md`` for exactly what to swap in and how to capture
it once real hosts are available. Every test in this file is written against
that fixture's *shape* (header names, one cached/one uncached boxart row, one
hidden row, one collection row), so replacing the fixture file should not
require rewriting these tests, only their row-count/name assertions if the
real capture's contents differ.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from moonlight_steam_sync import moonlight

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_CSV = (FIXTURES_DIR / "moonlight_list_sample.csv").read_text()


def _write_fake_moonlight(bin_dir: Path, *, script_body: str) -> None:
    script = bin_dir / "moonlight"
    script.write_text(f"#!/bin/sh\n{script_body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# --- find_binary -------------------------------------------------------


def test_find_binary_prefers_moonlight_bin_override(monkeypatch):
    monkeypatch.setenv("MOONLIGHT_BIN", "/custom/path/moonlight --flag")
    assert moonlight.find_binary() == ["/custom/path/moonlight", "--flag"]


def test_find_binary_finds_native_on_path(tmp_path, monkeypatch):
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    _write_fake_moonlight(tmp_path, script_body="exit 0")
    monkeypatch.setenv("PATH", str(tmp_path))

    result = moonlight.find_binary()
    assert result == [str(tmp_path / "moonlight")]


def test_find_binary_falls_back_to_flatpak(tmp_path, monkeypatch):
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    # No native `moonlight` on PATH, but a `flatpak` that lists the app.
    flatpak = tmp_path / "flatpak"
    flatpak.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "list" ]; then echo com.moonlight_stream.Moonlight; fi\n'
    )
    flatpak.chmod(flatpak.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(tmp_path))

    result = moonlight.find_binary()
    assert result == [str(flatpak), "run", "com.moonlight_stream.Moonlight"]


def test_find_binary_returns_none_when_nothing_available(tmp_path, monkeypatch):
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir, nothing on PATH
    assert moonlight.find_binary() is None


# --- list_apps / CSV parsing --------------------------------------------


def test_list_apps_parses_and_filters_sample_csv(tmp_path, monkeypatch):
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    _write_fake_moonlight(tmp_path, script_body=f"cat <<'EOF'\n{SAMPLE_CSV}EOF\n")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    apps = moonlight.list_apps("MY-GAMING-PC")

    names = [app.name for app in apps]
    # "Hidden Game" (Hidden=True) and "My Collection" (App Collection
    # Game=True) are dropped entirely; everything else survives.
    assert names == ["Elden Ring", "Desktop", "Steam Big Picture", "Some Weird Launcher Name"]
    assert all(not app.hidden for app in apps)

    elden_ring = apps[0]
    assert elden_ring.id == "1"
    # The real Boxart URL column is percent-encoded (QUrl::toDisplayString())
    # and its cache path always contains spaces; this must come back decoded.
    assert elden_ring.boxart_path == (
        "/home/deck/.var/app/com.moonlight_stream.Moonlight/cache/"
        "Moonlight Game Streaming Project/Moonlight/boxart/abc-host-uuid/1.png"
    )

    desktop = apps[1]
    assert desktop.boxart_path is None  # qrc:/res/no_app_image.png -> not cached


def test_list_apps_builds_expected_argv(tmp_path, monkeypatch):
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    captured = tmp_path / "argv.txt"
    _write_fake_moonlight(
        tmp_path,
        script_body=f'echo "$@" > {captured}\ncat <<\'EOF\'\n{SAMPLE_CSV}EOF\n',
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    moonlight.list_apps("MY-GAMING-PC")

    assert captured.read_text().split() == ["list", "MY-GAMING-PC", "--csv"]


def test_list_apps_raises_format_error_on_unexpected_header(tmp_path, monkeypatch):
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    _write_fake_moonlight(
        tmp_path, script_body="printf 'Name,ID\\nElden Ring,1\\n'\n"
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(moonlight.MoonlightCsvFormatError, match="Hidden"):
        moonlight.list_apps("MY-GAMING-PC")


def test_list_apps_raises_not_found_without_a_binary(tmp_path, monkeypatch):
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))

    with pytest.raises(moonlight.MoonlightNotFoundError):
        moonlight.list_apps("MY-GAMING-PC")


def test_list_apps_raises_unreachable_on_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    _write_fake_moonlight(
        tmp_path, script_body="echo 'connection refused' >&2\nexit 1\n"
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(moonlight.MoonlightUnreachableError, match="connection refused"):
        moonlight.list_apps("UNREACHABLE-HOST")


def test_list_apps_raises_unreachable_on_timeout(tmp_path, monkeypatch):
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    _write_fake_moonlight(tmp_path, script_body="sleep 5\n")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    with pytest.raises(moonlight.MoonlightUnreachableError, match="timed out"):
        moonlight.list_apps("SLOW-HOST", timeout=0.2)


@pytest.mark.parametrize(
    "value,expected",
    [("True", True), ("true", True), ("1", True), ("yes", True), ("False", False), ("", False)],
)
def test_parse_bool_spellings(value, expected):
    assert moonlight._parse_bool(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("file:///a/b/1.png", "/a/b/1.png"),
        # QUrl::toDisplayString() percent-encodes spaces (and other
        # reserved characters) in the path; these must come back decoded.
        (
            "file:///home/deck/.var/app/com.moonlight_stream.Moonlight/cache/"
            "Moonlight%20Game%20Streaming%20Project/Moonlight/boxart/"
            "abc-host-uuid/1.png",
            "/home/deck/.var/app/com.moonlight_stream.Moonlight/cache/"
            "Moonlight Game Streaming Project/Moonlight/boxart/"
            "abc-host-uuid/1.png",
        ),
        ("qrc:/res/no_app_image.png", None),
        ("", None),
    ],
)
def test_parse_boxart(value, expected):
    assert moonlight._parse_boxart(value) == expected


# --- stream --------------------------------------------------------------


def test_stream_execs_with_expected_argv(monkeypatch):
    monkeypatch.setattr(moonlight, "find_binary", lambda: ["/usr/bin/moonlight"])
    captured = {}

    def fake_execvp(file, args):
        captured["file"] = file
        captured["args"] = args

    monkeypatch.setattr(os, "execvp", fake_execvp)

    moonlight.stream("MY-GAMING-PC", "Elden Ring", ["--fps", "60"])

    assert captured["file"] == "/usr/bin/moonlight"
    assert captured["args"] == [
        "/usr/bin/moonlight",
        "stream",
        "MY-GAMING-PC",
        "Elden Ring",
        "--fps",
        "60",
    ]


def test_stream_execs_via_flatpak_argv_prefix(monkeypatch):
    monkeypatch.setattr(
        moonlight, "find_binary", lambda: ["flatpak", "run", "com.moonlight_stream.Moonlight"]
    )
    captured = {}
    monkeypatch.setattr(
        os, "execvp", lambda file, args: captured.update(file=file, args=args)
    )

    moonlight.stream("MY-GAMING-PC", "Elden Ring")

    assert captured["file"] == "flatpak"
    assert captured["args"] == [
        "flatpak",
        "run",
        "com.moonlight_stream.Moonlight",
        "stream",
        "MY-GAMING-PC",
        "Elden Ring",
    ]


def test_stream_raises_not_found_without_a_binary(monkeypatch):
    monkeypatch.setattr(moonlight, "find_binary", lambda: None)
    with pytest.raises(moonlight.MoonlightNotFoundError):
        moonlight.stream("MY-GAMING-PC", "Elden Ring")
