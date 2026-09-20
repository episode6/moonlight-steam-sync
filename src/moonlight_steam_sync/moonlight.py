"""Drive the `moonlight` CLI (spec 2.1, 2.3, 3.4): find it, list a host's
games, and stream one.

Binary discovery order (spec 3.4): the ``MOONLIGHT_BIN`` environment
override, then the native ``moonlight`` binary on ``PATH``, then the Flathub
flatpak (``com.moonlight_stream.Moonlight``). Nothing here is SteamOS-specific
beyond that flatpak fallback.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

#: moonlight-qt's own connect timeout (`COMPUTER_SEEK_TIMEOUT`,
#: app/cli/listapps.cpp) is 30s, so a 30s budget here usually races
#: moonlight's own "Failed to connect" and loses, and leaves no room for
#: `getAppList()` on a 500-title host either. Generous rather than tight.
LIST_TIMEOUT_S = 60.0

#: The flatpak's application id (spec 2.3).
FLATPAK_APP_ID = "com.moonlight_stream.Moonlight"


#: Why the last :func:`find_binary` flatpak probe failed, when it did: a
#: `flatpak list` that *crashes* (a broken library path, say) is not the same
#: finding as "the app is not installed", and used to be reported as one.
_flatpak_problem: str | None = None


class MoonlightNotFoundError(RuntimeError):
    """No usable `moonlight` binary (native, flatpak, or MOONLIGHT_BIN)."""


class MoonlightUnreachableError(RuntimeError):
    """The moonlight CLI ran but the host did not answer in time or errored
    (spec 3.3 exit code 3)."""


@dataclass(frozen=True)
class App:
    """One line of `moonlight list <host>` (spec 3.4)."""

    name: str


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

    global _flatpak_problem
    _flatpak_problem = None
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
        except (OSError, subprocess.TimeoutExpired) as exc:
            _flatpak_problem = f"`{flatpak} list` could not run: {exc}"
            return None
        if FLATPAK_APP_ID in result.stdout:
            return [flatpak, "run", FLATPAK_APP_ID]
        if result.returncode != 0:
            detail = (result.stderr.strip().splitlines() or ["no output"])[0]
            _flatpak_problem = f"`{flatpak} list` failed (exit {result.returncode}): {detail}"

    return None


def not_found_label() -> str:
    """`doctor`'s value for a missing binary: ``not found``, plus the reason
    when the flatpak probe itself failed rather than came back empty."""
    return f"not found ({_flatpak_problem})" if _flatpak_problem else "not found"


def _not_found_message() -> str:
    message = "moonlight CLI not found (native binary, flatpak, or MOONLIGHT_BIN)"
    return f"{message}: {_flatpak_problem}" if _flatpak_problem else message


def _parse_list(text: str) -> list[App]:
    """Parse plain `moonlight list <host>` output: one app name per line
    (moonlight-qt's `app/cli/listapps.cpp` prints ``"%s\\n"`` per app and
    nothing else on stdout). Blank lines are skipped; names are kept verbatim
    otherwise, since the name is what `moonlight stream` is later given.
    """
    return [App(name=line) for line in text.splitlines() if line.strip()]


def list_apps(host: str, *, timeout: float = LIST_TIMEOUT_S) -> list[App]:
    """Run `moonlight list <host>` and return its apps (spec 3.4's
    `list(host)`; named `list_apps` here to avoid shadowing the builtin).

    Deliberately the *plain* form, not ``--csv``: moonlight-qt's CSV mode
    calls ``loadBoxArt()`` for every app before printing, which fires one
    box-art fetch per title at the host in a burst. On a large library that
    burst has crashed an Apollo host outright; the plain form only asks for
    the app list. The price is that the CSV-only columns are gone: no
    per-app id, no ``Hidden``/``App Collection Game`` flags (an app hidden
    in the Moonlight client is listed like any other, so ``ignore`` is the
    way to keep one out), and no cached box-art path.

    Raises :class:`MoonlightNotFoundError` when no binary is available, and
    :class:`MoonlightUnreachableError` when the binary runs but the host does
    not answer within ``timeout`` or the process exits non-zero -- both map
    to CLI exit code 3 (spec 3.3).
    """
    binary = find_binary()
    if binary is None:
        raise MoonlightNotFoundError(_not_found_message())

    argv = [*binary, "list", host]
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

    return _parse_list(result.stdout)


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
        raise MoonlightNotFoundError(_not_found_message())

    argv = [*binary, "stream", host, name, *extra_args]
    os.execvp(argv[0], argv)


def run_client(extra_args: Sequence[str] = ()) -> None:
    """`exec` into the Moonlight GUI itself, with no host or app (decky spec
    3.4.5's `client` subcommand): the hidden "Moonlight" shortcut `sync
    --client-shortcut` writes launches this, so *Open Moonlight* in Game
    Mode is a Steam-launched app like any other. Same binary discovery as
    :func:`stream`; raises :class:`MoonlightNotFoundError` when there is no
    binary, and does not return on success.
    """
    binary = find_binary()
    if binary is None:
        raise MoonlightNotFoundError(_not_found_message())

    argv = [*binary, *extra_args]
    os.execvp(argv[0], argv)
