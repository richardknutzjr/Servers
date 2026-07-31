"""Item DAT parsing.

This module treats the fixed header of an item record as strongly typed
data and preserves the rest of the record verbatim. That's on purpose:
the strings/icon/scripts region of the record has a variable, per-item
layout that is easy to corrupt if you rewrite bytes you didn't fully
understand. Keeping the tail opaque means edits touch only the fields
we know, and unknown data survives a round-trip unchanged.

Record layout used here (matches retail equipment):

    offset  size  field
    0x00    4     id (uint32, little-endian)
    0x04    2     flags
    0x06    2     stack_size
    0x08    2     item_type
    0x0A    2     resource_id
    0x0C    2     valid_targets
    0x0E    2     level (min level to equip)
    0x10    2     slots (bitmask; see flags.Slot)
    0x12    2     races (bitmask; see flags.Race)
    0x14    4     jobs (bitmask; see flags.Job)
    0x18    2     superior_level (item level)
    0x1A    2     shield_size / ranged skill
    0x1C    1     max_charges
    0x1D    1     casting_time
    0x1E    2     use_delay
    0x20    4     reuse_delay
    0x24    4     model / unknown
    -- weapon-only extras --
    0x28    2     damage
    0x2A    2     delay
    0x2C    2     dps
    0x2E    1     skill
    0x2F    1     jug_size

The rest (0x30..end-of-record) is icon data + string tables + padding.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field, asdict
from enum import IntEnum
from pathlib import Path
from typing import Iterable, Optional

from .cipher import decode_bytes, encode_bytes
from .flags import Slot, Race, Job, bitmask_to_names, names_to_bitmask
from .strings import (
    EQUIPMENT_STRINGS_SIZE,
    KIND_INTEGER,
    KIND_STRING,
    StringsBlock,
    WEAPON_STRINGS_SIZE,
)

# 0xC00 - the retail record size for item_*.DAT files.
RECORD_SIZE = 0xC00

# The icon always sits at absolute offset 0x280 in an item record; that
# offset is 0x258 into the ``tail`` buffer we keep on Item.
_ICON_OFFSET_IN_TAIL = 0x258


class ItemType(IntEnum):
    NONE = 0
    QUEST = 1
    ITEM = 2
    WEAPON = 4
    ARMOR = 5
    LINKSHELL = 6
    USABLE = 7
    CRYSTAL = 8
    CURRENCY = 9
    FURNISHING = 10
    PLANT = 11
    FLOWERPOT = 12
    PUPPET_ITEM = 14
    MANNEQUIN = 15
    BOOK = 16
    RACING_FORM = 17
    BETTING_SLIP = 18
    SOULPLATE = 19
    RESERVER = 20
    STORAGE_SLIP = 21
    LEGENDS_CARD = 22
    MOG_TABLET = 23

    @classmethod
    def name_of(cls, value: int) -> str:
        try:
            return cls(value).name
        except ValueError:
            return f"UNKNOWN({value})"


# The struct format for the first 0x28 bytes of an item record. Little
# endian, no padding.
_HEADER_FMT = "<IHHHHHHHHIHHBBHIH2s"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
assert _HEADER_SIZE == 0x28, _HEADER_SIZE

# Weapon-only fields: dmg, delay, dps, skill, jug_size (0x28..0x30).
_WEAPON_FMT = "<HHHBB"
_WEAPON_SIZE = struct.calcsize(_WEAPON_FMT)
assert _WEAPON_SIZE == 0x08, _WEAPON_SIZE


@dataclass
class Item:
    id: int = 0
    flags: int = 0
    stack_size: int = 1
    item_type: int = int(ItemType.ARMOR)
    resource_id: int = 0
    valid_targets: int = 0
    level: int = 1
    slots: int = 0
    races: int = int(Race.ALL)
    jobs: int = 0
    superior_level: int = 0
    shield_size: int = 0
    max_charges: int = 0
    casting_time: int = 0
    use_delay: int = 0
    reuse_delay: int = 0
    model: int = 0
    unknown_26: bytes = b"\x00\x00"

    # Weapon-only fields (present when item_type == WEAPON). For non-
    # weapons these come from the tail bytes and are surfaced as zeros.
    damage: int = 0
    delay: int = 0
    dps: int = 0
    skill: int = 0
    jug_size: int = 0

    # Everything after the header: icon, strings, scripts, padding. Kept
    # verbatim so unknown regions survive a round-trip untouched.
    tail: bytes = field(default=b"", repr=False)

    # Best-effort preview strings pulled from the tail as a
    # last-resort fallback if we couldn't parse the structured strings
    # block. See extract_strings() below.
    strings: list[str] = field(default_factory=list)

    # Same run of extracted preview strings as ``strings``, but with the
    # byte offset (relative to ``tail``) and length of each run. Used by
    # the raw-string editor to safely rewrite individual strings in-place
    # for items whose strings block we couldn't fully parse.
    strings_meta: list[dict] = field(default_factory=list)

    # Structured strings block (name, log names, description). Editable
    # when ``strings_block.parsed`` is True; otherwise treated as opaque
    # and passed through unchanged on save.
    strings_block: Optional[StringsBlock] = None

    # The actual offset (within tail) where the strings block was found.
    # Equipment/weapons use fixed offsets, but general items (BOOK,
    # USABLE, currency, etc.) put their strings block earlier, at a
    # per-type offset we detect at parse time. Remember it so we can
    # splice back at the same place on save.
    strings_offset: int = 0
    strings_max_size: int = 0

    @property
    def is_weapon(self) -> bool:
        return self.item_type == int(ItemType.WEAPON)

    @property
    def _strings_offset_in_tail(self) -> int:
        # Weapon records claim the first 8 bytes of tail for damage/
        # delay/etc. before the strings block starts.
        return _WEAPON_SIZE if self.is_weapon else 0

    @property
    def _strings_max_size(self) -> int:
        return WEAPON_STRINGS_SIZE if self.is_weapon else EQUIPMENT_STRINGS_SIZE

    @property
    def slot_names(self) -> list[str]:
        return bitmask_to_names(self.slots, Slot)

    @property
    def race_names(self) -> list[str]:
        return bitmask_to_names(self.races, Race)

    @property
    def job_names(self) -> list[str]:
        return bitmask_to_names(self.jobs, Job)

    @property
    def item_type_name(self) -> str:
        return ItemType.name_of(self.item_type)

    @property
    def name(self) -> str:
        """Best guess at the display name.

        Prefers the first string in the structured strings block when
        we successfully parsed one; falls back to the heuristic ASCII
        extractor for records whose strings block we couldn't parse.
        """
        if self.strings_block and self.strings_block.parsed:
            for entry in self.strings_block.entries:
                if entry.kind == KIND_STRING and entry.text.strip():
                    return entry.text.strip()
        # No structured strings block. Use the ASCII heuristic only for
        # runs that actually look like a name (at least 3 alphabetic
        # characters and mostly letters) so we don't display gibberish
        # like ":E>" as the "name".
        for candidate in self.strings:
            s = candidate.strip()
            if len(s) < 3:
                continue
            letters = sum(1 for ch in s if ch.isalpha())
            if letters >= 3 and letters / len(s) >= 0.5:
                return s
        return f"{self.item_type_name.lower()}_{self.id}"

    @property
    def description(self) -> str:
        """Best guess at the description text.

        By retail convention, the description is one of the last string
        entries in the block. We pick the longest string that contains a
        space (heuristic: descriptions are sentences, log-names aren't).
        """
        if not (self.strings_block and self.strings_block.parsed):
            return ""
        best = ""
        for entry in self.strings_block.entries:
            if entry.kind != KIND_STRING:
                continue
            if " " in entry.text and len(entry.text) > len(best):
                best = entry.text
        return best

    # -- serialization -------------------------------------------------

    @classmethod
    def from_plain(cls, plain: bytes) -> "Item":
        """Parse a decoded (rotation-decoded) record."""
        header = struct.unpack_from(_HEADER_FMT, plain, 0)
        item = cls(
            id=header[0],
            flags=header[1],
            stack_size=header[2],
            item_type=header[3],
            resource_id=header[4],
            valid_targets=header[5],
            level=header[6],
            slots=header[7],
            races=header[8],
            jobs=header[9],
            superior_level=header[10],
            shield_size=header[11],
            max_charges=header[12],
            casting_time=header[13],
            use_delay=header[14],
            reuse_delay=header[15],
            model=header[16],
            unknown_26=header[17],
            tail=bytes(plain[_HEADER_SIZE:]),
        )
        if item.is_weapon and len(item.tail) >= _WEAPON_SIZE:
            dmg, delay, dps, skill, jug = struct.unpack_from(_WEAPON_FMT, item.tail, 0)
            item.damage = dmg
            item.delay = delay
            item.dps = dps
            item.skill = skill
            item.jug_size = jug

        # Locate the strings block. Equipment/weapons use fixed
        # offsets; general items (BOOK, USABLE, currency, etc.) put
        # their strings block much earlier — inside what the equipment
        # layout treats as "header". Scan candidate absolute offsets
        # and take the first that decodes cleanly.
        found = _find_strings_block(plain, item.is_weapon)
        if found is not None:
            offset, block, size = found
            item.strings_offset = offset
            item.strings_max_size = size
            item.strings_block = block

        item.strings_meta = extract_strings(item.tail)
        item.strings = [m["text"] for m in item.strings_meta]
        return item

    def to_plain(self, record_size: int = RECORD_SIZE) -> bytes:
        """Serialise back to a decoded (unrotated) record of ``record_size``."""
        unknown = self.unknown_26
        if len(unknown) != 2:
            unknown = (unknown + b"\x00\x00")[:2]

        header = struct.pack(
            _HEADER_FMT,
            self.id & 0xFFFFFFFF,
            self.flags & 0xFFFF,
            self.stack_size & 0xFFFF,
            self.item_type & 0xFFFF,
            self.resource_id & 0xFFFF,
            self.valid_targets & 0xFFFF,
            self.level & 0xFFFF,
            self.slots & 0xFFFF,
            self.races & 0xFFFF,
            self.jobs & 0xFFFFFFFF,
            self.superior_level & 0xFFFF,
            self.shield_size & 0xFFFF,
            self.max_charges & 0xFF,
            self.casting_time & 0xFF,
            self.use_delay & 0xFFFF,
            self.reuse_delay & 0xFFFFFFFF,
            self.model & 0xFFFF,
            unknown,
        )

        tail = bytearray(self.tail)
        if self.is_weapon:
            # Grow tail if needed so we can splice weapon fields in.
            if len(tail) < _WEAPON_SIZE:
                tail.extend(b"\x00" * (_WEAPON_SIZE - len(tail)))
            struct.pack_into(
                _WEAPON_FMT,
                tail,
                0,
                self.damage & 0xFFFF,
                self.delay & 0xFFFF,
                self.dps & 0xFFFF,
                self.skill & 0xFF,
                self.jug_size & 0xFF,
            )

        record = bytearray(header + bytes(tail))

        # Splice the (possibly edited) strings block back at its
        # absolute offset. For non-equipment items this overwrites
        # equipment-specific header bytes at 0x0E-0x27 — that's correct,
        # because those bytes were the strings block in the source file
        # (they only look like level/slots/etc. because we parsed with
        # the equipment layout).
        if self.strings_block is not None and self.strings_max_size > 0:
            offset = self.strings_offset
            size = self.strings_max_size
            if len(record) < offset + size:
                record.extend(b"\x00" * (offset + size - len(record)))
            record[offset : offset + size] = self.strings_block.serialize()

        record = bytes(record)
        if len(record) < record_size:
            record += b"\x00" * (record_size - len(record))
        elif len(record) > record_size:
            record = record[:record_size]
        return record

    def to_dict(self) -> dict:
        d = asdict(self)
        # Drop bytes fields; expose hex for the poking-around case.
        d["tail"] = self.tail.hex()
        d["unknown_26"] = self.unknown_26.hex()
        d["slot_names"] = self.slot_names
        d["race_names"] = self.race_names
        d["job_names"] = self.job_names
        d["item_type_name"] = self.item_type_name
        d["name"] = self.name
        d["description"] = self.description
        d["is_weapon"] = self.is_weapon
        d["strings_block"] = self.strings_block.to_dict() if self.strings_block else None
        d["strings_offset"] = self.strings_offset
        d["strings_meta"] = list(self.strings_meta)
        return d

    def apply_dict(self, patch: dict) -> None:
        """Overlay a JSON-style patch onto this item.

        Recognises the bitmask *_names lists in addition to raw integer
        fields, so callers can send either representation.
        """
        for scalar in (
            "id",
            "flags",
            "stack_size",
            "item_type",
            "resource_id",
            "valid_targets",
            "level",
            "superior_level",
            "shield_size",
            "max_charges",
            "casting_time",
            "use_delay",
            "reuse_delay",
            "model",
            "damage",
            "delay",
            "dps",
            "skill",
            "jug_size",
        ):
            if scalar in patch and patch[scalar] is not None:
                setattr(self, scalar, int(patch[scalar]))

        if "slots" in patch and patch["slots"] is not None:
            self.slots = int(patch["slots"])
        if "slot_names" in patch:
            self.slots = names_to_bitmask(patch["slot_names"], Slot)
        if "races" in patch and patch["races"] is not None:
            self.races = int(patch["races"])
        if "race_names" in patch:
            self.races = names_to_bitmask(patch["race_names"], Race)
        if "jobs" in patch and patch["jobs"] is not None:
            self.jobs = int(patch["jobs"])
        if "job_names" in patch:
            self.jobs = names_to_bitmask(patch["job_names"], Job)

        # Strings block: nested edits go through StringsBlock.apply_dict.
        if "strings_block" in patch and self.strings_block is not None and patch["strings_block"]:
            self.strings_block.apply_dict(patch["strings_block"])

        # Raw-string edits for items whose structured strings block we
        # couldn't parse: rewrite individual bytes at known preview
        # offsets. See apply_raw_string_edits() for the safety rules.
        if "raw_string_edits" in patch and patch["raw_string_edits"]:
            self.apply_raw_string_edits(patch["raw_string_edits"])

    def apply_raw_string_edits(self, edits: list[dict]) -> list[dict]:
        """Rewrite specific runs of preview-extracted strings in ``tail``.

        Each edit is ``{"offset": int, "text": str}``. The offset must
        match one recorded in ``strings_meta`` (i.e. one of the runs the
        preview extractor found). The new text is encoded latin-1 and
        must be no longer than the original run's byte length — shorter
        values are padded out with NUL bytes so nothing downstream in
        the tail shifts position.

        This is the fallback edit path for weapons and other items
        whose strings block layout we don't fully understand: we can
        safely overwrite bytes we can pinpoint exactly, but rejecting
        length-growing edits keeps us from corrupting whatever follows.
        """
        applied: list[dict] = []
        new_tail = bytearray(self.tail)
        for edit in edits:
            offset = int(edit["offset"])
            new_text = str(edit.get("text", ""))
            meta = next(
                (m for m in self.strings_meta if int(m["offset"]) == offset),
                None,
            )
            if meta is None:
                raise ValueError(
                    f"no preview string at tail offset {offset}; "
                    f"cannot safely edit an unknown byte range"
                )
            current_length = int(meta["length"])
            encoded = new_text.encode("latin-1")
            if len(encoded) > current_length:
                raise ValueError(
                    f"new value is {len(encoded)} bytes but the original "
                    f"string at offset {offset} is only {current_length} "
                    f"bytes; shorten it or leave the extra space blank"
                )
            padded = encoded + b"\x00" * (current_length - len(encoded))
            if offset + current_length > len(new_tail):
                raise ValueError(
                    f"offset {offset}+{current_length} runs past end of tail"
                )
            new_tail[offset : offset + current_length] = padded
            meta["text"] = new_text
            applied.append({
                "offset": offset,
                "text": new_text,
                "length": current_length,
            })
        self.tail = bytes(new_tail)
        # Keep the flat ``strings`` list in sync with strings_meta.
        self.strings = [m["text"] for m in self.strings_meta]
        return applied


def _looks_like_real_text(block: StringsBlock) -> bool:
    """A parsed StringsBlock at a candidate offset only counts if the
    entries look like human-readable strings — not just bytes that
    happened to satisfy the layout constraints. Real item names contain
    at least a few letters."""
    if not block.parsed or not block.entries:
        return False
    for entry in block.entries:
        if entry.kind != KIND_STRING:
            continue
        text = entry.text.strip()
        if not text:
            continue
        letters = sum(1 for ch in text if ch.isalpha())
        if letters >= 3 and letters / max(len(text), 1) >= 0.5:
            return True
    return False


# Offsets that FFXI item DATs park their strings block at, in
# best-match order. The first entry is the standard equipment offset;
# the rest cover weapons and the various general-item sub-formats
# (BOOK, USABLE, currency, etc.).
_STRINGS_OFFSET_CANDIDATES = (
    0x28,  # equipment (armor, general items with equipment-shaped header)
    0x30,  # weapon
    0x14,  # general item variant 1
    0x18,  # general item variant 2
    0x1C,  # general item variant 3
    0x20,  # general item variant 4
    0x24,  # general item variant 5
    0x0E,  # tightly-packed variant
    0x10,
    0x12,
)


def _find_strings_block(record: bytes, weapon: bool):
    """Return ``(offset, block, size)`` for the strings block, or ``None``.

    Offsets are absolute (relative to the start of the plaintext
    record). Equipment/weapon layouts get their standard offset tried
    first; the rest of the candidates cover general items whose strings
    block starts INSIDE what the equipment layout calls "header".
    """
    ordered = list(_STRINGS_OFFSET_CANDIDATES)
    if weapon and 0x30 in ordered:
        ordered.remove(0x30)
        ordered.insert(0, 0x30)

    # Icon typically lives at 0x280 in the record. Cap the strings
    # region there so a bad match doesn't reach into icon bytes.
    icon_start = 0x280

    for offset in ordered:
        if offset + 8 > len(record):
            continue
        avail = icon_start - offset
        if avail < 8:
            continue
        avail = min(avail, len(record) - offset)
        block = StringsBlock.parse(record[offset : offset + avail], max_size=avail)
        if block.parsed and _looks_like_real_text(block):
            return offset, block, avail
    return None


def extract_strings(tail: bytes, min_len: int = 3) -> list[dict]:
    """Return every ASCII/latin-1 run of printable bytes in the tail.

    Each entry is ``{"offset": int, "text": str, "length": int}`` with
    ``offset`` measured from the start of ``tail`` and ``length`` the
    byte length of the run (equals ``len(text)`` for latin-1, which
    every FFXI item DAT uses).

    Used both as a preview when the structured strings block parser
    fails, and as the source of truth for the raw-string editor that
    lets users rewrite individual strings in-place.
    """
    out: list[dict] = []
    run_start = 0
    current: list[int] = []
    for i, b in enumerate(tail):
        if 0x20 <= b < 0x7F:
            if not current:
                run_start = i
            current.append(b)
        else:
            if len(current) >= min_len:
                out.append({
                    "offset": run_start,
                    "text": bytes(current).decode("latin-1"),
                    "length": len(current),
                })
            current = []
    if len(current) >= min_len:
        out.append({
            "offset": run_start,
            "text": bytes(current).decode("latin-1"),
            "length": len(current),
        })
    return out


@dataclass
class ItemDat:
    """A whole item_*.DAT file split into fixed-size records."""

    path: Path | None
    record_size: int
    items: list[Item] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str, record_size: int = RECORD_SIZE) -> "ItemDat":
        path = Path(path)
        raw = path.read_bytes()
        if len(raw) % record_size:
            raise ValueError(
                f"{path}: size {len(raw)} is not a multiple of record size {record_size}. "
                "Pass --record-size to override if this DAT uses a different layout."
            )
        items = []
        for offset in range(0, len(raw), record_size):
            record = raw[offset : offset + record_size]
            plain = decode_bytes(record)
            items.append(Item.from_plain(plain))
        return cls(path=path, record_size=record_size, items=items)

    @classmethod
    def from_records(cls, plaintext_records: Iterable[bytes], record_size: int = RECORD_SIZE) -> "ItemDat":
        items = [Item.from_plain(r) for r in plaintext_records]
        return cls(path=None, record_size=record_size, items=items)

    def to_bytes(self) -> bytes:
        return b"".join(encode_bytes(item.to_plain(self.record_size)) for item in self.items)

    def save(self, path: Path | str | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("ItemDat has no path; pass one explicitly")
        target.write_bytes(self.to_bytes())
        return target
