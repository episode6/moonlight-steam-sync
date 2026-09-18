"""Tests for moonlight.py (spec 2.1, 2.3, 3.4): binary discovery, `list`
output parsing, and `stream`'s exec.

TODO (fixtures): ``tests/fixtures/moonlight_list_sample.txt`` is a synthetic
capture, hand-built from the documented plain-list shape (one name per
line), not a real one -- see ``tests/fixtures/README.md`` for exactly what to
swap in and how to capture it once real hosts are available. Every test in
this file is written against that fixture's *shape*, so replacing the
fixture file should not require rewriting these tests, only their name
assertions if the real capture's contents differ.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from moonlight_steam_sync import moonlight

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_LIST = (FIXTURES_DIR / "moonlight_list_sample.txt").read_text()


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


# --- list_apps / plain list parsing ---------------------------------------


def test_list_apps_parses_the_sample_list(tmp_path, monkeypatch):
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    _write_fake_moonlight(tmp_path, script_body=f"cat <<'EOF'\n{SAMPLE_LIST}EOF\n")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    apps = moonlight.list_apps("MY-GAMING-PC")

    assert [app.name for app in apps] == [
        "Elden Ring",
        "Desktop",
        "Steam Big Picture",
        "Some Weird Launcher Name",
    ]


def test_list_apps_builds_the_plain_argv_never_csv(tmp_path, monkeypatch):
    """The plain form on purpose: ``--csv`` makes moonlight fetch box art for
    every title before printing, a burst that has crashed an Apollo host."""
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    captured = tmp_path / "argv.txt"
    _write_fake_moonlight(
        tmp_path,
        script_body=f'echo "$@" > {captured}\ncat <<\'EOF\'\n{SAMPLE_LIST}EOF\n',
    )
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    moonlight.list_apps("MY-GAMING-PC")

    assert captured.read_text().split() == ["list", "MY-GAMING-PC"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", []),
        ("\n\n", []),
        ("Elden Ring\n", ["Elden Ring"]),
        # A name is kept verbatim, spaces and punctuation included: it is
        # what `moonlight stream` is later handed.
        ("Hades II\u2122\nSome: Weird, \"Name\"\n", ["Hades II\u2122", 'Some: Weird, "Name"']),
        # Blank lines are skipped, a trailing newline is not a title.
        ("A\n\nB", ["A", "B"]),
    ],
)
def test_parse_list(text, expected):
    assert [app.name for app in moonlight._parse_list(text)] == expected


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
