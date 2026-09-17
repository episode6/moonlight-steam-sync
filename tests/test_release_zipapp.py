"""Build the release zipapp exactly the way ``.github/workflows/release.yml``
does, and smoke-run it the same way -- ``--version`` and ``doctor`` -- so a
regression in the packaging invocation (spec 3.1) fails locally, not only in
CI's own build of the workflow.
"""

from __future__ import annotations

import subprocess
import sys
import zipapp
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_pyz(tmp_path: Path) -> Path:
    out = tmp_path / "moonlight-steam-sync.pyz"
    zipapp.create_archive(
        source=REPO_ROOT / "src",
        target=out,
        interpreter="/usr/bin/env python3",
        main="moonlight_steam_sync.__main__:main",
        compressed=True,
    )
    return out


def test_zipapp_builds_and_reports_its_version(tmp_path):
    pyz = _build_pyz(tmp_path)
    assert pyz.is_file()

    result = subprocess.run(
        [sys.executable, str(pyz), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "moonlight-steam-sync" in result.stdout


def test_zipapp_doctor_smoke_run(tmp_path):
    """``doctor`` (spec 3.3) must run to completion -- exit 0 -- with no
    Steam install, no ``moonlight`` binary and no config file present, which
    is exactly the state of a CI runner or a fresh install."""
    pyz = _build_pyz(tmp_path)

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    result = subprocess.run(
        [sys.executable, str(pyz), "doctor"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "python:" in result.stdout
    assert "steam dir:" in result.stdout
