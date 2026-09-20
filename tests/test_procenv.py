"""A PyInstaller-frozen parent's ``LD_LIBRARY_PATH`` (Decky's plugin_loader)
must not reach ``flatpak`` or this process's own ``libssl``.

The end-to-end tests run the real entry point in a child process under a
stripped environment that mimics the service: ``PATH`` holds only a fake
``flatpak`` that fails exactly the way the real one did on the device
(exit 1, a dynamic-linker error on stderr) whenever a ``_MEI`` directory is
on ``LD_LIBRARY_PATH``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from moonlight_steam_sync import moonlight, procenv

SRC = Path(__file__).resolve().parents[1] / "src"
LINKER_ERROR = (
    "flatpak: /tmp/_MEIabc123/libcrypto.so.3: version OPENSSL_3.4.0 not found "
    "(required by /usr/lib/libostree-1.so.1)"
)


def _install_fake_flatpak(bin_dir: Path) -> None:
    bin_dir.mkdir()
    script = bin_dir / "flatpak"
    script.write_text(
        "#!/bin/sh\n"
        'case "$LD_LIBRARY_PATH" in\n'
        f'  *_MEI*) echo "{LINKER_ERROR}" >&2; exit 1 ;;\n'
        "esac\n"
        'echo "LD_LIBRARY_PATH_ORIG=${LD_LIBRARY_PATH_ORIG-unset}" >&2\n'
        "echo com.moonlight_stream.Moonlight\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def _service_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """What a systemd service hands a child: no session, a bare PATH (here
    only the fake flatpak), plus whatever the frozen parent exported."""
    bin_dir = tmp_path / "bin"
    _install_fake_flatpak(bin_dir)
    home = tmp_path / "home"
    home.mkdir()
    return {
        "HOME": str(home),
        "PATH": str(bin_dir),
        "PYTHONPATH": str(SRC),
        "STEAM_ROOT": str(tmp_path / "no-steam"),
        **extra,
    }


def _doctor(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "moonlight_steam_sync", "doctor"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _moonlight_line(result: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in result.stdout.splitlines() if line.startswith("moonlight:")]
    assert lines, result.stdout + result.stderr
    return lines[0]


@pytest.mark.parametrize(
    "extra",
    [
        # plugin_loader under systemd: nothing to save, so no _ORIG.
        {"LD_LIBRARY_PATH": "/tmp/_MEIabc123"},
        {"LD_LIBRARY_PATH": "/tmp/_MEIabc123", "LD_LIBRARY_PATH_ORIG": "/opt/lib"},
        {"LD_LIBRARY_PATH": "/tmp/_MEIabc123/", "LD_LIBRARY_PATH_ORIG": ""},
    ],
)
def test_doctor_finds_the_flatpak_under_a_frozen_parents_library_path(tmp_path, extra):
    result = _doctor(_service_env(tmp_path, **extra))

    assert _moonlight_line(result).endswith("flatpak run com.moonlight_stream.Moonlight")


def test_doctor_finds_the_flatpak_in_a_bare_service_environment(tmp_path):
    result = _doctor(_service_env(tmp_path))

    assert _moonlight_line(result).endswith("flatpak run com.moonlight_stream.Moonlight")


def test_a_crashing_flatpak_is_reported_as_such_not_as_not_installed(tmp_path, monkeypatch):
    # In-process (no re-exec), so the poisoned path reaches the fake flatpak.
    env = _service_env(tmp_path, LD_LIBRARY_PATH="/tmp/_MEIabc123")
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("LD_LIBRARY_PATH", env["LD_LIBRARY_PATH"])

    assert moonlight.find_binary() is None
    assert "list` failed (exit 1)" in moonlight.not_found_label()
    assert "OPENSSL_3.4.0" in moonlight.not_found_label()
    with pytest.raises(moonlight.MoonlightNotFoundError, match="OPENSSL_3.4.0"):
        moonlight.list_apps("MY-GAMING-PC")


def test_not_installed_keeps_the_plain_message(tmp_path, monkeypatch):
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))

    assert moonlight.find_binary() is None
    assert moonlight.not_found_label() == "not found"
    with pytest.raises(moonlight.MoonlightNotFoundError) as excinfo:
        moonlight.list_apps("MY-GAMING-PC")
    assert str(excinfo.value) == (
        "moonlight CLI not found (native binary, flatpak, or MOONLIGHT_BIN)"
    )


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, None),
        ({"LD_LIBRARY_PATH": ""}, None),
        ({"LD_LIBRARY_PATH": "/opt/lib:/usr/local/lib"}, None),
        ({"LD_LIBRARY_PATH": "/tmp/_MEIx"}, {}),
        ({"LD_LIBRARY_PATH": "/tmp/_MEIx:/opt/lib"}, {"LD_LIBRARY_PATH": "/opt/lib"}),
        (
            {"LD_LIBRARY_PATH": "/tmp/_MEIx", "LD_LIBRARY_PATH_ORIG": "/opt/lib"},
            {"LD_LIBRARY_PATH": "/opt/lib"},
        ),
        ({"LD_LIBRARY_PATH": "/tmp/_MEIx", "LD_LIBRARY_PATH_ORIG": ""}, {}),
    ],
)
def test_repaired_env(environ, expected):
    base = {"HOME": "/home/deck"}
    result = procenv.repaired_env({**base, **environ})

    assert result == (None if expected is None else {**base, **expected})


def test_reexec_is_a_no_op_without_a_frozen_parent(monkeypatch):
    monkeypatch.delenv("LD_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setattr(os, "execve", lambda *a: pytest.fail("must not exec"))

    procenv.reexec_if_needed()
