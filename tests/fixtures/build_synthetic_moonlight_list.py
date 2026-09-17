#!/usr/bin/env python3
"""Regenerate ``moonlight_list_large_synthetic.csv``: a 500-title host.

===========================================================================
TODO (synthetic fixture): replace ``moonlight_list_large_synthetic.csv``
with real ``moonlight list <host> --csv`` output from the user's largest
Moonlight host (the one that publishes 500+ titles, spec 7).

    HOW TO CAPTURE
      1. On a machine with the Moonlight client paired to that host:
         ``moonlight list <host> --csv > moonlight_list_large_real.csv``
         (flatpak: ``flatpak run com.moonlight_stream.Moonlight list <host>
         --csv``). Give it time; ``--csv`` may block while box art caches.
      2. Sanitise: replace the host UUID in every ``Boxart URL`` cache path
         with ``<host-uuid>`` and any absolute home directory with
         ``/home/deck``. Game names are fine to keep.
      3. Drop it in as ``tests/fixtures/moonlight_list_large_real.csv``.
         ``tests/test_sync_e2e.py`` asks for the large library *by role*
         (``large_library_csv`` in ``tests/conftest.py``) and prefers the
         real file when it exists, so nothing else changes.

    WHY IT MATTERS: the resumability test (spec PR-5 (b), 3.9) proves that
    a crash after N calls into a 500-title import costs nothing already
    done. The synthetic names are regular on purpose ("Synthetic Title
    001"...) so the bulk fake transport in ``tests/art_fixtures.py`` can
    answer for them by pattern; a real capture has real names, and the
    same test then runs against the same transport keyed on the CSV's
    names rather than on a number.
===========================================================================

Byte shape (spec 2.3, moonlight-qt ``app/cli/listapps.cpp``): the header
separates fields with ``", "``; rows are plain comma-separated with the name
and the box art URL double-quoted; booleans are lowercase ``true``/``false``;
``Boxart URL`` is ``qrc:/res/no_app_image.png`` when nothing is cached. No
row here carries a cached box-art path: that case is covered by
``moonlight_list_sample.csv`` and the host-art tests in ``test_art_apply.py``.

Run from the repo root::

    python3 tests/fixtures/build_synthetic_moonlight_list.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the repo root

from tests.fakes import csv_row, moonlight_csv  # noqa: E402

TITLES = 500
FIXTURE_PATH = Path(__file__).with_name("moonlight_list_large_synthetic.csv")


def title(index: int) -> str:
    """``Synthetic Title 001`` ... ``Synthetic Title 500``."""
    return f"Synthetic Title {index:03d}"


def build() -> str:
    return moonlight_csv(csv_row(title(i), i) for i in range(1, TITLES + 1))


if __name__ == "__main__":
    FIXTURE_PATH.write_text(build(), encoding="utf-8")
    print(f"wrote {FIXTURE_PATH} ({TITLES} titles)")
