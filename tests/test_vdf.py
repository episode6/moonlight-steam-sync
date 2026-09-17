"""Binary KeyValues codec tests (spec 2.1 / 3.4).

Two independent checks back each other up:

* the ``ValvePython/vdf`` package as an **oracle** -- it is a dev-only
  dependency (never imported by the shipped package) and is what "the format
  is really like this" means here;
* a **hand-built fixture** (``tests/fixtures/build_synthetic_shortcuts.py``)
  written from raw byte literals, which mimics Steam's own field order and
  quoting without going through the code under test.

The contract in both directions is ``dumps(loads(x)) == x``, byte for byte.
"""

from __future__ import annotations

import struct

# ``build_synthetic_shortcuts`` lives in tests/fixtures/, which conftest.py
# puts on sys.path; it builds the fixture from raw bytes, independently of
# the code under test.
import build_synthetic_shortcuts as builder
import pytest

from moonlight_steam_sync import vdf

oracle = pytest.importorskip("vdf", reason="the vdf package is a dev dependency (test oracle)")


# --- oracle cross-checks ---------------------------------------------------

EVERY_TYPE = {
    "root": {
        "a_string": "hello world",
        "an_int32": 42,
        "a_negative_int32": -1234,
        "an_int32_min": -(2**31),
        "a_uint64": vdf.UInt64(2**63 + 5),
        "an_int64": vdf.Int64(-5),
        "a_nested_map": {"deeper": {"still": "yes"}, "empty": {}},
        "an_empty_string": "",
        "unicode": "Ōkami ™ – ünïcøde",
    }
}


def _to_oracle(obj):
    """Our typed ints -> the oracle's, so the two can be compared directly."""
    if isinstance(obj, dict):
        return {k: _to_oracle(v) for k, v in obj.items()}
    if isinstance(obj, vdf.UInt64):
        return oracle.UINT_64(int(obj))
    if isinstance(obj, vdf.Int64):
        return oracle.INT_64(int(obj))
    return obj


def test_dumps_matches_the_oracle_byte_for_byte():
    assert vdf.dumps(EVERY_TYPE) == oracle.binary_dumps(_to_oracle(EVERY_TYPE))


def test_loads_matches_the_oracle_for_every_type():
    payload = oracle.binary_dumps(_to_oracle(EVERY_TYPE))
    parsed = vdf.loads(payload)
    assert parsed == EVERY_TYPE
    assert isinstance(parsed["root"]["a_uint64"], vdf.UInt64)
    assert isinstance(parsed["root"]["an_int64"], vdf.Int64)
    # A plain int must not be widened on the way back out.
    assert not isinstance(parsed["root"]["an_int32"], vdf.UInt64 | vdf.Int64)


def test_round_trip_of_oracle_output_is_byte_identical():
    payload = oracle.binary_dumps(_to_oracle(EVERY_TYPE))
    assert vdf.dumps(vdf.loads(payload)) == payload


def test_alt_end_marker_round_trips_and_is_detected():
    payload = oracle.binary_dumps(_to_oracle(EVERY_TYPE), alt_format=True)
    assert vdf.detect_alt_format(payload) is True
    parsed = vdf.loads(payload)
    assert parsed == EVERY_TYPE
    assert vdf.dumps(parsed, alt_format=True) == payload
    # ...and the standard marker is not mistaken for it.
    assert vdf.detect_alt_format(oracle.binary_dumps(_to_oracle(EVERY_TYPE))) is False


def test_explicit_alt_format_flag_overrides_detection():
    payload = oracle.binary_dumps({"a": {"b": "c"}}, alt_format=True)
    with pytest.raises(vdf.VdfParseError):
        vdf.loads(payload, alt_format=False)


# --- the hand-built, Steam-shaped fixture ---------------------------------


def test_committed_fixture_matches_its_generator(synthetic_shortcuts_vdf):
    """The fixture is regenerable: ``python3 tests/fixtures/build_synthetic_shortcuts.py``."""
    assert synthetic_shortcuts_vdf == builder.build()


def test_hand_built_fixture_parses_the_same_as_the_oracle(synthetic_shortcuts_vdf):
    assert vdf.loads(synthetic_shortcuts_vdf) == oracle.binary_loads(synthetic_shortcuts_vdf)


def test_hand_built_fixture_round_trips_byte_for_byte(synthetic_shortcuts_vdf):
    assert vdf.dumps(vdf.loads(synthetic_shortcuts_vdf)) == synthetic_shortcuts_vdf


def test_fixture_keeps_steams_field_order_and_quoting(synthetic_shortcuts_vdf):
    entry = vdf.loads(synthetic_shortcuts_vdf)["shortcuts"]["0"]
    assert list(entry)[:7] == [
        "appid",
        "AppName",
        "Exe",
        "StartDir",
        "icon",
        "ShortcutPath",
        "LaunchOptions",
    ]
    assert entry["Exe"].startswith('"') and entry["Exe"].endswith('"')
    assert entry["StartDir"].startswith('"')
    assert entry["LaunchOptions"] == 'launch "Elden Ring"'  # stored verbatim, no quoting


def test_real_device_fixture_round_trips(real_shortcuts_vdf):
    """Skipped until a sanitised device capture lands (see tests/fixtures/README.md)."""
    assert vdf.dumps(vdf.loads(real_shortcuts_vdf)) == real_shortcuts_vdf
    assert "shortcuts" in {k.lower() for k in vdf.loads(real_shortcuts_vdf)}


# --- edges -----------------------------------------------------------------


def test_empty_payload_is_an_empty_map():
    assert vdf.loads(b"") == {}


def test_appid_sized_ints_round_trip_through_the_signed_encoding():
    """An appid is a u32 with the high bit set; int32 storage is the same bytes."""
    unsigned = 0xFFF4A9B4
    payload = vdf.dumps({"m": {"appid": unsigned}})
    assert vdf.loads(payload)["m"]["appid"] == struct.unpack("<i", struct.pack("<I", unsigned))[0]
    assert vdf.dumps(vdf.loads(payload)) == payload


def test_non_utf8_strings_survive_the_round_trip():
    """Steam does not guarantee UTF-8 in third-party game names."""
    payload = b"\x01" + b"key\x00" + b"caf\xe9\x00" + b"\x08"
    parsed = vdf.loads(payload)
    assert vdf.dumps(parsed) == payload


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (b"\x03key\x00\x00\x00\x00\x00\x08", "unknown type byte"),
        (b"\x01key\x00value", "unterminated string"),
        (b"\x02key\x00\x01\x02\x08", "truncated int32"),
        (b"\x01key\x00value\x00", "missing end marker"),
        (b"\x01key\x00value\x00\x08\x08", "trailing data"),
        (b"\x00m\x00\x01k\x00v\x00\x08", "unclosed nested map"),
        (b"\x01k\x00a\x00\x01k\x00b\x00\x08", "duplicate key"),
    ],
)
def test_malformed_input_raises_and_returns_nothing(payload, why):
    with pytest.raises(vdf.VdfParseError):
        vdf.loads(payload)


def test_parse_errors_are_vdf_errors():
    assert issubclass(vdf.VdfParseError, vdf.VdfError)
    assert issubclass(vdf.VdfSerializeError, vdf.VdfError)


def test_deeply_nested_input_is_rejected_rather_than_blowing_the_stack():
    payload = b"".join(b"\x00k\x00" for _ in range(200)) + b"\x08" * 201
    with pytest.raises(vdf.VdfParseError):
        vdf.loads(payload)


@pytest.mark.parametrize(
    "value",
    [1.5, None, b"bytes", 2**32, -(2**31) - 1, ["a"]],
)
def test_unserialisable_values_raise(value):
    with pytest.raises(vdf.VdfSerializeError):
        vdf.dumps({"k": value})


def test_nul_inside_a_string_is_rejected():
    with pytest.raises(vdf.VdfSerializeError):
        vdf.dumps({"k": "a\x00b"})


def test_non_string_key_is_rejected():
    with pytest.raises(vdf.VdfSerializeError):
        vdf.dumps({1: "a"})


def test_loads_rejects_non_bytes():
    with pytest.raises(TypeError):
        vdf.loads("not bytes")
