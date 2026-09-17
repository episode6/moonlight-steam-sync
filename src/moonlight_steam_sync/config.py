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

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Where the config file lives unless overridden (tests override this).
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "moonlight-steam-sync"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"
DEFAULT_KEY_FILE = DEFAULT_CONFIG_DIR / "sgdb-api-key"

#: Names Sunshine ships out of the box (spec 6.13); only used to *document*
#: the example config, never applied unless the user pastes it themselves.
EXAMPLE_IGNORE = ["Desktop", "Steam Big Picture"]

#: Sentinel meaning "the flag was not passed on the command line", so a flag
#: whose default happens to equal the config default does not clobber it.
_UNSET = object()


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
) -> Config:
    """Build a :class:`Config` from the file, environment and CLI flags.

    ``args`` is an :class:`argparse.Namespace` (or anything with matching
    attributes / ``None``); a missing or ``None`` attribute means "no flag
    override was supplied" and does not shadow the file value. Passing an
    object whose attributes are the sentinel :data:`_UNSET` also counts as
    "not supplied", which is how the tests exercise precedence directly.
    """
    env = os.environ if env is None else env
    config_path = DEFAULT_CONFIG_PATH if config_path is None else config_path
    key_file = DEFAULT_KEY_FILE if key_file is None else key_file

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

    cfg = Config(
        host=pick("host", ""),
        name_suffix=pick("name_suffix", ""),
        exe=exe,
        launch_options=launch_options,
        start_dir=start_dir,
        ignore=pick("ignore", []),
        restart_steam=pick("restart_steam", True),
        request_interval_ms=pick("request_interval_ms", 250),
        sgdb_api_key=api_key,
        sgdb_community_fallback=community_fallback,
        overrides=overrides_section,
        config_path=config_path,
    )
    return cfg
