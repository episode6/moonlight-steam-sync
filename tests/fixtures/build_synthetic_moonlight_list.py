#!/usr/bin/env python3
"""Regenerate ``moonlight_list_large_synthetic.txt``: a 500-title host.

===========================================================================
TODO (synthetic fixture): replace ``moonlight_list_large_synthetic.txt``
with real ``moonlight list <host>`` output from the user's largest
Moonlight host (the one that publishes 500+ titles, spec 7).

    HOW TO CAPTURE
      1. On a machine with the Moonlight client paired to that host:
         ``moonlight list <host> > moonlight_list_large_real.txt``
         (flatpak: ``flatpak run com.moonlight_stream.Moonlight list
         <host>``). The plain form, not ``--csv``: CSV mode fetches box
         art for every title first, which has crashed an Apollo host.
      2. Sanitise: nothing host-specific is in the output (it is only the
         app names), so game names are fine to keep as they are.
      3. Drop it in as ``tests/fixtures/moonlight_list_large_real.txt``.
         ``tests/test_sync_e2e.py`` asks for the large library *by role*
         (``large_library_list`` in ``tests/conftest.py``) and prefers the
         real file when it exists, so nothing else changes.

    WHY IT MATTERS: the resumability test (spec PR-5 (b), 3.9) proves that
    a crash after N calls into a 500-title import costs nothing already
    done. The synthetic names are regular on purpose ("Synthetic Title
    001"...) so the bulk fake transport in ``tests/art_fixtures.py`` can
    answer for them by pattern; a real capture has real names, and the
    same test then runs against the same transport keyed on the list's
    names rather than on a number.
===========================================================================

Byte shape (spec 2.3, moonlight-qt ``app/cli/listapps.cpp``): one app name
per line, ``"%s\\n"``, nothing else on stdout and no header.

Run from the repo root::

    python3 tests/fixtures/build_synthetic_moonlight_list.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repo root

from tests.fakes import moonlight_list  # noqa: E402

TITLES = 500
FIXTURE_PATH = Path(__file__).with_name("moonlight_list_large_synthetic.txt")


def title(index: int) -> str:
    """``Synthetic Title 001`` ... ``Synthetic Title 500``."""
    return f"Synthetic Title {index:03d}"


def build() -> str:
    return moonlight_list(title(i) for i in range(1, TITLES + 1))


if __name__ == "__main__":
    FIXTURE_PATH.write_text(build(), encoding="utf-8")
    print(f"wrote {FIXTURE_PATH} ({TITLES} titles)")
