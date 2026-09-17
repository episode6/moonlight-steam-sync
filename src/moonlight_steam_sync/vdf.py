"""Binary KeyValues (VDF) codec -- byte-identical round trip.

Steam stores ``userdata/<steamid3>/config/shortcuts.vdf`` as binary
KeyValues (spec 2.1): a stream of ``<type byte><NUL-terminated key><value>``
records, where a map's records run until an end marker.

Type bytes implemented here (spec 3.4 -- the full set that appears in
userdata files, so a field Steam adds later round-trips instead of
crashing):

===== ================================================================
byte  meaning
===== ================================================================
0x00  map: nested records until an end marker
0x01  string: NUL-terminated, UTF-8
0x02  int32, little endian, signed
0x07  uint64, little endian
0x08  end of map
0x0A  int64, little endian
0x0B  alternate end of map (some Valve writers use this instead of 0x08)
===== ================================================================

The contract the rest of the package leans on is
``dumps(loads(data)) == data`` for every file Steam writes. Keeping that
true costs three deliberate design choices:

* **Integer width is part of the value, not inferred.** A plain :class:`int`
  is an ``int32``; :class:`UInt64` and :class:`Int64` are thin ``int``
  subclasses that select the wider encodings. :func:`loads` hands back the
  same wrappers, so a value read as a uint64 is written back as one.
* **Strings use ``surrogateescape``.** Steam is not rigorous about encoding
  the names of third-party games, so a key or value that is not valid UTF-8
  would otherwise be lost. ``decode('utf-8', 'surrogateescape')`` followed by
  ``encode('utf-8', 'surrogateescape')`` reproduces arbitrary non-NUL bytes
  exactly.
* **Anything ambiguous is an error, never a guess.** Malformed input raises
  :class:`VdfParseError` and no partial structure escapes the call: the
  parser builds into a local object and only returns once the whole payload
  has been consumed. Duplicate keys inside one map are rejected too -- a
  :class:`dict` would silently drop one and break the round trip.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from typing import Any

__all__ = [
    "END",
    "END_ALT",
    "Int64",
    "UInt64",
    "VdfError",
    "VdfParseError",
    "VdfSerializeError",
    "detect_alt_format",
    "dumps",
    "loads",
]

#: Type bytes (spec 2.1 / 3.4).
TYPE_MAP = 0x00
TYPE_STRING = 0x01
TYPE_INT32 = 0x02
TYPE_UINT64 = 0x07
END = 0x08
TYPE_INT64 = 0x0A
END_ALT = 0x0B

_INT32_MIN = -(2**31)
_INT32_MAX = 2**32 - 1  # unsigned values are accepted and packed two's-complement
_UINT64_MAX = 2**64 - 1
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


class VdfError(Exception):
    """Base class for every error this module raises."""


class VdfParseError(VdfError):
    """Input is not a well-formed binary KeyValues payload."""


class VdfSerializeError(VdfError):
    """A Python object cannot be represented as binary KeyValues."""


class UInt64(int):
    """An ``int`` that encodes as the 0x07 (uint64) type."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"UInt64({int(self)})"


class Int64(int):
    """An ``int`` that encodes as the 0x0A (int64) type."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Int64({int(self)})"


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _encode(value: str) -> bytes:
    raw = value.encode("utf-8", "surrogateescape")
    if b"\x00" in raw:
        raise VdfSerializeError("NUL is not representable in a binary VDF string")
    return raw


class _Parser:
    """Single-use cursor over a binary KeyValues payload."""

    def __init__(self, data: bytes, end_byte: int) -> None:
        self._data = data
        self._end = end_byte
        self._pos = 0

    @property
    def pos(self) -> int:
        return self._pos

    def _need(self, count: int) -> None:
        if self._pos + count > len(self._data):
            raise VdfParseError(
                f"truncated binary VDF: wanted {count} byte(s) at offset {self._pos}, "
                f"only {len(self._data) - self._pos} remain"
            )

    def read_byte(self) -> int:
        self._need(1)
        value = self._data[self._pos]
        self._pos += 1
        return value

    def read_cstring(self) -> str:
        end = self._data.find(b"\x00", self._pos)
        if end < 0:
            raise VdfParseError(f"unterminated string starting at offset {self._pos}")
        raw = self._data[self._pos : end]
        self._pos = end + 1
        return _decode(raw)

    def read_struct(self, fmt: str, size: int) -> Any:
        self._need(size)
        (value,) = struct.unpack_from(fmt, self._data, self._pos)
        self._pos += size
        return value

    def read_map(self, depth: int = 0) -> dict[str, Any]:
        if depth > 64:
            raise VdfParseError("binary VDF nested deeper than 64 maps")
        result: dict[str, Any] = {}
        while True:
            type_pos = self._pos
            type_byte = self.read_byte()
            if type_byte == self._end:
                return result
            other_end = END_ALT if self._end == END else END
            if type_byte == other_end:
                raise VdfParseError(
                    f"unexpected end marker 0x{type_byte:02x} at offset {type_pos}: "
                    f"this payload terminates maps with 0x{self._end:02x}"
                )
            key = self.read_cstring()
            if key in result:
                raise VdfParseError(
                    f"duplicate key {key!r} in one map at offset {self._pos}: "
                    "keeping only one would break the byte-identical round trip"
                )
            if type_byte == TYPE_MAP:
                result[key] = self.read_map(depth + 1)
            elif type_byte == TYPE_STRING:
                result[key] = self.read_cstring()
            elif type_byte == TYPE_INT32:
                result[key] = self.read_struct("<i", 4)
            elif type_byte == TYPE_UINT64:
                result[key] = UInt64(self.read_struct("<Q", 8))
            elif type_byte == TYPE_INT64:
                result[key] = Int64(self.read_struct("<q", 8))
            else:
                raise VdfParseError(f"unknown type byte 0x{type_byte:02x} at offset {type_pos}")


def _loads_with(data: bytes, end_byte: int) -> dict[str, Any]:
    parser = _Parser(data, end_byte)
    result = parser.read_map()
    if parser.pos != len(data):
        raise VdfParseError(
            f"binary VDF ended at offset {parser.pos} but {len(data) - parser.pos} "
            "byte(s) of trailing data remain"
        )
    return result


def _loads_detect(data: bytes) -> tuple[dict[str, Any], bool]:
    """Parse *data*, trying the standard end marker first, then the alternate.

    Returns ``(parsed, alt_format)``. When neither marker yields a clean
    parse the *standard*-format error is what propagates: it is the one that
    describes the file Steam actually writes.
    """
    try:
        return _loads_with(data, END), False
    except VdfParseError as standard_error:
        try:
            return _loads_with(data, END_ALT), True
        except VdfParseError:
            raise standard_error from None


def detect_alt_format(data: bytes) -> bool:
    """Return ``True`` when *data* terminates its maps with :data:`END_ALT`.

    Detection is unambiguous: 0x0B is not a valid type byte in the standard
    format and 0x08 is not one in the alternate format, so only one of the
    two parses cleanly. An empty payload reports ``False``; input that is
    malformed under both markers raises :class:`VdfParseError`.
    """
    if not data:
        return False
    return _loads_detect(data)[1]


def loads(data: bytes, *, alt_format: bool | None = None) -> dict[str, Any]:
    """Parse a binary KeyValues payload into nested :class:`dict` objects.

    ``alt_format=None`` (the default) auto-detects the end marker; pass
    ``True``/``False`` to require :data:`END_ALT`/:data:`END`. An empty
    payload parses to an empty map, which is what an empty (or absent)
    ``shortcuts.vdf`` means for a fresh Steam user (spec 3.6).

    Raises :class:`VdfParseError` for anything malformed -- truncated data,
    an unknown type byte, an unterminated string, duplicate keys in one map,
    unbalanced end markers or trailing bytes. Nothing partial is returned.
    """
    if not isinstance(data, bytes | bytearray | memoryview):
        raise TypeError(f"loads() wants bytes, got {type(data).__name__}")
    data = bytes(data)
    if not data:
        return {}
    if alt_format is None:
        return _loads_detect(data)[0]
    return _loads_with(data, END_ALT if alt_format else END)


def _dump_map(mapping: Mapping[str, Any], out: bytearray, end_byte: int, depth: int) -> None:
    if depth > 64:
        raise VdfSerializeError("refusing to serialise a structure nested deeper than 64 maps")
    for key, value in mapping.items():
        if not isinstance(key, str):
            raise VdfSerializeError(f"binary VDF keys must be str, got {type(key).__name__}")
        encoded_key = _encode(key) + b"\x00"
        if isinstance(value, Mapping):
            out.append(TYPE_MAP)
            out += encoded_key
            _dump_map(value, out, end_byte, depth + 1)
        elif isinstance(value, str):
            out.append(TYPE_STRING)
            out += encoded_key
            out += _encode(value) + b"\x00"
        elif isinstance(value, UInt64):
            if not 0 <= value <= _UINT64_MAX:
                raise VdfSerializeError(f"uint64 out of range for key {key!r}: {int(value)}")
            out.append(TYPE_UINT64)
            out += encoded_key
            out += struct.pack("<Q", int(value))
        elif isinstance(value, Int64):
            if not _INT64_MIN <= value <= _INT64_MAX:
                raise VdfSerializeError(f"int64 out of range for key {key!r}: {int(value)}")
            out.append(TYPE_INT64)
            out += encoded_key
            out += struct.pack("<q", int(value))
        elif isinstance(value, int):
            # Plain ints (and bools, which Steam stores as 0/1) are int32.
            # Unsigned values up to 2**32-1 are accepted so callers can hand
            # over an appid (spec 2.1 makes it a u32 with the high bit set)
            # without doing the two's-complement dance themselves.
            number = int(value)
            if not _INT32_MIN <= number <= _INT32_MAX:
                raise VdfSerializeError(
                    f"int32 out of range for key {key!r}: {number} "
                    "(wrap it in UInt64/Int64 if you meant a wide integer)"
                )
            out.append(TYPE_INT32)
            out += encoded_key
            out += struct.pack("<I", number & 0xFFFFFFFF)
        else:
            raise VdfSerializeError(
                f"cannot serialise {type(value).__name__} at key {key!r}; "
                "binary VDF holds maps, strings and integers only"
            )
    out.append(end_byte)


def dumps(mapping: Mapping[str, Any], *, alt_format: bool = False) -> bytes:
    """Serialise nested mappings back to a binary KeyValues payload.

    ``alt_format=True`` terminates maps with :data:`END_ALT`; pair it with
    :func:`detect_alt_format` to round-trip a payload byte for byte::

        assert dumps(loads(raw), alt_format=detect_alt_format(raw)) == raw

    Raises :class:`VdfSerializeError` for a value binary KeyValues cannot
    represent (a float, ``None``, an out-of-range integer, a NUL inside a
    string, a non-``str`` key).
    """
    if not isinstance(mapping, Mapping):
        raise VdfSerializeError(f"dumps() wants a mapping, got {type(mapping).__name__}")
    out = bytearray()
    _dump_map(mapping, out, END_ALT if alt_format else END, 0)
    return bytes(out)
