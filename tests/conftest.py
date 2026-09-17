"""Shared fixture plumbing.

Fixture files live in ``tests/fixtures/``. Everything there is synthetic for
now -- see ``tests/fixtures/README.md`` for what each one stands in for and
how to capture the real thing. The helpers here are what make "swap in a real
capture later" a file drop rather than a test rewrite: tests ask for a
fixture *by role*, and take whichever of the real/synthetic files exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(FIXTURES))


def fixture_bytes(*names: str) -> bytes | None:
    """Contents of the first of *names* that exists under ``tests/fixtures``."""
    for name in names:
        path = FIXTURES / name
        if path.is_file():
            return path.read_bytes()
    return None


@pytest.fixture
def synthetic_shortcuts_vdf() -> bytes:
    """The hand-built binary ``shortcuts.vdf`` (spec 2.1 field order)."""
    data = fixture_bytes("shortcuts_synthetic.vdf")
    assert data is not None, "tests/fixtures/shortcuts_synthetic.vdf is missing"
    return data


@pytest.fixture
def real_shortcuts_vdf() -> bytes:
    """A sanitised device capture, or skip.

    TODO (synthetic fixture): drop ``tests/fixtures/shortcuts_real.vdf`` in
    to activate every test that asks for this fixture. Capture instructions
    are in ``tests/fixtures/README.md``.
    """
    data = fixture_bytes("shortcuts_real.vdf")
    if data is None:
        pytest.skip(
            "no real shortcuts.vdf capture yet: add tests/fixtures/shortcuts_real.vdf "
            "(see tests/fixtures/README.md)"
        )
    return data


@pytest.fixture
def loginusers_text() -> str:
    """``loginusers.vdf``: the real capture if present, else the synthetic one."""
    data = fixture_bytes("loginusers_real.vdf", "loginusers_synthetic.vdf")
    assert data is not None, "tests/fixtures/loginusers_synthetic.vdf is missing"
    return data.decode("utf-8")
