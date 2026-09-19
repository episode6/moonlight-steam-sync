"""Configuration: TOML file + environment + CLI flags -> :class:`Config`.

Precedence (spec 3.2): CLI flags win over the config file, the config file
wins over built-in defaults. The tool never writes the config file back.

The SteamGridDB API key has its own, narrower lookup chain (spec 3.2):

    1. ``[steamgriddb].api_key`` in the config file (or its CLI flag override)
    2. the ``SGDB_API_KEY`` environment variable
    3. the contents of ``~/.config/moonlight-steam-sync/sgdb-api-key``

The key is optional throughout: an empty string means "no key", and callers
fall back to Steam-store-only matching (spec 3.5).
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from moonlight_steam_sync.hosts import active_host_path, read_active_host

#: Where the config file lives unless overridden (tests override this).
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "moonlight-steam-sync"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"
DEFAULT_KEY_FILE = DEFAULT_CONFIG_DIR / "sgdb-api-key"

#: Sentinel meaning "the flag was not passed on the command line", so a flag
#: whose default happens to equal the config default does not clobber it.
_UNSET = object()


class ConfigError(ValueError):
    """A bad ``--owned-apps`` / ``--ignore-file`` file (spec 3.4.1/3.4.3/3.4.8).

    Raised before the Steam library is opened or Moonlight is asked
    anything; ``main()`` turns it into ``<command>: <message>`` on stderr
    (an ``error`` event under ``--json``) and exit 1.
    """


__all__ = [
    "Config",
    "ConfigError",
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_KEY_FILE",
    "active_host_path",
    "default_exe",
    "default_launch_options",
    "load_config",
    "load_owned_apps",
    "owned_apps_steamid3",
]


def default_exe() -> str:
    """The tool's own resolved path, used as the default ``exe``.

    When ``exe`` is left at this default, the shortcut re-invokes
    moonlight-steam-sync itself (``launch "<name>"``, spec 3.2/3.3) rather
    than a separate stream script.
    """
    return os.path.realpath(sys.argv[0])


def default_launch_options(exe: str) -> str:
    """``'launch "{name}"'`` when ``exe`` is still this tool, else ``'"{name}"'``."""
    if os.path.realpath(exe) == default_exe():
        return 'launch "{name}"'
    return '"{name}"'


@dataclass
class Config:
    """Fully resolved configuration for one run."""

    host: str = ""
    name_suffix: str = ""
    exe: str = field(default_factory=default_exe)
    launch_options: str = ""
    start_dir: str = ""
    ignore: list[str] = field(default_factory=list)
    restart_steam: bool = True
    request_interval_ms: int = 250
    sgdb_api_key: str = ""
    sgdb_community_fallback: bool = True
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    config_path: Path = DEFAULT_CONFIG_PATH
    #: Where ``host`` came from: ``"flag"``, ``"state"`` or ``"config"``
    #: (which also covers "nothing set", spec 3.4.8/3.12).
    host_source: str = "config"
    #: ``--owned-apps PATH``, unparsed -- flag only, never a TOML key (spec
    #: 3.4.1); ``load_owned_apps()`` does the actual loading/validation.
    owned_apps_path: Path | None = None

    def __post_init__(self) -> None:
        if not self.launch_options:
            self.launch_options = default_launch_options(self.exe)
        if not self.start_dir:
            self.start_dir = os.path.dirname(self.exe)


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _resolve_api_key(
    file_value: str,
    flag_value: Any,
    env: dict[str, str],
    key_file: Path,
) -> str:
    """Implements the config -> env -> keyfile lookup order (spec 3.2)."""
    if flag_value is not _UNSET and flag_value:
        return str(flag_value)
    if file_value:
        return file_value
    env_value = env.get("SGDB_API_KEY", "")
    if env_value:
        return env_value
    if key_file.is_file():
        return key_file.read_text().strip()
    return ""


def load_config(
    args: Any = None,
    *,
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
    key_file: Path | None = None,
    state_file: Path | None = None,
) -> Config:
    """Build a :class:`Config` from the file, environment and CLI flags.

    ``args`` is an :class:`argparse.Namespace` (or anything with matching
    attributes / ``None``); a missing or ``None`` attribute means "no flag
    override was supplied" and does not shadow the file value. Passing an
    object whose attributes are the sentinel :data:`_UNSET` also counts as
    "not supplied", which is how the tests exercise precedence directly.

    ``host`` resolution (spec 3.4.8/3.12): the ``--host`` flag, else
    ``state_file`` (``active_host_path()`` by default; read only when the
    flag is not given, and an empty/whitespace-only file counts as absent),
    else ``host`` in the config file.
    """
    env = os.environ if env is None else env
    config_path = DEFAULT_CONFIG_PATH if config_path is None else config_path
    key_file = DEFAULT_KEY_FILE if key_file is None else key_file
    state_file = active_host_path() if state_file is None else state_file

    file_data = _load_toml(config_path)

    def flag(name: str) -> Any:
        if args is None:
            return _UNSET
        value = getattr(args, name, _UNSET)
        return _UNSET if value is None else value

    def pick(name: str, default: Any) -> Any:
        flag_value = flag(name)
        if flag_value is not _UNSET:
            return flag_value
        if name in file_data:
            return file_data[name]
        return default

    sgdb_section = file_data.get("steamgriddb", {})
    overrides_section = file_data.get("overrides", {})

    exe = pick("exe", default_exe())
    launch_options = pick("launch_options", "")
    start_dir = pick("start_dir", "")

    api_key = _resolve_api_key(
        file_value=sgdb_section.get("api_key", ""),
        flag_value=flag("sgdb_api_key"),
        env=env,
        key_file=key_file,
    )

    # pick() only looks at top-level file_data, so resolve this nested key
    # (it lives under [steamgriddb] in the file) explicitly.
    community_fallback_flag = flag("sgdb_community_fallback")
    community_fallback = (
        community_fallback_flag
        if community_fallback_flag is not _UNSET
        else sgdb_section.get("community_fallback", True)
    )

    # `sync --no-restart-steam` parses to `args.no_restart_steam = True`
    # (there is no `--restart-steam` flag to negate), so it cannot be read
    # back through pick("restart_steam", ...), which looks for an attribute
    # named `restart_steam` that argparse never sets. Treat the negated flag
    # as an explicit False override before falling back to the file/default.
    restart_steam = False if flag("no_restart_steam") is True else pick("restart_steam", True)

    host_flag = flag("host")
    if host_flag is not _UNSET and host_flag:
        host, host_source = str(host_flag), "flag"
    else:
        state_host = read_active_host(state_file)
        if state_host:
            host, host_source = state_host, "state"
        else:
            host, host_source = str(file_data.get("host") or ""), "config"

    owned_apps_flag = flag("owned_apps")
    owned_apps_path = Path(owned_apps_flag) if owned_apps_flag not in (_UNSET, "", None) else None

    cfg = Config(
        host=host,
        host_source=host_source,
        owned_apps_path=owned_apps_path,
        name_suffix=pick("name_suffix", ""),
        exe=exe,
        launch_options=launch_options,
        start_dir=start_dir,
        ignore=pick("ignore", []),
        restart_steam=restart_steam,
        request_interval_ms=pick("request_interval_ms", 250),
        sgdb_api_key=api_key,
        sgdb_community_fallback=community_fallback,
        overrides=overrides_section,
        config_path=config_path,
    )
    return cfg


def load_owned_apps(path: Path) -> dict[int, str]:
    """Parse a ``--owned-apps`` file (spec 3.4.1): ``{appid: display name}``.

    Raises :class:`ConfigError` on a missing file, invalid JSON, a
    ``version`` other than 1, or a non-numeric key -- always before the
    Steam library is opened or Moonlight is asked anything. The
    ``steamid3`` field is returned alongside so callers can compare it with
    the picked Steam user; this function itself does not need a Steam
    install (``search`` and ``doctor`` load the file without one).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"owned-apps file {path} could not be read: {exc}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"owned-apps file {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"owned-apps file {path} is not a JSON object")
    if payload.get("version") != 1:
        raise ConfigError(f"owned-apps file {path} has an unsupported version")
    apps = payload.get("apps")
    if not isinstance(apps, dict):
        raise ConfigError(f"owned-apps file {path} has no 'apps' object")
    result: dict[int, str] = {}
    for key, value in apps.items():
        try:
            appid = int(key)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"owned-apps file {path} has a non-numeric appid: {key!r}"
            ) from exc
        result[appid] = str(value)
    return result


def owned_apps_steamid3(path: Path) -> int | None:
    """The ``steamid3`` field of a ``--owned-apps`` file, without validating
    ``apps`` (used only for the mismatch check, spec 3.4.1)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload["steamid3"])
    except (KeyError, TypeError, ValueError):
        return None
