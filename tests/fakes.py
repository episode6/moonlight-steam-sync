"""Fakes for the outside world: Steam's process, a Steam directory tree, and
the ``moonlight`` CLI.

Nothing in the test suite touches a real Steam install or a real Moonlight
host. The Steam tree is built under ``tmp_path`` and pointed at through
``$STEAM_ROOT`` (the documented override, spec 3.4); every subprocess call
the Steam side makes goes through :class:`FakeRunner`; and ``moonlight`` is
a shell script on ``PATH`` that prints a ``list --csv`` capture, so the real
:func:`moonlight_steam_sync.moonlight.list_apps` (argv, timeout, CSV
parsing) is what the end-to-end tests exercise.

TODO(real-data): the CSV those scripts print is SYNTHETIC -- see
``tests/fixtures/README.md`` for the exact byte shape it copies from
moonlight-qt's source and how to replace it with a real
``moonlight list <host> --csv`` capture per host. :func:`moonlight_csv` and
``tests/fixtures/build_synthetic_moonlight_list.py`` are the only places
that shape is written down in code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.parse
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from moonlight_steam_sync.steam import ProcessRunner

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: The stream script the synthetic ``shortcuts.vdf`` entries point at
#: (``tests/fixtures/build_synthetic_shortcuts.py``): the configured ``exe``
#: that makes those entries "owned" (spec 3.3).
STREAM_SH = "/home/deck/server-scripts/chimera/stream.sh"
STEAMID3 = 123456789

#: moonlight-qt's ``--csv`` header, verbatim (spec 2.3): fields separated by
#: ``", "``.
CSV_HEADER = "Name, ID, HDR Support, App Collection Game, Hidden, Direct Launch, Boxart URL"
NO_BOXART = "qrc:/res/no_app_image.png"


# ---------------------------------------------------------------------------
# Steam's process
# ---------------------------------------------------------------------------


class FakeRunner(ProcessRunner):
    """Records every call and answers from a scripted plan.

    ``running`` says whether ``pgrep -x steam`` finds a live Steam;
    ``steam -shutdown`` makes it report "gone" two polls later, unless
    ``ignores_shutdown`` is set, in which case Steam never exits (the
    30 s timeout path). ``spawned`` records relaunches.
    """

    def __init__(
        self,
        *,
        running: bool = False,
        pgrep_missing: bool = False,
        ignores_shutdown: bool = False,
    ) -> None:
        self.running = running
        self.pgrep_missing = pgrep_missing
        self.ignores_shutdown = ignores_shutdown
        self.calls: list[list[str]] = []
        self.spawned: list[list[str]] = []
        self.slept = 0.0
        self.clock = 0.0
        self.shutdown_after_polls: int | None = None
        self._polls = 0

    def run(self, cmd, *, timeout: float = 10.0) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if cmd[0] == "pgrep":
            if self.pgrep_missing:
                raise FileNotFoundError("pgrep")
            self._polls += 1
            if self.shutdown_after_polls is not None and self._polls > self.shutdown_after_polls:
                self.running = False
            return subprocess.CompletedProcess(cmd, 0 if self.running else 1, "", "")
        if cmd[:2] == ["steam", "-shutdown"]:
            if not self.ignores_shutdown:
                self.shutdown_after_polls = self._polls + 2
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def spawn(self, cmd) -> None:
        self.spawned.append(list(cmd))
        if cmd[:1] == ["steam"]:
            self.running = True

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.clock += seconds

    def monotonic(self) -> float:
        return self.clock

    @property
    def shutdowns(self) -> int:
        return sum(1 for c in self.calls if c[:2] == ["steam", "-shutdown"])

    @property
    def relaunches(self) -> int:
        return sum(1 for c in self.spawned if c[:1] == ["steam"])


# ---------------------------------------------------------------------------
# a Steam directory tree
# ---------------------------------------------------------------------------


def make_steam_root(
    root: Path,
    *,
    shortcuts: bytes | None = None,
    steamid3: int = STEAMID3,
) -> Path:
    """``<root>/userdata/<steamid3>/config[/shortcuts.vdf]``.

    ``shortcuts=None`` is a fresh Steam user (no file at all, spec 3.6);
    pass the synthetic fixture's bytes for a library with adoptable entries.
    """
    config_dir = root / "userdata" / str(steamid3) / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    if shortcuts is not None:
        (config_dir / "shortcuts.vdf").write_bytes(shortcuts)
    return root


def point_steam_root_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setenv("STEAM_ROOT", str(root))


# ---------------------------------------------------------------------------
# the moonlight CLI
# ---------------------------------------------------------------------------


def csv_row(
    name: str,
    app_id: int | str,
    *,
    boxart: str | None = None,
    hidden: bool = False,
    collection: bool = False,
) -> str:
    """One ``moonlight list --csv`` row, shaped like moonlight-qt writes it.

    ``boxart`` is a filesystem path (percent-encoded into a ``file://`` URL
    the way ``QUrl::toDisplayString()`` does) or ``None`` for "not cached"
    (``qrc:/res/no_app_image.png``). Names are double-quoted; a quote inside
    a name is doubled, which is what Qt's CSV writer and Python's reader
    agree on.
    """
    quoted_name = '"' + name.replace('"', '""') + '"'
    url = NO_BOXART if boxart is None else "file://" + urllib.parse.quote(boxart)
    return ",".join(
        [
            quoted_name,
            str(app_id),
            "false",
            "true" if collection else "false",
            "true" if hidden else "false",
            "true",
            f'"{url}"',
        ]
    )


def moonlight_csv(rows: Iterable[str]) -> str:
    return CSV_HEADER + "\n" + "".join(row + "\n" for row in rows)


def install_fake_moonlight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, csv_text: str, *, exit_code: int = 0
) -> Path:
    """Put a ``moonlight`` shell script on ``PATH`` that prints ``csv_text``.

    Records its argv to ``<tmp_path>/moonlight-argv.txt`` so a test can
    check the real ``list <host> --csv`` invocation went out.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    csv_file = tmp_path / "moonlight-list.csv"
    csv_file.write_text(csv_text, encoding="utf-8")
    argv_file = tmp_path / "moonlight-argv.txt"
    script = bin_dir / "moonlight"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{argv_file}"\n'
        f'cat "{csv_file}"\n'
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.delenv("MOONLIGHT_BIN", raising=False)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    assert shutil.which("moonlight") == str(script)
    return argv_file


def fake_moonlight_argv(tmp_path: Path) -> Sequence[str]:
    argv_file = tmp_path / "moonlight-argv.txt"
    return argv_file.read_text(encoding="utf-8").split("\n")[:-1]
