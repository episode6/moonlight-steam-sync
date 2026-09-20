"""Finding the Steam install, picking the user, and stopping/starting Steam.

Discovery (spec 3.4): ``$STEAM_ROOT`` wins if set (that is also how the tests
point the whole module at a temporary tree), otherwise the usual SteamOS /
Linux locations are tried in order. ``~/.steam/steam`` and ``~/.steam/root``
are symlinks to ``~/.local/share/Steam`` on SteamOS, so any of the three is a
valid answer.

Picking the user: one directory under ``userdata/`` means one user and no
ambiguity. With several, ``config/loginusers.vdf`` (*text* KeyValues) names
the one with ``MostRecent "1"``; its key is a steam64 id, and
``steamid3 = steam64 - 76561197960265728`` is the ``userdata`` directory name.

Concurrency (spec 2.1 / 3.6): Steam keeps ``shortcuts.vdf`` in memory and
rewrites it on exit, so it has to be shut down before the file is touched --
``steam -shutdown``, wait for the process to go, write, relaunch
``steam -silent``. Every subprocess call in here goes through
:class:`ProcessRunner` so tests can inject a fake instead of poking at a real
Steam install.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "GRID_IMAGE_EXTENSIONS",
    "GRID_SLOTS",
    "ProcessRunner",
    "STEAM64_OFFSET",
    "SteamError",
    "SteamNotFoundError",
    "SteamRunningError",
    "SteamUser",
    "SteamUserNotFoundError",
    "find_steam_root",
    "find_grid_file",
    "grid_stem",
    "is_running",
    "parse_text_vdf",
    "pick_user",
    "relaunch",
    "shutdown",
    "steamid3_from_steam64",
    "wait_for_exit",
]

#: ``steamid3 = steam64 - STEAM64_OFFSET`` (spec 3.4).
STEAM64_OFFSET = 76561197960265728

#: Candidate Steam roots, in the order they are tried. On SteamOS the first
#: is real and the others are symlinks to it.
STEAM_ROOT_CANDIDATES = (
    ".local/share/Steam",
    ".steam/steam",
    ".steam/root",
    ".var/app/com.valvesoftware.Steam/.local/share/Steam",
)

#: Artwork slot -> the suffix its file carries in ``config/grid/`` (spec 2.1).
#: The extension varies (``.png``/``.jpg``), which is why lookups go through
#: :func:`find_grid_file` rather than guessing one.
GRID_SLOTS: dict[str, str] = {
    "portrait": "p",
    "landscape": "",
    "hero": "_hero",
    "logo": "_logo",
    "icon": "_icon",
}

#: The extensions that count as artwork in ``config/grid/``. Steam honours
#: PNG and JPG (spec 2.1) and ``.ico`` for the icon slot; nothing else in that
#: directory is art. The allowlist exists because the landscape slot's stem is
#: the bare appid, so ``<appid>.json`` -- the logo-*position* sidecar the Steam
#: UI writes, not an image -- would otherwise look like a filled slot and stop
#: the landscape art from ever being fetched.
GRID_IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".ico"})


class SteamError(Exception):
    """Base class for Steam-install errors."""


class SteamNotFoundError(SteamError):
    """No Steam installation could be found."""


class SteamUserNotFoundError(SteamError):
    """The Steam user whose ``userdata`` directory to use could not be decided."""


class SteamRunningError(SteamError):
    """Steam is up when ``shortcuts.vdf`` has to be written, and may not be stopped.

    Raised instead of writing (spec 3.6: Steam holds the file in memory and
    rewrites it on exit, so an edit made under a live client is lost). Maps
    to CLI exit code 2.
    """


# ---------------------------------------------------------------------------
# text KeyValues (loginusers.vdf)
# ---------------------------------------------------------------------------


def parse_text_vdf(text: str) -> dict[str, Any]:
    """Parse *text* KeyValues into nested dicts.

    Enough of the format for ``loginusers.vdf``: quoted tokens with ``\\"``
    and ``\\\\`` escapes, ``{``/``}`` blocks, ``//`` line comments, and bare
    (unquoted) tokens. Conditional suffixes (``[$WIN32]``) are ignored, and a
    repeated key keeps the last value -- neither appears in the files this
    tool reads. Malformed input raises :class:`ValueError`.
    """
    tokens = _tokenize_text_vdf(text)
    root: dict[str, Any] = {}
    stack: list[dict[str, Any]] = [root]
    pending: str | None = None
    for token, quoted in tokens:
        if not quoted and token == "{":
            if pending is None:
                raise ValueError("text VDF: '{' without a key")
            child: dict[str, Any] = {}
            stack[-1][pending] = child
            stack.append(child)
            pending = None
        elif not quoted and token == "}":
            if pending is not None:
                raise ValueError(f"text VDF: key {pending!r} has no value")
            if len(stack) == 1:
                raise ValueError("text VDF: '}' without a matching '{'")
            stack.pop()
        elif pending is None:
            pending = token
        else:
            stack[-1][pending] = token
            pending = None
    if pending is not None:
        raise ValueError(f"text VDF: key {pending!r} has no value")
    if len(stack) != 1:
        raise ValueError("text VDF: unclosed '{'")
    return root


def _tokenize_text_vdf(text: str) -> list[tuple[str, bool]]:
    tokens: list[tuple[str, bool]] = []
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            newline = text.find("\n", i)
            i = length if newline < 0 else newline + 1
        elif char == '"':
            i += 1
            chunk: list[str] = []
            while True:
                if i >= length:
                    raise ValueError("text VDF: unterminated quoted string")
                c = text[i]
                if c == "\\" and i + 1 < length:
                    nxt = text[i + 1]
                    chunk.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                    i += 2
                    continue
                if c == '"':
                    i += 1
                    break
                chunk.append(c)
                i += 1
            tokens.append(("".join(chunk), True))
        elif char in "{}":
            tokens.append((char, False))
            i += 1
        else:
            start = i
            while i < length and text[i] not in ' \t\r\n"{}':
                i += 1
            tokens.append((text[start:i], False))
    return tokens


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def find_steam_root(
    *,
    env: Mapping[str, str] | None = None,
    home: str | os.PathLike[str] | None = None,
) -> Path:
    """Locate the Steam root directory (spec 3.4).

    ``$STEAM_ROOT`` overrides everything -- it is the documented escape hatch
    for an unusual install and how the tests point at a fixture tree. Raises
    :class:`SteamNotFoundError` when nothing is found.
    """
    env = os.environ if env is None else env
    override = env.get("STEAM_ROOT", "").strip()
    if override:
        root = Path(override).expanduser()
        if not root.is_dir():
            raise SteamNotFoundError(f"STEAM_ROOT is set to {override!r}, which is not a directory")
        return root
    base = Path(home) if home is not None else Path(env.get("HOME") or Path.home())
    for candidate in STEAM_ROOT_CANDIDATES:
        path = base / candidate
        if path.is_dir():
            return path
    raise SteamNotFoundError(
        f"no Steam installation under {base}; tried {', '.join(STEAM_ROOT_CANDIDATES)}. "
        "Set STEAM_ROOT if Steam lives somewhere else."
    )


def steamid3_from_steam64(steam64: int | str) -> int:
    """``steamid3 = steam64 - 76561197960265728`` (spec 3.4)."""
    value = int(steam64)
    if value < STEAM64_OFFSET:
        raise ValueError(f"{value} is not a steam64 id")
    return value - STEAM64_OFFSET


@dataclass(frozen=True)
class SteamUser:
    """One Steam account's ``userdata`` directory."""

    steamid3: int
    path: Path
    account_name: str = ""
    persona_name: str = ""
    most_recent: bool = field(default=False, compare=False)

    @property
    def steam64(self) -> int:
        return self.steamid3 + STEAM64_OFFSET

    @property
    def config_dir(self) -> Path:
        return self.path / "config"

    @property
    def shortcuts_path(self) -> Path:
        """``userdata/<steamid3>/config/shortcuts.vdf`` (spec 2.1)."""
        return self.config_dir / "shortcuts.vdf"

    @property
    def grid_dir(self) -> Path:
        """``userdata/<steamid3>/config/grid/``, where artwork lives (spec 2.1)."""
        return self.config_dir / "grid"


def list_users(root: Path) -> list[SteamUser]:
    """Every numeric directory under ``userdata/``, lowest id first.

    ``userdata/0`` and ``userdata/anonymous`` are Steam's own placeholders
    and are skipped.
    """
    userdata = Path(root) / "userdata"
    if not userdata.is_dir():
        return []
    users = []
    for entry in sorted(userdata.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or not entry.name.isdigit():
            continue
        steamid3 = int(entry.name)
        if steamid3 == 0:
            continue
        users.append(SteamUser(steamid3=steamid3, path=entry))
    return sorted(users, key=lambda u: u.steamid3)


def read_login_users(root: Path) -> dict[int, dict[str, str]]:
    """``config/loginusers.vdf`` keyed by steamid3.

    A missing or unparseable file is reported as "no information" (an empty
    dict) rather than an error: it only ever narrows a choice that
    :func:`list_users` has already made.
    """
    path = Path(root) / "config" / "loginusers.vdf"
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return {}
    try:
        parsed = parse_text_vdf(text)
    except ValueError:
        return {}
    users_block: Any = {}
    for key, value in parsed.items():
        if key.lower() == "users" and isinstance(value, Mapping):
            users_block = value
            break
    result: dict[int, dict[str, str]] = {}
    for steam64, info in users_block.items():
        if not isinstance(info, Mapping):
            continue
        try:
            steamid3 = steamid3_from_steam64(steam64)
        except ValueError:
            continue
        result[steamid3] = {str(k): str(v) for k, v in info.items() if isinstance(v, str)}
    return result


def pick_user(root: str | os.PathLike[str]) -> SteamUser:
    """Decide which Steam user this run acts on (spec 3.4).

    One ``userdata`` directory means one user. With several, the one
    ``loginusers.vdf`` marks ``MostRecent "1"`` wins; if that does not
    resolve exactly one, :class:`SteamUserNotFoundError` explains which ids
    were found rather than guessing.
    """
    root = Path(root)
    users = list_users(root)
    if not users:
        raise SteamUserNotFoundError(
            f"no Steam user directories under {root / 'userdata'}; "
            "log in to Steam at least once first"
        )
    login_info = read_login_users(root)
    enriched = [
        SteamUser(
            steamid3=user.steamid3,
            path=user.path,
            account_name=login_info.get(user.steamid3, {}).get("AccountName", ""),
            persona_name=login_info.get(user.steamid3, {}).get("PersonaName", ""),
            most_recent=login_info.get(user.steamid3, {}).get("MostRecent", "0") == "1",
        )
        for user in users
    ]
    if len(enriched) == 1:
        return enriched[0]
    most_recent = [user for user in enriched if user.most_recent]
    if len(most_recent) == 1:
        return most_recent[0]
    ids = ", ".join(str(user.steamid3) for user in enriched)
    raise SteamUserNotFoundError(
        f"{len(enriched)} Steam users under {root / 'userdata'} ({ids}) and "
        f"{'no' if not most_recent else 'more than one'} MostRecent entry in "
        "config/loginusers.vdf; cannot decide which one to write to"
    )


# ---------------------------------------------------------------------------
# grid paths
# ---------------------------------------------------------------------------


def grid_stem(appid: int, slot: str) -> str:
    """The artwork filename stem for *appid* in *slot*, e.g. ``123p`` (spec 2.1)."""
    try:
        suffix = GRID_SLOTS[slot]
    except KeyError:
        raise ValueError(
            f"unknown artwork slot {slot!r}; expected one of {sorted(GRID_SLOTS)}"
        ) from None
    return f"{appid}{suffix}"


def find_grid_file(grid_dir: str | os.PathLike[str], appid: int, slot: str) -> Path | None:
    """The existing artwork *image* for this slot, whatever its extension.

    "A slot whose file already exists in ``grid/`` is skipped" (spec 3.5) is
    both the resume rule and the don't-clobber-hand-picked-art rule, so the
    check has to be extension-agnostic -- but only across the extensions that
    are actually art (:data:`GRID_IMAGE_EXTENSIONS`). ``<appid>`` (landscape)
    is matched exactly so that it never picks up ``<appid>p`` or
    ``<appid>_hero``, and the extension allowlist keeps two non-images out:
    ``<appid>.json`` (the logo-position sidecar the Steam UI writes, spec 2.1)
    and ``<stem>.<ext>.part`` (a download still in flight, spec 3.9 item 3).
    """
    stem = grid_stem(appid, slot)
    directory = Path(grid_dir)
    if not directory.is_dir():
        return None
    for candidate in sorted(directory.glob(f"{stem}.*")):
        if candidate.suffix.lower() not in GRID_IMAGE_EXTENSIONS:
            continue
        if candidate.is_file() and candidate.stem == stem:
            return candidate
    return None


# ---------------------------------------------------------------------------
# process control
# ---------------------------------------------------------------------------


class ProcessRunner:
    """The seam every subprocess call in this module goes through.

    Tests substitute a fake; nothing else in the package shells out to Steam
    directly.
    """

    def run(self, cmd: Sequence[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603 - fixed argv, never a shell string
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )

    def spawn(self, cmd: Sequence[str]) -> None:
        """Start a detached process whose output goes nowhere."""
        with open(os.devnull, "wb") as devnull:
            subprocess.Popen(  # noqa: S603 - fixed argv, never a shell string
                list(cmd),
                stdout=devnull,
                stderr=devnull,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()


def is_running(
    runner: ProcessRunner | None = None,
    *,
    proc_dir: str | os.PathLike[str] = "/proc",
) -> bool:
    """Whether a Steam client is running (spec 3.6).

    ``pgrep -x steam`` first; if ``pgrep`` is not installed, fall back to
    scanning ``/proc/*/comm``.
    """
    runner = runner or ProcessRunner()
    try:
        result = runner.run(["pgrep", "-x", "steam"], timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass
    else:
        if result.returncode in (0, 1):
            return result.returncode == 0
    directory = Path(proc_dir)
    if not directory.is_dir():
        return False
    for comm_path in directory.glob("[0-9]*/comm"):
        try:
            if comm_path.read_text().strip() == "steam":
                return True
        except OSError:
            continue
    return False


def shutdown(
    runner: ProcessRunner | None = None,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.5,
    proc_dir: str | os.PathLike[str] = "/proc",
) -> bool:
    """``steam -shutdown``, then wait for the process to go (spec 3.6).

    Returns ``True`` once Steam is gone (immediately, if it was not running),
    ``False`` if it is still up after *timeout* seconds -- the caller decides
    whether that is fatal, because writing ``shortcuts.vdf`` under a live
    Steam loses the edit.
    """
    runner = runner or ProcessRunner()
    if not is_running(runner, proc_dir=proc_dir):
        return True
    try:
        runner.run(["steam", "-shutdown"], timeout=15)
    except subprocess.TimeoutExpired:
        # The request was very likely delivered -- `steam -shutdown` hands it
        # to the running client and can then sit there. Whether Steam is
        # actually gone is the poll loop's question, not this one's.
        pass
    except (OSError, subprocess.SubprocessError) as exc:
        raise SteamError(f"could not run 'steam -shutdown': {exc}") from exc
    return wait_for_exit(runner, timeout=timeout, poll_interval=poll_interval, proc_dir=proc_dir)


def wait_for_exit(
    runner: ProcessRunner | None = None,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.5,
    proc_dir: str | os.PathLike[str] = "/proc",
) -> bool:
    """Poll :func:`is_running` until Steam is gone or *timeout* seconds pass.

    The wait half of :func:`shutdown`, on its own for ``--commit
    await-exit`` (decky spec 3.5), where something else -- the plugin's
    ``StartShutdown`` from Game Mode -- takes the client down and this tool
    only watches for it to go. Returns ``True`` once Steam is gone (at once
    if it was not running), ``False`` if it is still up at the deadline.
    Nothing here catches ``KeyboardInterrupt``: a caller that must not be
    interrupted defers ``SIGINT`` itself, and one that may (the await-exit
    wait) lets it through.
    """
    runner = runner or ProcessRunner()
    deadline = runner.monotonic() + timeout
    while True:
        if not is_running(runner, proc_dir=proc_dir):
            return True
        if runner.monotonic() >= deadline:
            return False
        runner.sleep(poll_interval)


def relaunch(runner: ProcessRunner | None = None) -> None:
    """Start Steam again, detached, in the background (spec 3.6)."""
    runner = runner or ProcessRunner()
    try:
        runner.spawn(["steam", "-silent"])
    except (OSError, subprocess.SubprocessError) as exc:
        raise SteamError(f"could not relaunch Steam: {exc}") from exc
