"""Item strings-block parser/writer.

Each item DAT record has a strings section sitting between the item
header and the icon. It carries the display name, log names, description,
and a few numeric interpolation slots that the client splices into the
description text (e.g. defense bonus values).

Two on-disk layouts are supported:

**V1 (length-prefixed)** — the synthetic layout used by the tool's
tests, and the shape described in older format docs::

    u32   count
    u32   header_flag
    entry[count]:
        u32 offset   (from block start; points at the payload)
        u32 kind     (0 = string, 1 = integer)
    for each string entry, at block[entry.offset]:
        u32 flag
        u32 length
        char data[length]
        u8   NUL
    for each integer entry, at block[entry.offset]:
        u32 value

**V2 (retail)** — what real FFXI item DATs (retail and LSB-derived
private servers) use. Confirmed by decoding a live customer DAT::

    u32   header_flag  (always 0x01000000)
    u32   count
    entry[count]:
        u32 offset     (offset within the "data area" — see below)
        u32 marker_or_value
                       - marker_or_value == 0  -> string entry, next string
                                                  starts at DATA_BASE + offset
                       - marker_or_value != 0  -> integer entry with that value
    strings are null-terminated ASCII; there is no per-string length prefix.

The V2 "data area" starts ``DATA_BASE_V2`` bytes into the block. Empirically
this is fixed at 0x20 across every record we've inspected, and the first
string offset is always ``0x2C``, meaning strings begin at byte 0x4C of the
block (0x74 in absolute record coordinates for equipment).

If the block at a given offset doesn't validate against either layout we
give up gracefully and return an "unparsed" block; the caller then
preserves the raw bytes so the record stays intact. That way we never
silently corrupt a DAT whose layout we don't recognise.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

# Strings blocks in retail item DATs are 0x258 (600) bytes for
# equipment/general items and 0x250 (592) bytes for weapon records
# (because weapons steal 8 bytes for the damage/delay/etc. fields).
EQUIPMENT_STRINGS_SIZE = 0x258
WEAPON_STRINGS_SIZE = 0x250

# Sentinel kinds.
KIND_STRING = 0
KIND_INTEGER = 1

# V2-specific: the "data area" that entry offsets are relative to
# starts this many bytes into the block. Empirical.
DATA_BASE_V2 = 0x20

# V2-specific: the fixed header_flag value in every observed record.
HEADER_FLAG_V2 = 0x01000000

# V2-specific: each string in the retail layout has a `01 00 00 00`
# marker sitting this many bytes before its start position. Confirmed
# empirically against multiple records.
V2_MARKER_DISTANCE = 0x1C
V2_MARKER = b"\x01\x00\x00\x00"


@dataclass
class StringEntry:
    kind: int  # KIND_STRING or KIND_INTEGER
    text: str = ""
    value: int = 0
    # Only meaningful for V1 string entries — the per-string flag u32
    # that precedes the length. V2 doesn't use it.
    flag: int = 1


@dataclass
class StringsBlock:
    max_size: int
    header_flag: int = 1
    entries: list[StringEntry] = field(default_factory=list)
    # Which on-disk layout this block was loaded from / will save with.
    # 1 = length-prefixed synthetic layout, 2 = retail null-terminated.
    format_version: int = 1
    # V2-only: where the first string entry's virtual offset was on
    # load. Preserved on save so the string data area starts at the
    # same block-relative position as the retail layout (empirically
    # 0x2C for equipment records with 5 entries). Also record where
    # integer entries put their offset field, to keep it monotonic.
    first_string_virtual_offset: int = 0x2C
    # V2-only: the exact virtual offsets each entry had on load. When
    # a string hasn't changed size, we reuse its original slot on save
    # so the whole block is byte-identical to the input. Populated by
    # ``_parse_v2``; empty for V1.
    _original_offsets: list[int] = field(default_factory=list)
    # If parsing failed, ``raw`` holds the original bytes so we can
    # write them back unchanged. When ``parsed`` is True, ``raw`` is
    # ignored on serialize.
    raw: bytes = b""
    parsed: bool = False

    # ----- parsing ------------------------------------------------

    @classmethod
    def parse(cls, data: bytes, max_size: int) -> "StringsBlock":
        raw = bytes(data[:max_size])
        # Try the retail format first — that's what real DATs use.
        result = _parse_v2(raw, max_size)
        if result is not None:
            return result
        result = _parse_v1(raw, max_size)
        if result is not None:
            return result
        return cls(max_size=max_size, raw=raw, parsed=False, format_version=0)

    # ----- serialization -----------------------------------------

    def serialize(self) -> bytes:
        if not self.parsed:
            raw = self.raw[: self.max_size]
            if len(raw) < self.max_size:
                raw = raw + b"\x00" * (self.max_size - len(raw))
            return raw

        if self.format_version == 2:
            return _serialize_v2(self)
        return _serialize_v1(self)

    # ----- convenience --------------------------------------------

    def to_dict(self) -> dict:
        return {
            "parsed": self.parsed,
            "max_size": self.max_size,
            "header_flag": self.header_flag,
            "format_version": self.format_version,
            "entries": [
                {"kind": e.kind, "text": e.text, "value": e.value, "flag": e.flag}
                for e in self.entries
            ],
            "raw_hex": self.raw.hex() if not self.parsed else None,
        }

    def apply_dict(self, patch: dict) -> None:
        if not self.parsed:
            return
        entries_patch = patch.get("entries")
        if entries_patch is None:
            return
        if len(entries_patch) != len(self.entries):
            raise ValueError(
                f"cannot change entry count "
                f"({len(self.entries)} existing, {len(entries_patch)} in patch)"
            )
        for i, ep in enumerate(entries_patch):
            entry = self.entries[i]
            if entry.kind == KIND_STRING and "text" in ep:
                entry.text = str(ep["text"])
            if entry.kind == KIND_INTEGER and "value" in ep:
                entry.value = int(ep["value"])


# ---------------------------------------------------------------------
# V1 (length-prefixed) — original synthetic format
# ---------------------------------------------------------------------

def _parse_v1(block: bytes, max_size: int) -> Optional[StringsBlock]:
    if len(block) < 8:
        return None
    count, header_flag = struct.unpack_from("<II", block, 0)
    if not (1 <= count <= 32):
        return None
    if header_flag > 0xFFFF:
        return None
    table_end = 8 + count * 8
    if table_end > len(block):
        return None

    entries: list[StringEntry] = []
    for i in range(count):
        offset, kind = struct.unpack_from("<II", block, 8 + i * 8)
        if kind not in (KIND_STRING, KIND_INTEGER):
            return None
        if not (table_end <= offset < len(block)):
            return None

        if kind == KIND_INTEGER:
            if offset + 4 > len(block):
                return None
            (value,) = struct.unpack_from("<I", block, offset)
            entries.append(StringEntry(kind=KIND_INTEGER, value=value))
            continue

        if offset + 8 > len(block):
            return None
        flag, length = struct.unpack_from("<II", block, offset)
        data_start = offset + 8
        data_end = data_start + length
        if length > len(block) or data_end + 1 > len(block):
            return None
        payload = block[data_start:data_end]
        try:
            text = payload.decode("latin-1")
        except UnicodeDecodeError:
            return None
        entries.append(StringEntry(kind=KIND_STRING, text=text, flag=flag))

    return StringsBlock(
        max_size=max_size,
        header_flag=header_flag,
        entries=entries,
        format_version=1,
        raw=bytes(block[:max_size]),
        parsed=True,
    )


def _serialize_v1(sb: StringsBlock) -> bytes:
    count = len(sb.entries)
    out = bytearray(sb.max_size)
    struct.pack_into("<II", out, 0, count, sb.header_flag)
    cursor = 8 + count * 8

    for i, entry in enumerate(sb.entries):
        entry_offset = cursor
        if entry.kind == KIND_INTEGER:
            if cursor + 4 > sb.max_size:
                raise ValueError(f"strings block overflow at entry {i} (integer)")
            struct.pack_into("<I", out, cursor, entry.value & 0xFFFFFFFF)
            cursor += 4
        elif entry.kind == KIND_STRING:
            encoded = entry.text.encode("latin-1")
            needed = 8 + len(encoded) + 1
            if cursor + needed > sb.max_size:
                raise ValueError(
                    f"strings block overflow at entry {i} "
                    f"({len(encoded)}-byte string, {sb.max_size - cursor} avail)"
                )
            struct.pack_into("<II", out, cursor, entry.flag, len(encoded))
            cursor += 8
            out[cursor : cursor + len(encoded)] = encoded
            cursor += len(encoded)
            out[cursor] = 0
            cursor += 1
        else:
            raise ValueError(f"unknown entry kind {entry.kind}")

        struct.pack_into("<II", out, 8 + i * 8, entry_offset, entry.kind)

    return bytes(out)


# ---------------------------------------------------------------------
# V2 (null-terminated, retail) — the real FFXI format
# ---------------------------------------------------------------------

def _parse_v2(block: bytes, max_size: int) -> Optional[StringsBlock]:
    if len(block) < 8:
        return None
    header_flag, count = struct.unpack_from("<II", block, 0)
    # V2 is identified by its header_flag; anything else falls through
    # to the V1 attempt.
    if header_flag != HEADER_FLAG_V2:
        return None
    if not (1 <= count <= 32):
        return None
    table_end = 8 + count * 8
    if table_end > len(block):
        return None

    entries: list[StringEntry] = []
    original_offsets: list[int] = []
    first_string_virtual_offset = None
    for i in range(count):
        offset, marker = struct.unpack_from("<II", block, 8 + i * 8)
        original_offsets.append(offset)
        if marker == 0:
            # String — null-terminated at DATA_BASE_V2 + offset.
            start = DATA_BASE_V2 + offset
            if not (table_end <= start < len(block)):
                return None
            end = start
            while end < len(block) and block[end] != 0:
                end += 1
            try:
                text = block[start:end].decode("latin-1")
            except UnicodeDecodeError:
                return None
            entries.append(StringEntry(kind=KIND_STRING, text=text))
            if first_string_virtual_offset is None:
                first_string_virtual_offset = offset
        else:
            # Integer — value is stored inline in the entry's second u32.
            entries.append(StringEntry(kind=KIND_INTEGER, value=marker))

    return StringsBlock(
        max_size=max_size,
        header_flag=header_flag,
        entries=entries,
        format_version=2,
        first_string_virtual_offset=first_string_virtual_offset or 0x2C,
        _original_offsets=original_offsets,
        raw=bytes(block[:max_size]),
        parsed=True,
    )


def _serialize_v2(sb: StringsBlock) -> bytes:
    """Rewrite the strings block in the retail V2 layout.

    Layout preserved on save:
      - header {u32 flag, u32 count} at block start
      - entries {u32 offset, u32 marker_or_value}[count] at block+8
      - the first string sits at ``first_string_virtual_offset`` (relative
        to DATA_BASE_V2) — this is what the retail layout uses (0x2C for
        equipment records with 5 entries) and we preserve it so the
        emitted block matches the shape the game client is used to.

    Integer entries carry their value inline in the entry's second u32,
    so their `offset` field is a bookkeeping value only. To keep entry
    offsets monotonically increasing, we thread integer offsets right
    through the middle of the sequence.
    """
    count = len(sb.entries)
    out = bytearray(sb.max_size)
    struct.pack_into("<II", out, 0, sb.header_flag or HEADER_FLAG_V2, count)

    entries_end = 8 + count * 8
    first_virtual_offset = max(
        sb.first_string_virtual_offset,
        entries_end - DATA_BASE_V2,
    )
    # Align to 4 bytes to match retail's convention.
    first_virtual_offset = (first_virtual_offset + 3) & ~3

    # First pass: assign each string entry a slot. Retail records
    # pack strings at fixed positions matching the original's layout;
    # to preserve the shape, use the original entry offsets from
    # ``sb._original_offsets`` when they still fit, otherwise fall
    # back to packing tightly after the previous string.
    string_offsets: list[Optional[int]] = [None] * count
    cursor_virtual = first_virtual_offset
    for i, entry in enumerate(sb.entries):
        if entry.kind != KIND_STRING:
            continue
        encoded = entry.text.encode("latin-1")
        needed = len(encoded) + 1  # trailing NUL
        # Use the original slot if it still fits, otherwise pack tightly.
        original = (
            sb._original_offsets[i]
            if i < len(sb._original_offsets)
            else None
        )
        if original is not None and original >= cursor_virtual:
            # Padding between cursor_virtual and original stays as
            # NUL bytes (out was pre-zeroed).
            cursor_virtual = original
        abs_pos = DATA_BASE_V2 + cursor_virtual
        end_pos = abs_pos + needed
        if end_pos > sb.max_size:
            raise ValueError(
                f"strings block overflow at entry {i} "
                f"({len(encoded)}-byte string, {sb.max_size - abs_pos} avail)"
            )
        string_offsets[i] = cursor_virtual
        out[abs_pos : abs_pos + len(encoded)] = encoded
        out[abs_pos + len(encoded)] = 0
        # Per-slot marker sits V2_MARKER_DISTANCE bytes before the
        # string. Skip it if that position would land inside the
        # entry table.
        marker_pos = abs_pos - V2_MARKER_DISTANCE
        if marker_pos >= 8 + count * 8:
            out[marker_pos : marker_pos + 4] = V2_MARKER
        cursor_virtual += needed

    # Second pass: write the entry table. Integer entries get
    # ``next_string.offset - 4`` per the retail convention.
    def _next_string_offset(from_idx: int) -> int:
        for j in range(from_idx + 1, count):
            if sb.entries[j].kind == KIND_STRING:
                return string_offsets[j] or 0
        # No later string; use current cursor.
        return cursor_virtual

    for i, entry in enumerate(sb.entries):
        e_off = 8 + i * 8
        if entry.kind == KIND_INTEGER:
            int_virtual_offset = max(0, _next_string_offset(i) - 4)
            struct.pack_into(
                "<II", out, e_off, int_virtual_offset, entry.value & 0xFFFFFFFF
            )
        elif entry.kind == KIND_STRING:
            struct.pack_into("<II", out, e_off, string_offsets[i] or 0, 0)

    return bytes(out)
