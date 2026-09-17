"""Steam's non-Steam shortcut store: ``shortcuts.vdf``.

Layout (spec 2.1): the file is one binary KeyValues payload whose root holds
a single map ``shortcuts``, keyed ``"0"``, ``"1"``, ... -- one entry per
shortcut, each a map of the fields in :data:`FIELDS`, in that order.

Two format details this module is deliberately literal about:

* **``Exe`` and ``StartDir`` are stored wrapped in double quotes.**
  :class:`Shortcut` holds them *exactly as stored*, quotes included, because
  the appid rule hashes the stored string. :func:`unquote` /
  :attr:`Shortcut.exe_path` give the bare path; :meth:`Shortcut.create` adds
  the quotes for you.
* **The appid is derived, not assigned**: ``crc32(Exe + AppName) |
  0x80000000`` over the *quoted* ``Exe`` (spec 2.1). Steam does not enforce
  it, but every tool in this space uses it, and it is what lets the artwork
  filenames be known before the shortcut exists.

Everything here is built so that reading a file and writing it straight back
reproduces it byte for byte: keys keep their original spelling and order
(Steam treats them case-insensitively, so a real file may say ``Openvr``
where the docs say ``OpenVR``), fields absent from an entry stay absent, and
unknown fields are preserved verbatim. A freshly created :class:`Shortcut`
is written in the canonical spec-2.1 field order instead.

Durability (spec 3.6 / 3.9 item 4): :meth:`ShortcutsFile.write` is the one
non-incremental step in a run. It rotates a timestamped backup, writes a
temporary file beside the target and :func:`os.replace`\\ s it into place, and
it does nothing at all when the serialised bytes are unchanged.
"""

from __future__ import annotations

import contextlib
import os
import zlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moonlight_steam_sync import vdf

__all__ = [
    "BACKUP_PREFIX",
    "FIELDS",
    "ROOT_KEY",
    "Shortcut",
    "ShortcutsError",
    "ShortcutsFile",
    "ShortcutsParseError",
    "app_name_for",
    "appid",
    "moonlight_name_from",
    "moonlight_name_from_app_name",
    "quote",
    "render_launch_options",
    "unquote",
]

#: The root map every ``shortcuts.vdf`` holds.
ROOT_KEY = "shortcuts"

#: Backups are ``shortcuts.vdf.bak-<UTC ISO basic timestamp>`` (spec 3.6).
BACKUP_PREFIX = ".bak-"
BACKUP_KEEP = 5

#: ``(vdf key, attribute, kind)`` in the order Steam writes them (spec 2.1).
#: ``kind`` is ``"str"``, ``"i32"`` or ``"map"``.
FIELDS: tuple[tuple[str, str, str], ...] = (
    ("appid", "appid", "i32"),
    ("AppName", "app_name", "str"),
    ("Exe", "exe", "str"),
    ("StartDir", "start_dir", "str"),
    ("icon", "icon", "str"),
    ("ShortcutPath", "shortcut_path", "str"),
    ("LaunchOptions", "launch_options", "str"),
    ("IsHidden", "is_hidden", "i32"),
    ("AllowDesktopConfig", "allow_desktop_config", "i32"),
    ("AllowOverlay", "allow_overlay", "i32"),
    ("OpenVR", "openvr", "i32"),
    ("Devkit", "devkit", "i32"),
    ("DevkitGameID", "devkit_game_id", "str"),
    ("DevkitOverrideAppID", "devkit_override_appid", "i32"),
    ("LastPlayTime", "last_play_time", "i32"),
    ("FlatpakAppID", "flatpak_appid", "str"),
    ("tags", "tags", "map"),
)

_BY_LOWER_KEY = {key.lower(): (key, attr, kind) for key, attr, kind in FIELDS}


class ShortcutsError(Exception):
    """Base class for shortcut-store errors."""


class ShortcutsParseError(ShortcutsError):
    """``shortcuts.vdf`` is not a shortcut store this tool understands."""


def quote(path: str) -> str:
    """Wrap *path* in the double quotes Steam stores ``Exe``/``StartDir`` with.

    Idempotent: a value that is already quoted comes back unchanged.
    """
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        return path
    return f'"{path}"'


def unquote(value: str) -> str:
    """Strip one layer of surrounding double quotes, if present."""
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def appid(exe: str, app_name: str) -> int:
    """``crc32(Exe + AppName) | 0x80000000`` (spec 2.1).

    *exe* is the string **as stored in the file**, i.e. normally quoted;
    :func:`quote` is applied here so callers may pass either form. The result
    is the unsigned 32-bit id used for the grid artwork filenames; stored in
    the file as an int32 it appears negative, which is the same 4 bytes.
    """
    payload = (quote(exe) + app_name).encode("utf-8", "surrogateescape")
    return (zlib.crc32(payload) | 0x80000000) & 0xFFFFFFFF


def render_launch_options(template: str, name: str) -> str:
    """Fill ``{name}`` in a launch-options template (spec 3.2).

    ``str.replace`` rather than ``str.format``: game names contain braces
    often enough (``Portal {2}`` style fan titles, ``{Rebirth}``) that
    ``format`` would raise or mangle them.
    """
    return template.replace("{name}", name)


def moonlight_name_from(template: str, launch_options: str) -> str | None:
    """Recover the Moonlight app name from stored launch options (spec 3.3).

    The inverse of :func:`render_launch_options`: splits the template on
    ``{name}`` and peels the literal prefix and suffix off *launch_options*.
    Returns ``None`` when the template has no ``{name}`` placeholder or the
    stored value does not match it, so callers can fall back to the
    ``AppName`` (spec 3.3 recovers the name from ``LaunchOptions`` first).
    """
    if "{name}" not in template:
        return None
    prefix, _, suffix = template.partition("{name}")
    end = len(launch_options) - len(suffix)
    if end < len(prefix):
        return None
    if not launch_options.startswith(prefix) or not launch_options.endswith(suffix):
        return None
    return launch_options[len(prefix) : end]


def app_name_for(name: str, name_suffix: str = "") -> str:
    """The Steam ``AppName``: the Moonlight app name plus the configured suffix."""
    return f"{name}{name_suffix}"


def moonlight_name_from_app_name(app_name: str, name_suffix: str = "") -> str:
    """Strip ``name_suffix`` back off an ``AppName``."""
    if name_suffix and app_name.endswith(name_suffix):
        return app_name[: -len(name_suffix)]
    return app_name


@dataclass
class Shortcut:
    """One entry in ``shortcuts.vdf``.

    Field values are held exactly as the file stores them -- in particular
    :attr:`exe` and :attr:`start_dir` keep their surrounding double quotes.
    Use :meth:`create` to build a new entry from a bare path, and
    :attr:`exe_path` / :attr:`start_dir_path` to read one back.
    """

    appid: int = 0
    app_name: str = ""
    exe: str = ""
    start_dir: str = ""
    icon: str = ""
    shortcut_path: str = ""
    launch_options: str = ""
    is_hidden: int = 0
    allow_desktop_config: int = 1
    allow_overlay: int = 1
    openvr: int = 0
    devkit: int = 0
    devkit_game_id: str = ""
    devkit_override_appid: int = 0
    last_play_time: int = 0
    flatpak_appid: str = ""
    tags: dict[str, str] = field(default_factory=dict)

    #: Fields this tool does not know about, preserved verbatim on write.
    extra: dict[str, Any] = field(default_factory=dict)

    #: Original key spelling/order and the ``"0"``/``"1"`` index this entry
    #: was read under. Bookkeeping for the byte-identical round trip, not
    #: part of the entry's identity.
    key_order: tuple[str, ...] = field(default=(), compare=False, repr=False)
    index_key: str | None = field(default=None, compare=False, repr=False)

    # -- construction ---------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        app_name: str,
        exe: str,
        start_dir: str | None = None,
        launch_options: str = "",
        icon: str = "",
        tags: Iterable[str] = (),
    ) -> Shortcut:
        """Build a new entry per the write policy in spec 3.6.

        *exe* and *start_dir* are bare paths and get quoted here;
        *start_dir* defaults to ``dirname(exe)``. ``AllowDesktopConfig`` and
        ``AllowOverlay`` are 1, everything else 0 / empty, and ``appid`` is
        derived from the quoted ``Exe`` plus *app_name*.
        """
        quoted_exe = quote(exe)
        bare_exe = unquote(quoted_exe)
        quoted_start_dir = quote(start_dir if start_dir is not None else os.path.dirname(bare_exe))
        return cls(
            appid=appid(quoted_exe, app_name),
            app_name=app_name,
            exe=quoted_exe,
            start_dir=quoted_start_dir,
            icon=icon,
            launch_options=launch_options,
            allow_desktop_config=1,
            allow_overlay=1,
            tags={str(i): tag for i, tag in enumerate(tags)},
        )

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any], *, index_key: str | None = None) -> Shortcut:
        """Parse one entry map, preserving key order, spelling and unknowns."""
        values: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in mapping.items():
            known = _BY_LOWER_KEY.get(key.lower())
            if known is None:
                extra[key] = value
                continue
            _, attr, kind = known
            if kind == "str":
                if not isinstance(value, str):
                    raise ShortcutsParseError(f"{key} should be a string, got {value!r}")
                values[attr] = value
            elif kind == "i32":
                if isinstance(value, str) or not isinstance(value, int):
                    raise ShortcutsParseError(f"{key} should be an integer, got {value!r}")
                # Keep the value object as parsed (including the vdf.UInt64
                # / vdf.Int64 wrappers) so an integer's stored width survives
                # a read/write; only the appid is normalised, to the unsigned
                # form the grid filenames use.
                values[attr] = (int(value) & 0xFFFFFFFF) if attr == "appid" else value
            else:  # map
                if not isinstance(value, Mapping):
                    raise ShortcutsParseError(f"{key} should be a map, got {value!r}")
                if any(not isinstance(v, str) for v in value.values()):
                    raise ShortcutsParseError(f"{key} should map to strings, got {value!r}")
                values[attr] = dict(value)
        # Defaults are the "new shortcut" defaults, which are not the right
        # answer for an entry that simply omitted the field; zero them so a
        # parsed entry never gains a value it did not have.
        for _key, attr, kind in FIELDS:
            values.setdefault(attr, {} if kind == "map" else ("" if kind == "str" else 0))
        return cls(
            **values,
            extra=extra,
            key_order=tuple(mapping),
            index_key=index_key,
        )

    # -- accessors ------------------------------------------------------

    @property
    def exe_path(self) -> str:
        """:attr:`exe` without its stored quotes."""
        return unquote(self.exe)

    @property
    def start_dir_path(self) -> str:
        """:attr:`start_dir` without its stored quotes."""
        return unquote(self.start_dir)

    @property
    def expected_appid(self) -> int:
        """What :func:`appid` says this entry's id should be.

        Differs from :attr:`appid` only for entries some other tool wrote
        with a random id; the stored value is what Steam and the grid
        filenames use either way.
        """
        return appid(self.exe, self.app_name)

    def moonlight_name(self, launch_options_template: str, name_suffix: str = "") -> str:
        """The Moonlight app name behind this shortcut (spec 3.3).

        Recovered from ``LaunchOptions`` by reversing the template, falling
        back to ``AppName`` minus ``name_suffix`` when that does not match
        (an entry a human edited, or a template that has since changed).
        """
        recovered = moonlight_name_from(launch_options_template, self.launch_options)
        if recovered:
            return recovered
        return moonlight_name_from_app_name(self.app_name, name_suffix)

    def is_owned_by(self, exe: str) -> bool:
        """Ownership test from spec 3.3: unquoted ``Exe``, compared by realpath.

        This is what adopts the SteamTinkerLaunch-era entries: they already
        point at the configured ``exe``.
        """
        mine = self.exe_path
        if not mine:
            return False
        return os.path.realpath(mine) == os.path.realpath(unquote(exe))

    # -- serialisation --------------------------------------------------

    def to_mapping(self) -> dict[str, Any]:
        """Render back to the nested-dict form :mod:`~moonlight_steam_sync.vdf` writes.

        Keys that were present when the entry was read come back first, in
        their original order and spelling; a field that was absent stays
        absent. An entry built in memory (no :attr:`key_order`) is written in
        the canonical spec-2.1 order instead.
        """
        out: dict[str, Any] = {}
        seen_lower: set[str] = set()
        for key in self.key_order:
            lower = key.lower()
            seen_lower.add(lower)
            known = _BY_LOWER_KEY.get(lower)
            if known is None:
                if key in self.extra:
                    out[key] = self.extra[key]
                continue
            out[key] = self._value_for(known[1], known[2])
        if not self.key_order:
            for key, attr, kind in FIELDS:
                out[key] = self._value_for(attr, kind)
                seen_lower.add(key.lower())
        for key, value in self.extra.items():
            if key.lower() not in seen_lower:
                out[key] = value
        return out

    def _value_for(self, attr: str, kind: str) -> Any:
        value = getattr(self, attr)
        if kind == "map":
            return dict(value)
        return value


@dataclass
class ShortcutsFile:
    """The parsed contents of one ``shortcuts.vdf``, plus how to write it back."""

    shortcuts: list[Shortcut] = field(default_factory=list)
    path: Path | None = None
    root_key: str = ROOT_KEY
    alt_format: bool = False
    #: Top-level keys other than the shortcuts map, preserved verbatim.
    root_extra: dict[str, Any] = field(default_factory=dict)
    #: Original top-level key order, so an untouched file round-trips exactly.
    root_order: tuple[str, ...] = field(default=(), compare=False, repr=False)
    #: Bytes as read, for the "nothing changed, write nothing" check.
    original_bytes: bytes | None = field(default=None, compare=False, repr=False)

    # -- reading --------------------------------------------------------

    @classmethod
    def from_bytes(cls, data: bytes, *, path: Path | None = None) -> ShortcutsFile:
        """Parse a payload. Empty input is a valid, empty store (spec 3.6)."""
        try:
            alt_format = vdf.detect_alt_format(data)
            root = vdf.loads(data, alt_format=alt_format)
        except vdf.VdfError as exc:
            raise ShortcutsParseError(f"{path or '<bytes>'}: {exc}") from exc

        root_key: str | None = None
        entries: Mapping[str, Any] = {}
        root_extra: dict[str, Any] = {}
        for key, value in root.items():
            if root_key is None and key.lower() == ROOT_KEY and isinstance(value, Mapping):
                root_key = key
                entries = value
            else:
                root_extra[key] = value
        if root and root_key is None:
            raise ShortcutsParseError(
                f"{path or '<bytes>'}: no 'shortcuts' map at the root "
                f"(found {sorted(root)!r}); this is not a shortcuts.vdf"
            )

        shortcuts = []
        for index_key, entry in entries.items():
            if not isinstance(entry, Mapping):
                raise ShortcutsParseError(
                    f"{path or '<bytes>'}: shortcuts[{index_key!r}] is not a map"
                )
            shortcuts.append(Shortcut.from_mapping(entry, index_key=index_key))
        parsed = cls(
            shortcuts=shortcuts,
            path=path,
            root_key=root_key or ROOT_KEY,
            alt_format=alt_format,
            root_extra=root_extra,
            root_order=tuple(root) or (root_key or ROOT_KEY,),
        )
        # A zero-byte file is a valid empty store, but ``b""`` is not the
        # serialisation of one, so use the canonical empty payload as the
        # baseline: a run that adds nothing then writes nothing (spec 3.6).
        parsed.original_bytes = data if data else parsed.to_bytes()
        return parsed

    @classmethod
    def read(cls, path: str | os.PathLike[str]) -> ShortcutsFile:
        """Read *path*. A missing file is an empty store (fresh Steam user)."""
        path = Path(path)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            # A missing file is an empty store; baseline it as such so that a
            # run which adds no shortcuts does not create one (spec 3.6).
            empty = cls(path=path)
            empty.original_bytes = empty.to_bytes()
            return empty
        return cls.from_bytes(data, path=path)

    # -- collection interface -------------------------------------------

    def __iter__(self) -> Iterator[Shortcut]:
        return iter(self.shortcuts)

    def __len__(self) -> int:
        return len(self.shortcuts)

    def owned(self, exe: str) -> list[Shortcut]:
        """Entries whose ``Exe`` resolves to *exe* (spec 3.3 ownership)."""
        return [s for s in self.shortcuts if s.is_owned_by(exe)]

    def by_appid(self, wanted: int) -> Shortcut | None:
        """The entry with this appid, or ``None`` -- the "same shortcut" test (spec 3.6)."""
        wanted &= 0xFFFFFFFF
        for shortcut in self.shortcuts:
            if shortcut.appid == wanted:
                return shortcut
        return None

    def append(self, shortcut: Shortcut) -> Shortcut:
        """Add an entry under the next free numeric key (spec 3.6)."""
        shortcut.index_key = str(self._next_index())
        self.shortcuts.append(shortcut)
        return shortcut

    def remove(self, shortcut: Shortcut) -> None:
        """Drop an entry. Remaining entries keep their existing keys."""
        self.shortcuts.remove(shortcut)

    def _next_index(self) -> int:
        highest = -1
        for shortcut in self.shortcuts:
            key = shortcut.index_key
            if key is not None and key.isdigit():
                highest = max(highest, int(key))
        return highest + 1

    # -- writing --------------------------------------------------------

    def to_mapping(self) -> dict[str, Any]:
        entries: dict[str, Any] = {}
        used: set[str] = set()
        next_free = 0
        for shortcut in self.shortcuts:
            key = shortcut.index_key
            if key is None or key in used:
                while str(next_free) in used:
                    next_free += 1
                key = str(next_free)
                shortcut.index_key = key
            used.add(key)
            entries[key] = shortcut.to_mapping()
        root: dict[str, Any] = {}
        for key in self.root_order or (self.root_key,):
            if key == self.root_key:
                root[key] = entries
            elif key in self.root_extra:
                root[key] = self.root_extra[key]
        root.setdefault(self.root_key, entries)
        for key, value in self.root_extra.items():
            root.setdefault(key, value)
        return root

    def to_bytes(self) -> bytes:
        return vdf.dumps(self.to_mapping(), alt_format=self.alt_format)

    @property
    def changed(self) -> bool:
        """Whether writing would produce different bytes than were read."""
        return self.to_bytes() != self.original_bytes

    def write(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        backups: int = BACKUP_KEEP,
        now: datetime | None = None,
    ) -> bool:
        """Write the store back, atomically. Returns whether anything was written.

        Spec 3.6: serialise, keep ``shortcuts.vdf.bak-<timestamp>`` (the last
        :data:`BACKUP_KEEP`) beside the target, write ``shortcuts.vdf.tmp``
        and :func:`os.replace` it into place. When the serialised bytes match
        what was read the file is left completely alone and ``False`` comes
        back -- "nothing is written if the plan is empty".
        """
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ShortcutsError("no path to write to: pass one or read the file first")
        payload = self.to_bytes()
        if target == self.path and payload == self.original_bytes:
            return False

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self._rotate_backups(target, backups=backups, now=now)

        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, target)
        self.path = target
        self.original_bytes = payload
        return True

    @staticmethod
    def _backup_stamp(now: datetime | None) -> str:
        moment = now or datetime.now(UTC)
        if moment.tzinfo is not None:
            moment = moment.astimezone(UTC)
        return moment.strftime("%Y%m%dT%H%M%SZ")

    def _rotate_backups(self, target: Path, *, backups: int, now: datetime | None) -> None:
        if backups <= 0:
            return
        stamp = self._backup_stamp(now)
        backup = target.with_name(f"{target.name}{BACKUP_PREFIX}{stamp}")
        suffix = 1
        while backup.exists():
            backup = target.with_name(f"{target.name}{BACKUP_PREFIX}{stamp}-{suffix}")
            suffix += 1
        backup.write_bytes(target.read_bytes())

        existing = sorted(target.parent.glob(f"{target.name}{BACKUP_PREFIX}*"))
        for stale in existing[:-backups]:
            # Pruning is best effort: a backup we cannot delete is not a
            # reason to fail a write that already succeeded.
            with contextlib.suppress(OSError):
                stale.unlink()
