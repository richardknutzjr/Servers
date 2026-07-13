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

    # Structured strings block (name, log names, description). Editable
    # when ``strings_block.parsed`` is True; otherwise treated as opaque
    # and passed through unchanged on save.
    strings_block: Optional[StringsBlock] = None

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
        for s in self.strings:
            if s.strip():
                return s.strip()
        return f"item_{self.id}"

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

        # Parse the structured strings block if there's room for one.
        strings_offset = item._strings_offset_in_tail
        strings_size = item._strings_max_size
        if len(item.tail) >= strings_offset + strings_size:
            item.strings_block = StringsBlock.parse(
                item.tail[strings_offset : strings_offset + strings_size],
                max_size=strings_size,
            )

        item.strings = extract_strings(item.tail)
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

        # Splice the (possibly edited) strings block back over its slot
        # in the tail. If we never parsed one, this is a no-op — the
        # existing bytes were preserved verbatim.
        if self.strings_block is not None:
            strings_offset = self._strings_offset_in_tail
            strings_size = self._strings_max_size
            if len(tail) < strings_offset + strings_size:
                tail.extend(b"\x00" * (strings_offset + strings_size - len(tail)))
            tail[strings_offset : strings_offset + strings_size] = (
                self.strings_block.serialize()
            )

        record = header + bytes(tail)
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


def extract_strings(tail: bytes, min_len: int = 3) -> list[str]:
    """Pull ASCII/latin-1 run-of-printable strings out of the tail.

    This is a heuristic preview so the UI can label items. It is NOT a
    full FFXI dialog-table decoder; editing item names still requires
    working on the tail bytes directly.
    """
    out: list[str] = []
    current: list[int] = []
    for b in tail:
        if 0x20 <= b < 0x7F:
            current.append(b)
        else:
            if len(current) >= min_len:
                out.append(bytes(current).decode("latin-1"))
            current = []
    if len(current) >= min_len:
        out.append(bytes(current).decode("latin-1"))
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
