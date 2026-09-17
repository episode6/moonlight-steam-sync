"""Drive the `moonlight` CLI (spec 2.1, 2.3, 3.4): find it, list a host's
games, and stream one.

Binary discovery order (spec 3.4): the ``MOONLIGHT_BIN`` environment
override, then the native ``moonlight`` binary on ``PATH``, then the Flathub
flatpak (``com.moonlight_stream.Moonlight``). Nothing here is SteamOS-specific
beyond that flatpak fallback.
"""

from __future__ import annotations

import csv
import io
import os
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

#: `moonlight list --csv` can block while box art downloads on a first run
#: (spec 2.3's "[verify]" note); this is generous rather than tight.
LIST_TIMEOUT_S = 30.0

#: The flatpak's application id (spec 2.3).
FLATPAK_APP_ID = "com.moonlight_stream.Moonlight"


class MoonlightNotFoundError(RuntimeError):
    """No usable `moonlight` binary (native, flatpak, or MOONLIGHT_BIN)."""


class MoonlightUnreachableError(RuntimeError):
    """The moonlight CLI ran but the host did not answer in time or errored
    (spec 3.3 exit code 3)."""


@dataclass(frozen=True)
class App:
    """One row of `moonlight list --csv`, after filtering (spec 3.4)."""

    name: str
    id: str
    hidden: bool
    boxart_path: str | None


def find_binary() -> list[str] | None:
    """Return the argv prefix that invokes moonlight, or ``None``.

    A flatpak result is ``["flatpak", "run", "com.moonlight_stream.Moonlight"]``
    -- the manifest's own `command` is already `moonlight`, so no
    `--command=` override is needed (spec 2.3).
    """
    override = os.environ.get("MOONLIGHT_BIN")
    if override:
        return shlex.split(override)

    native = shutil.which("moonlight")
    if native:
        return [native]

    flatpak = shutil.which("flatpak")
    if flatpak:
        try:
            result = subprocess.run(
                [flatpak, "list", "--app", "--columns=application"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except OSError:
            return None
        if FLATPAK_APP_ID in result.stdout:
            return [flatpak, "run", FLATPAK_APP_ID]

    return None


def _parse_bool(value: str) -> bool:
    """moonlight-qt's CSV boolean spelling is not documented (spec 2.3); this
    accepts the common spellings a Qt CLI could plausibly emit
    (``true``/``1``/``yes``, case-insensitive) and treats everything else,
    including an empty cell, as false."""
    return value.strip().lower() in ("true", "1", "yes")


def _parse_boxart(value: str) -> str | None:
    """A `file://` URL becomes a plain filesystem path; ``qrc:/res/no_app_image.png``
    (not cached yet, spec 2.3) or anything else becomes ``None``."""
    value = value.strip()
    if value.startswith("file://"):
        return value[len("file://") :]
    return None


def _parse_csv(text: str) -> list[App]:
    """Parse `moonlight list --csv` output into :class:`App` rows.

    Rows flagged ``App Collection Game`` (a Steam-collection placeholder, not
    a streamable app) or ``Hidden`` are dropped entirely rather than returned
    with a flag set, per the PR-3 brief ("Hidden/App Collection Game flags ->
    skipped"); see the PR description's "Notes for review" for the
    alternative reading this rules out.
    """
    apps: list[App] = []
    for row in csv.DictReader(io.StringIO(text)):
        if _parse_bool(row.get("App Collection Game", "")):
            continue
        hidden = _parse_bool(row.get("Hidden", ""))
        if hidden:
            continue
        apps.append(
            App(
                name=row["Name"],
                id=row["ID"],
                hidden=hidden,
                boxart_path=_parse_boxart(row.get("Boxart URL", "")),
            )
        )
    return apps


def list_apps(host: str, *, timeout: float = LIST_TIMEOUT_S) -> list[App]:
    """Run `moonlight list <host> --csv` and return the visible, streamable
    apps (spec 3.4's `list(host)`; named `list_apps` here to avoid shadowing
    the builtin).

    Raises :class:`MoonlightNotFoundError` when no binary is available, and
    :class:`MoonlightUnreachableError` when the binary runs but the host does
    not answer within ``timeout`` or the process exits non-zero -- both map
    to CLI exit code 3 (spec 3.3).
    """
    binary = find_binary()
    if binary is None:
        raise MoonlightNotFoundError(
            "moonlight CLI not found (native binary, flatpak, or MOONLIGHT_BIN)"
        )

    argv = [*binary, "list", host, "--csv"]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise MoonlightUnreachableError(
            f"moonlight list {host!r} timed out after {timeout:g}s"
        ) from exc
    except OSError as exc:
        raise MoonlightUnreachableError(f"failed to run moonlight: {exc}") from exc

    if result.returncode != 0:
        raise MoonlightUnreachableError(
            f"moonlight list {host!r} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )

    return _parse_csv(result.stdout)


def stream(host: str, name: str, extra_args: Sequence[str] = ()) -> None:
    """`exec` into `moonlight stream <host> <name> [extra_args...]` (spec
    3.3's `launch` subcommand), replacing this process so signals and the
    terminal pass straight through to the stream, exactly like a shell
    running the command directly.

    Raises :class:`MoonlightNotFoundError` when no binary is available;
    on success this function does not return.
    """
    binary = find_binary()
    if binary is None:
        raise MoonlightNotFoundError(
            "moonlight CLI not found (native binary, flatpak, or MOONLIGHT_BIN)"
        )

    argv = [*binary, "stream", host, name, *extra_args]
    os.execvp(argv[0], argv)
