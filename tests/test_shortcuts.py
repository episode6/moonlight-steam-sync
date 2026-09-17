"""Shortcut store tests (spec 2.1, 3.3, 3.6, 3.9).

The synthetic fixture these run against is described in
``tests/fixtures/README.md``; the tests read whichever of the real/synthetic
files exists, so a device capture can be dropped in without touching them.
"""

from __future__ import annotations

import os
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from moonlight_steam_sync import vdf
from moonlight_steam_sync.shortcuts import (
    Shortcut,
    ShortcutsError,
    ShortcutsFile,
    ShortcutsParseError,
    app_name_for,
    appid,
    moonlight_name_from,
    moonlight_name_from_app_name,
    quote,
    render_launch_options,
    unquote,
)

STREAM_SH = "/home/deck/server-scripts/chimera/stream.sh"
DEFAULT_TEMPLATE = 'launch "{name}"'


@pytest.fixture
def store(synthetic_shortcuts_vdf) -> ShortcutsFile:
    return ShortcutsFile.from_bytes(synthetic_shortcuts_vdf)


# --- appid (spec 2.1) ------------------------------------------------------


def test_appid_matches_the_documented_rule_for_a_known_input():
    """``crc32(Exe + AppName) | 0x80000000`` over the *quoted* Exe.

    The fixed value below is the rule applied by hand to exe ``"/tmp/x"``
    (quoted, as stored) and name ``Foo`` -- the same arithmetic
    ``steam_shortcuts_util``'s ``app_id_generator.rs`` and Steam ROM
    Manager's ``generate-app-id.ts`` do.
    """
    assert appid('"/tmp/x"', "Foo") == 4294224308
    assert appid('"/tmp/x"', "Foo") == (zlib.crc32(b'"/tmp/x"Foo') | 0x80000000)
    assert appid('"/tmp/x"', "Foo") & 0x80000000  # high bit always set


def test_appid_quotes_a_bare_exe_so_both_call_styles_agree():
    assert appid("/tmp/x", "Foo") == appid('"/tmp/x"', "Foo")


def test_appid_is_stable_and_name_sensitive():
    assert appid('"/tmp/x"', "Foo") != appid('"/tmp/x"', "Foo (streaming)")
    assert appid('"/tmp/y"', "Foo") != appid('"/tmp/x"', "Foo")


def test_appid_fits_in_the_int32_slot_the_file_stores_it_in():
    value = appid('"/tmp/x"', "Foo")
    payload = vdf.dumps({"m": {"appid": value}})
    assert vdf.loads(payload)["m"]["appid"] & 0xFFFFFFFF == value


# --- quoting ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "quoted"),
    [("/tmp/x", '"/tmp/x"'), ('"/tmp/x"', '"/tmp/x"'), ("", '""'), ('"', '"""')],
)
def test_quote_and_unquote(raw, quoted):
    assert quote(raw) == quoted
    assert unquote(quote(raw)) == unquote(raw)


# --- launch options templating, both directions (spec 3.3) -----------------


@pytest.mark.parametrize(
    ("template", "name", "rendered"),
    [
        (DEFAULT_TEMPLATE, "Elden Ring", 'launch "Elden Ring"'),
        ('"{name}"', "Elden Ring", '"Elden Ring"'),
        ("{name}", "Elden Ring", "Elden Ring"),
        (DEFAULT_TEMPLATE, "", 'launch ""'),
        (DEFAULT_TEMPLATE, 'Weird "Quoted" Name', 'launch "Weird "Quoted" Name"'),
        ('"{name}"', "Braces {2} Game", '"Braces {2} Game"'),
    ],
)
def test_launch_options_round_trip(template, name, rendered):
    assert render_launch_options(template, name) == rendered
    assert moonlight_name_from(template, rendered) == name


def test_moonlight_name_from_returns_none_when_it_does_not_match():
    assert moonlight_name_from(DEFAULT_TEMPLATE, "run org.libretro.RetroArch") is None
    assert moonlight_name_from(DEFAULT_TEMPLATE, "launch") is None
    assert moonlight_name_from("no placeholder", "no placeholder") is None


def test_app_name_suffix_round_trip():
    assert app_name_for("Elden Ring", " (streaming)") == "Elden Ring (streaming)"
    assert moonlight_name_from_app_name("Elden Ring (streaming)", " (streaming)") == "Elden Ring"
    assert moonlight_name_from_app_name("Elden Ring", " (streaming)") == "Elden Ring"
    assert moonlight_name_from_app_name("Elden Ring (streaming)", "") == "Elden Ring (streaming)"


def test_shortcut_moonlight_name_prefers_launch_options_then_app_name():
    sc = Shortcut(app_name="Elden Ring (streaming)", launch_options='launch "Elden Ring"')
    assert sc.moonlight_name(DEFAULT_TEMPLATE, " (streaming)") == "Elden Ring"
    hand_edited = Shortcut(app_name="Elden Ring (streaming)", launch_options="")
    assert hand_edited.moonlight_name(DEFAULT_TEMPLATE, " (streaming)") == "Elden Ring"


# --- parsing the fixture ---------------------------------------------------


def test_fixture_parses_into_the_expected_entries(store):
    assert [s.app_name for s in store] == [
        "Elden Ring (streaming)",
        "Hollow Knight (streaming)",
        "RetroArch",
    ]
    assert len(store) == 3
    first = store.shortcuts[0]
    assert first.exe == f'"{STREAM_SH}"'
    assert first.exe_path == STREAM_SH
    assert first.start_dir_path == os.path.dirname(STREAM_SH)
    assert first.appid == first.expected_appid
    assert first.index_key == "0"


def test_fixture_round_trips_byte_for_byte(store, synthetic_shortcuts_vdf):
    assert store.to_bytes() == synthetic_shortcuts_vdf
    assert store.changed is False


def test_real_device_fixture_round_trips(real_shortcuts_vdf):
    """Skipped until a sanitised device capture lands (tests/fixtures/README.md)."""
    store = ShortcutsFile.from_bytes(real_shortcuts_vdf)
    assert store.to_bytes() == real_shortcuts_vdf
    assert store.changed is False
    for shortcut in store:
        assert shortcut.app_name
        assert shortcut.exe


def test_unknown_fields_and_key_casing_are_preserved(store, synthetic_shortcuts_vdf):
    retroarch = store.shortcuts[2]
    assert retroarch.extra == {"ExtraSteamField": "whatever-steam-adds-next"}
    assert "Openvr" in retroarch.key_order  # not the canonical "OpenVR"
    assert retroarch.openvr == 0
    assert retroarch.tags == {"0": "favorite"}
    # Touch an unrelated entry; the odd one still serialises exactly as read.
    store.shortcuts[0].icon = "/tmp/icon.png"
    rebuilt = vdf.loads(store.to_bytes())["shortcuts"]["2"]
    assert rebuilt == vdf.loads(synthetic_shortcuts_vdf)["shortcuts"]["2"]


def test_absent_fields_stay_absent(store):
    minimal = {"appid": 5, "AppName": "Thing", "Exe": '"/bin/true"'}
    shortcut = Shortcut.from_mapping(minimal)
    assert shortcut.to_mapping() == minimal
    assert shortcut.allow_overlay == 0  # not the "new shortcut" default of 1


def test_an_absent_field_that_is_given_a_value_is_written(tmp_path):
    """Patching ``icon`` on an entry that had no ``icon`` key must add it (spec 3.6).

    Adopted SteamTinkerLaunch-era entries are exactly the case: the field is
    absent, PR-5 sets it, and dropping it would silently lose the icon.
    """
    minimal = {"appid": 5, "AppName": "Thing", "Exe": '"/bin/true"'}
    payload = vdf.dumps({"shortcuts": {"0": minimal}})
    store = ShortcutsFile.from_bytes(payload)
    assert store.changed is False

    store.shortcuts[0].icon = "/grid/5_icon.png"
    assert store.changed is True
    rebuilt = vdf.loads(store.to_bytes())["shortcuts"]["0"]
    assert rebuilt == {**minimal, "icon": "/grid/5_icon.png"}

    # Tags are a map; an added tag comes back too, and the fields still at
    # their absent values stay out of the file.
    store.shortcuts[0].tags = {"0": "streaming"}
    rebuilt = vdf.loads(store.to_bytes())["shortcuts"]["0"]
    assert rebuilt["tags"] == {"0": "streaming"}
    assert "AllowOverlay" not in rebuilt


def test_a_wide_integer_field_keeps_its_stored_width(tmp_path):
    """A future Steam field stored as u64 must not be narrowed to int32."""
    payload = vdf.dumps(
        {"shortcuts": {"0": {"appid": 5, "AppName": "X", "SomeFutureId": vdf.UInt64(2**40)}}}
    )
    store = ShortcutsFile.from_bytes(payload)
    assert store.to_bytes() == payload
    store.shortcuts[0].icon = "/tmp/icon.png"  # touch it, then check the rest survived
    assert vdf.loads(store.to_bytes())["shortcuts"]["0"]["SomeFutureId"] == 2**40


def test_a_new_shortcut_is_written_in_the_canonical_field_order():
    shortcut = Shortcut.create(app_name="Foo", exe="/tmp/x")
    assert list(shortcut.to_mapping()) == [
        "appid",
        "AppName",
        "Exe",
        "StartDir",
        "icon",
        "ShortcutPath",
        "LaunchOptions",
        "IsHidden",
        "AllowDesktopConfig",
        "AllowOverlay",
        "OpenVR",
        "Devkit",
        "DevkitGameID",
        "DevkitOverrideAppID",
        "LastPlayTime",
        "FlatpakAppID",
        "tags",
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"AppName": 5},
        {"appid": "not an int"},
        {"tags": "not a map"},
        {"tags": {"0": 1}},
    ],
)
def test_wrongly_typed_fields_raise(entry):
    with pytest.raises(ShortcutsParseError):
        Shortcut.from_mapping(entry)


def test_a_malformed_file_raises_before_anything_is_touched(tmp_path):
    path = tmp_path / "shortcuts.vdf"
    path.write_bytes(b"\x03garbage\x00")
    with pytest.raises(ShortcutsParseError):
        ShortcutsFile.read(path)


def test_a_payload_without_a_shortcuts_map_raises():
    with pytest.raises(ShortcutsParseError):
        ShortcutsFile.from_bytes(vdf.dumps({"something_else": {"0": {}}}))


def test_an_entry_that_is_not_a_map_raises():
    with pytest.raises(ShortcutsParseError):
        ShortcutsFile.from_bytes(vdf.dumps({"shortcuts": {"0": "nope"}}))


# --- creation and ownership (spec 3.3 / 3.6) -------------------------------


def test_create_follows_the_write_policy():
    shortcut = Shortcut.create(
        app_name="Elden Ring (streaming)",
        exe=STREAM_SH,
        launch_options=render_launch_options(DEFAULT_TEMPLATE, "Elden Ring"),
    )
    assert shortcut.exe == f'"{STREAM_SH}"'
    assert shortcut.start_dir == f'"{os.path.dirname(STREAM_SH)}"'
    assert shortcut.appid == appid(shortcut.exe, shortcut.app_name)
    assert shortcut.allow_desktop_config == 1
    assert shortcut.allow_overlay == 1
    assert (shortcut.is_hidden, shortcut.openvr, shortcut.devkit) == (0, 0, 0)
    assert shortcut.icon == "" and shortcut.shortcut_path == ""
    assert shortcut.tags == {}


def test_create_accepts_an_explicit_start_dir_and_tags():
    shortcut = Shortcut.create(
        app_name="X", exe="/tmp/x", start_dir="/var/tmp", tags=["fav", "stream"]
    )
    assert shortcut.start_dir == '"/var/tmp"'
    assert shortcut.tags == {"0": "fav", "1": "stream"}


def test_ownership_strips_quotes_and_follows_symlinks(tmp_path):
    real = tmp_path / "real" / "stream.sh"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\n")
    link = tmp_path / "link.sh"
    link.symlink_to(real)

    shortcut = Shortcut.create(app_name="X", exe=str(link))
    assert shortcut.is_owned_by(str(real))
    assert shortcut.is_owned_by(str(link))
    assert shortcut.is_owned_by(f'"{real}"')  # a quoted configured exe works too
    assert not shortcut.is_owned_by(str(tmp_path / "other.sh"))


def test_an_entry_with_no_exe_is_owned_by_nobody():
    assert not Shortcut().is_owned_by("/tmp/x")


def test_owned_picks_out_the_moonlight_entries(store):
    owned = store.owned(STREAM_SH)
    assert [s.app_name for s in owned] == [
        "Elden Ring (streaming)",
        "Hollow Knight (streaming)",
    ]
    assert store.owned("/usr/bin/flatpak")[0].app_name == "RetroArch"


def test_by_appid_finds_the_same_shortcut(store):
    first = store.shortcuts[0]
    assert store.by_appid(first.appid) is first
    # The stored int32 form is the same shortcut (spec 2.1).
    assert store.by_appid(first.appid - 2**32) is first
    assert store.by_appid(1) is None


def test_append_uses_the_next_numeric_key(store):
    added = store.append(Shortcut.create(app_name="New", exe="/tmp/new"))
    assert added.index_key == "3"
    assert list(vdf.loads(store.to_bytes())["shortcuts"]) == ["0", "1", "2", "3"]


def test_remove_leaves_the_other_keys_alone(store):
    store.remove(store.shortcuts[1])
    keys = list(vdf.loads(store.to_bytes())["shortcuts"])
    assert keys == ["0", "2"]


def test_an_in_memory_store_numbers_from_zero():
    store = ShortcutsFile()
    store.append(Shortcut.create(app_name="A", exe="/tmp/a"))
    store.append(Shortcut.create(app_name="B", exe="/tmp/b"))
    assert list(vdf.loads(store.to_bytes())["shortcuts"]) == ["0", "1"]


# --- writing (spec 3.6 / 3.9 item 4) ---------------------------------------


def test_write_is_a_no_op_when_nothing_changed(tmp_path, synthetic_shortcuts_vdf):
    path = tmp_path / "shortcuts.vdf"
    path.write_bytes(synthetic_shortcuts_vdf)
    before = path.stat().st_mtime_ns

    store = ShortcutsFile.read(path)
    assert store.write() is False
    assert path.stat().st_mtime_ns == before
    assert list(tmp_path.iterdir()) == [path]  # not even a backup


def test_write_replaces_atomically_and_leaves_no_temp_file(tmp_path, synthetic_shortcuts_vdf):
    path = tmp_path / "shortcuts.vdf"
    path.write_bytes(synthetic_shortcuts_vdf)

    store = ShortcutsFile.read(path)
    store.append(Shortcut.create(app_name="New (streaming)", exe=STREAM_SH))
    assert store.write() is True

    assert not (tmp_path / "shortcuts.vdf.tmp").exists()
    reread = ShortcutsFile.read(path)
    assert [s.app_name for s in reread][-1] == "New (streaming)"
    assert reread.changed is False
    # Writing again now that the store matches the file does nothing.
    assert reread.write() is False


def test_write_keeps_the_last_five_backups(tmp_path, synthetic_shortcuts_vdf):
    path = tmp_path / "shortcuts.vdf"
    path.write_bytes(synthetic_shortcuts_vdf)

    for index in range(7):
        store = ShortcutsFile.read(path)
        store.append(Shortcut.create(app_name=f"Game {index}", exe=STREAM_SH))
        moment = datetime(2026, 9, 16, 12, index, 0, tzinfo=UTC)
        assert store.write(now=moment) is True

    backups = sorted(p.name for p in tmp_path.glob("shortcuts.vdf.bak-*"))
    assert len(backups) == 5
    assert backups[0] == "shortcuts.vdf.bak-20260916T120200Z"
    assert backups[-1] == "shortcuts.vdf.bak-20260916T120600Z"
    # The newest backup is the file as it was before the last write.
    assert len(ShortcutsFile.from_bytes((tmp_path / backups[-1]).read_bytes())) == 9


def test_backups_within_the_same_second_do_not_collide(tmp_path, synthetic_shortcuts_vdf):
    path = tmp_path / "shortcuts.vdf"
    path.write_bytes(synthetic_shortcuts_vdf)
    moment = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
    for index in range(3):
        store = ShortcutsFile.read(path)
        store.append(Shortcut.create(app_name=f"Game {index}", exe=STREAM_SH))
        store.write(now=moment)
    assert len(list(tmp_path.glob("shortcuts.vdf.bak-*"))) == 3


def test_a_missing_file_is_an_empty_store_and_is_not_created_for_nothing(tmp_path):
    path = tmp_path / "config" / "shortcuts.vdf"
    store = ShortcutsFile.read(path)
    assert len(store) == 0
    assert store.changed is False
    assert store.write() is False
    assert not path.exists()


def test_a_missing_file_is_created_once_there_is_something_to_write(tmp_path):
    path = tmp_path / "config" / "shortcuts.vdf"
    store = ShortcutsFile.read(path)
    store.append(Shortcut.create(app_name="First (streaming)", exe=STREAM_SH))
    assert store.write() is True
    assert path.is_file()
    assert [s.app_name for s in ShortcutsFile.read(path)] == ["First (streaming)"]


def test_a_zero_byte_file_is_an_empty_store(tmp_path):
    path = tmp_path / "shortcuts.vdf"
    path.write_bytes(b"")
    store = ShortcutsFile.read(path)
    assert len(store) == 0
    assert store.write() is False


def test_write_without_a_path_raises():
    with pytest.raises(ShortcutsError):
        ShortcutsFile().write()


def test_patching_an_icon_changes_only_that_field(tmp_path, synthetic_shortcuts_vdf):
    path = tmp_path / "shortcuts.vdf"
    path.write_bytes(synthetic_shortcuts_vdf)
    store = ShortcutsFile.read(path)
    icon_path = "/home/deck/.local/share/Steam/userdata/123456789/config/grid/x_icon.png"
    store.shortcuts[0].icon = icon_path
    assert store.write() is True

    before = vdf.loads(synthetic_shortcuts_vdf)["shortcuts"]
    after = vdf.loads(Path(path).read_bytes())["shortcuts"]
    assert after["0"]["icon"] == icon_path
    assert {k: v for k, v in after["0"].items() if k != "icon"} == {
        k: v for k, v in before["0"].items() if k != "icon"
    }
    assert after["1"] == before["1"] and after["2"] == before["2"]
