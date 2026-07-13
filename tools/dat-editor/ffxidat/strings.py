"""Item strings-block parser/writer.

Each item DAT record has a strings section sitting between the item
header and the icon. It carries the display name, log names, description,
and a few numeric interpolation slots that the client splices into the
description text (e.g. defense bonus values).

Layout that this module targets (matches retail item DATs):

    offset  size   field
    0x00    4      count        (number of entries, 1..32)
    0x04    4      header_flag  (usually 0x00000001)
    0x08    N*8    entries[count] of {u32 offset, u32 kind}
                   - offset is relative to the start of the strings block
                   - kind = 0  -> string entry
                   - kind = 1  -> integer entry (used for stat numbers
                                  the client interpolates into text)

Per-entry payload at ``block[entry.offset]``:

    string entry (kind == 0):
        u32   flag    (usually 1)
        u32   length  (byte length, excluding terminator)
        char  data[length]
        u8    NUL

    integer entry (kind == 1):
        u32   value

Everything past the last entry payload is treated as padding and zeroed
out on write. The whole strings block has a fixed maximum size (600
bytes for equipment, 592 for weapons) so we always pad back to that.

If the block at a given offset doesn't validate against this layout we
give up gracefully and return ``None``; the caller then preserves the
raw bytes so the record stays intact. That way we never silently corrupt
a DAT we don't fully understand.
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


@dataclass
class StringEntry:
    kind: int  # KIND_STRING or KIND_INTEGER
    text: str = ""
    value: int = 0
    # For strings, the "flag" u32 that precedes the length. Different
    # slots use different flags (usually 1) and we preserve them so a
    # round-trip is byte-identical.
    flag: int = 1


@dataclass
class StringsBlock:
    max_size: int
    header_flag: int = 1
    entries: list[StringEntry] = field(default_factory=list)
    # If parsing failed, ``raw`` holds the original bytes so we can
    # write them back unchanged. When ``parsed`` is True, ``raw`` is
    # ignored on serialize.
    raw: bytes = b""
    parsed: bool = False

    # ----- parsing ------------------------------------------------

    @classmethod
    def parse(cls, data: bytes, max_size: int) -> "StringsBlock":
        raw = bytes(data[:max_size])
        result = cls(max_size=max_size, raw=raw, parsed=False)

        block = raw
        if len(block) < 8:
            return result

        count, header_flag = struct.unpack_from("<II", block, 0)
        if not (1 <= count <= 32):
            return result
        if header_flag > 0xFFFF:
            return result
        table_end = 8 + count * 8
        if table_end > len(block):
            return result

        entries: list[StringEntry] = []
        for i in range(count):
            offset, kind = struct.unpack_from("<II", block, 8 + i * 8)
            if kind not in (KIND_STRING, KIND_INTEGER):
                return result
            if not (table_end <= offset < len(block)):
                return result

            if kind == KIND_INTEGER:
                if offset + 4 > len(block):
                    return result
                (value,) = struct.unpack_from("<I", block, offset)
                entries.append(StringEntry(kind=KIND_INTEGER, value=value))
                continue

            # String payload: flag, length, data, terminator.
            if offset + 8 > len(block):
                return result
            flag, length = struct.unpack_from("<II", block, offset)
            data_start = offset + 8
            data_end = data_start + length
            if length > len(block) or data_end + 1 > len(block):
                return result
            payload = block[data_start:data_end]
            try:
                text = payload.decode("latin-1")
            except UnicodeDecodeError:
                return result
            entries.append(StringEntry(kind=KIND_STRING, text=text, flag=flag))

        result.header_flag = header_flag
        result.entries = entries
        result.parsed = True
        return result

    # ----- serialization -----------------------------------------

    def serialize(self) -> bytes:
        """Reserialize back to ``max_size`` bytes.

        Recomputes offsets from scratch based on current entry contents.
        Raises ``ValueError`` if the new content doesn't fit.
        """
        if not self.parsed:
            # Nothing we understood — send the raw bytes back, padded to
            # max_size in case they came in short.
            raw = self.raw[: self.max_size]
            if len(raw) < self.max_size:
                raw = raw + b"\x00" * (self.max_size - len(raw))
            return raw

        count = len(self.entries)
        out = bytearray(self.max_size)
        struct.pack_into("<II", out, 0, count, self.header_flag)

        # Payloads start right after the offset table.
        cursor = 8 + count * 8

        for i, entry in enumerate(self.entries):
            entry_offset = cursor
            if entry.kind == KIND_INTEGER:
                if cursor + 4 > self.max_size:
                    raise ValueError(
                        f"strings block overflow at entry {i} (integer)"
                    )
                struct.pack_into("<I", out, cursor, entry.value & 0xFFFFFFFF)
                cursor += 4
            elif entry.kind == KIND_STRING:
                encoded = entry.text.encode("latin-1")
                # 8-byte header (flag + length) + payload + 1 NUL.
                needed = 8 + len(encoded) + 1
                if cursor + needed > self.max_size:
                    raise ValueError(
                        f"strings block overflow at entry {i} "
                        f"({len(encoded)}-byte string, {self.max_size - cursor} avail)"
                    )
                struct.pack_into("<II", out, cursor, entry.flag, len(encoded))
                cursor += 8
                out[cursor : cursor + len(encoded)] = encoded
                cursor += len(encoded)
                # Explicit NUL terminator.
                out[cursor] = 0
                cursor += 1
            else:
                raise ValueError(f"unknown entry kind {entry.kind}")

            struct.pack_into("<II", out, 8 + i * 8, entry_offset, entry.kind)

        return bytes(out)

    # ----- convenience --------------------------------------------

    def to_dict(self) -> dict:
        return {
            "parsed": self.parsed,
            "max_size": self.max_size,
            "header_flag": self.header_flag,
            "entries": [
                {"kind": e.kind, "text": e.text, "value": e.value, "flag": e.flag}
                for e in self.entries
            ],
            "raw_hex": self.raw.hex() if not self.parsed else None,
        }

    def apply_dict(self, patch: dict) -> None:
        if not self.parsed:
            # Editing an unparsed block is not supported — we don't
            # know the format, so we can't safely rewrite it.
            return
        entries_patch = patch.get("entries")
        if entries_patch is None:
            return
        # Length must match; changing the entry count would require
        # game-side knowledge about which slot the client expects.
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
