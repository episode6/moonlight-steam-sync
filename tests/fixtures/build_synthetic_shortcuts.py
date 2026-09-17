#!/usr/bin/env python3
"""Regenerate ``shortcuts_synthetic.vdf`` from raw bytes.

===========================================================================
TODO (synthetic fixture): replace ``shortcuts_synthetic.vdf`` with a real,
sanitised ``shortcuts.vdf`` captured from a device.

    HOW TO CAPTURE
      1. On the Steam Deck / Linux box, in Desktop Mode, run
         ``moonlight-steam-sync doctor`` and note the "steam user" line; it
         prints the exact ``.../userdata/<steamid3>/config/shortcuts.vdf``.
      2. Quit Steam completely (``steam -shutdown``) so the file on disk is
         the one Steam last wrote, then copy it off the device.
      3. Sanitise it: the only personal data it can carry is file paths and
         game names. Rewrite absolute home paths to ``/home/deck/...`` and
         replace any host name with ``MY-GAMING-PC`` -- byte-for-byte edits
         are not needed, just re-save it through a VDF editor, or hand-patch
         the strings and let the round-trip test catch a mistake.
      4. Drop it in as ``tests/fixtures/shortcuts_real.vdf`` and delete the
         ``pytest.skip`` guard in ``tests/test_shortcuts.py``
         (``test_real_device_fixture_round_trips``). The tests already read
         whichever fixtures exist, so this is a file drop, not a rewrite.

    WHY IT MATTERS: this synthetic file is built from the field order, type
    bytes and quoting documented in spec 2.1, which is second-hand knowledge.
    A real capture is the only thing that proves Steam's writer agrees --
    especially about key spelling (``OpenVR`` vs ``Openvr``), which fields
    Steam actually emits, and whether anything new has appeared since.
===========================================================================

This generator writes the binary by hand -- ``struct`` and byte literals
only, no import of :mod:`moonlight_steam_sync.vdf`. That is deliberate: a
fixture produced by the code under test would prove nothing. ``pytest``
asserts the committed file still matches this script's output, so the
fixture can be regenerated with::

    python3 tests/fixtures/build_synthetic_shortcuts.py

The entries are modelled on what a real device holds after the
SteamTinkerLaunch era described in spec 3.3/3.8:

* entry 0 -- a Moonlight shortcut pointing at ``stream.sh``: the "owned"
  case the tool adopts, complete with the ``(streaming)`` name suffix and a
  ``launch "<name>"`` style launch-options value.
* entry 1 -- a second Moonlight shortcut, already carrying an ``icon`` path
  into ``grid/``, so the icon-patch path has something to leave alone.
* entry 2 -- an unrelated non-Steam shortcut (a flatpak emulator) with
  non-canonical key casing, a tag, and an unknown ``ExtraSteamField`` that
  this tool must preserve verbatim rather than drop.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

TYPE_MAP = b"\x00"
TYPE_STRING = b"\x01"
TYPE_INT32 = b"\x02"
END = b"\x08"

EXE = '"/home/deck/server-scripts/chimera/stream.sh"'
START_DIR = '"/home/deck/server-scripts/chimera"'
GRID = "/home/deck/.local/share/Steam/userdata/123456789/config/grid"


def cstr(value: str) -> bytes:
    return value.encode("utf-8") + b"\x00"


def kv_string(key: str, value: str) -> bytes:
    return TYPE_STRING + cstr(key) + cstr(value)


def kv_int(key: str, value: int) -> bytes:
    return TYPE_INT32 + cstr(key) + struct.pack("<I", value & 0xFFFFFFFF)


def kv_map(key: str, body: bytes) -> bytes:
    return TYPE_MAP + cstr(key) + body + END


def appid(exe: str, app_name: str) -> int:
    """crc32(Exe + AppName) | 0x80000000, over the *quoted* Exe (spec 2.1)."""
    return (zlib.crc32((exe + app_name).encode("utf-8")) | 0x80000000) & 0xFFFFFFFF


def entry(
    *,
    app_name: str,
    exe: str,
    start_dir: str,
    launch_options: str,
    icon: str = "",
    tags: tuple[str, ...] = (),
    openvr_key: str = "OpenVR",
    extra: bytes = b"",
    last_play_time: int = 0,
) -> bytes:
    """One shortcut entry, fields in the order spec 2.1 says Steam writes them."""
    return (
        kv_int("appid", appid(exe, app_name))
        + kv_string("AppName", app_name)
        + kv_string("Exe", exe)
        + kv_string("StartDir", start_dir)
        + kv_string("icon", icon)
        + kv_string("ShortcutPath", "")
        + kv_string("LaunchOptions", launch_options)
        + kv_int("IsHidden", 0)
        + kv_int("AllowDesktopConfig", 1)
        + kv_int("AllowOverlay", 1)
        + kv_int(openvr_key, 0)
        + kv_int("Devkit", 0)
        + kv_string("DevkitGameID", "")
        + kv_int("DevkitOverrideAppID", 0)
        + kv_int("LastPlayTime", last_play_time)
        + kv_string("FlatpakAppID", "")
        + extra
        + kv_map("tags", b"".join(kv_string(str(i), tag) for i, tag in enumerate(tags)))
    )


def build() -> bytes:
    entry0 = entry(
        app_name="Elden Ring (streaming)",
        exe=EXE,
        start_dir=START_DIR,
        launch_options='launch "Elden Ring"',
    )
    entry1 = entry(
        app_name="Hollow Knight (streaming)",
        exe=EXE,
        start_dir=START_DIR,
        launch_options='launch "Hollow Knight"',
        icon=f"{GRID}/{appid(EXE, 'Hollow Knight (streaming)')}_icon.png",
        last_play_time=1757980800,
    )
    entry2 = entry(
        app_name="RetroArch",
        exe='"/usr/bin/flatpak"',
        start_dir='"/usr/bin"',
        launch_options="run org.libretro.RetroArch",
        tags=("favorite",),
        # Steam is case-insensitive about keys and third-party writers are
        # inconsistent; this proves the codec preserves the original spelling.
        openvr_key="Openvr",
        # A field this tool knows nothing about, which must survive a
        # read/modify/write untouched (spec 3.4, "unknown keys preserved").
        extra=kv_string("ExtraSteamField", "whatever-steam-adds-next"),
    )
    shortcuts = b"".join(
        kv_map(str(index), body) for index, body in enumerate((entry0, entry1, entry2))
    )
    return kv_map("shortcuts", shortcuts) + END


FIXTURE_PATH = Path(__file__).with_name("shortcuts_synthetic.vdf")

if __name__ == "__main__":
    FIXTURE_PATH.write_bytes(build())
    print(f"wrote {FIXTURE_PATH} ({FIXTURE_PATH.stat().st_size} bytes)")
