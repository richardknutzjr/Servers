"""End-to-end tests that exercise the strings block through the Item API.

Synthesise a full item record with a well-formed strings block, parse it,
edit the strings, save, re-parse, and confirm the edits survive.
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ffxidat import Item, ItemDat, ItemType, Job, RECORD_SIZE, Race, Slot  # noqa: E402
from ffxidat.items import _HEADER_FMT, _WEAPON_SIZE  # noqa: E402
from ffxidat.strings import (  # noqa: E402
    EQUIPMENT_STRINGS_SIZE,
    KIND_INTEGER,
    KIND_STRING,
    StringEntry,
    StringsBlock,
    WEAPON_STRINGS_SIZE,
)


def _pack_strings(entries: list[StringEntry], size: int) -> bytes:
    """Pack strings into a strings block of exactly ``size`` bytes."""
    block = StringsBlock(max_size=size, header_flag=1, entries=entries, parsed=True)
    return block.serialize()


def make_record(
    item_id: int,
    item_type: int = int(ItemType.ARMOR),
    strings: list[StringEntry] | None = None,
) -> bytes:
    """Build a plaintext item record with a valid strings block."""
    header = struct.pack(
        _HEADER_FMT,
        item_id,
        0,                              # flags
        1,                              # stack_size
        item_type,
        0,                              # resource_id
        0,                              # valid_targets
        1,                              # level
        int(Slot.HEAD) if item_type == int(ItemType.ARMOR) else int(Slot.MAIN),
        int(Race.ALL),
        int(Job.WAR),
        0, 0, 0, 0, 0, 0, 0, b"\x00\x00",
    )
    tail = bytearray(RECORD_SIZE - len(header))

    if item_type == int(ItemType.WEAPON):
        # Weapon extras occupy the first 8 bytes of the tail.
        struct.pack_into("<HHHBB", tail, 0, 10, 240, 41, 1, 0)
        strings_offset = _WEAPON_SIZE
        strings_size = WEAPON_STRINGS_SIZE
    else:
        strings_offset = 0
        strings_size = EQUIPMENT_STRINGS_SIZE

    if strings is None:
        strings = [
            StringEntry(kind=KIND_STRING, text=f"Test Item {item_id}"),
            StringEntry(kind=KIND_STRING, text="test item"),
            StringEntry(kind=KIND_STRING, text="test items"),
            StringEntry(kind=KIND_STRING, text="A synthesised item for tests."),
        ]

    strings_bytes = _pack_strings(strings, strings_size)
    tail[strings_offset : strings_offset + strings_size] = strings_bytes

    # Stamp a marker into the icon region so we can prove it survived.
    tail[0x258 : 0x258 + 4] = b"\xDE\xAD\xBE\xEF"
    return header + bytes(tail)


class EquipmentTests(unittest.TestCase):
    def test_parses_and_exposes_name(self):
        plain = make_record(item_id=15100)
        item = Item.from_plain(plain)
        self.assertIsNotNone(item.strings_block)
        self.assertTrue(item.strings_block.parsed)
        self.assertEqual(item.name, "Test Item 15100")
        self.assertEqual(item.description, "A synthesised item for tests.")

    def test_edit_name_and_description_roundtrip(self):
        plain = make_record(item_id=15101)
        item = Item.from_plain(plain)

        # Repurpose the item: new name, new description.
        item.apply_dict(
            {
                "strings_block": {
                    "entries": [
                        {"text": "Void Crown"},
                        {"text": "void crown"},
                        {"text": "void crowns"},
                        {"text": "A crown of dark energy. DEF:99 Vit +5"},
                    ]
                }
            }
        )
        self.assertEqual(item.name, "Void Crown")

        # Serialize the whole record, re-parse from bytes, verify.
        rebuilt = item.to_plain()
        item2 = Item.from_plain(rebuilt)
        self.assertEqual(item2.name, "Void Crown")
        self.assertEqual(item2.description, "A crown of dark energy. DEF:99 Vit +5")
        self.assertEqual(item2.strings_block.entries[2].text, "void crowns")

        # And the icon marker survives untouched.
        self.assertEqual(rebuilt[0x280 : 0x280 + 4], b"\xDE\xAD\xBE\xEF")

    def test_full_dat_roundtrip_with_string_edits(self):
        records = [make_record(item_id=i) for i in range(16000, 16003)]
        dat = ItemDat.from_records(records)
        dat.items[1].apply_dict(
            {
                "strings_block": {
                    "entries": [
                        {"text": "Godslayer"},
                        {"text": "godslayer"},
                        {"text": "godslayers"},
                        {"text": "Cleaves through anything."},
                    ]
                }
            }
        )
        encoded = dat.to_bytes()
        # Re-parse via the file-level loader.
        from ffxidat.cipher import decode_bytes
        reparsed = ItemDat.from_records(
            [decode_bytes(encoded[i : i + RECORD_SIZE]) for i in range(0, len(encoded), RECORD_SIZE)]
        )
        self.assertEqual(reparsed.items[1].name, "Godslayer")
        # Neighbours untouched.
        self.assertEqual(reparsed.items[0].name, "Test Item 16000")
        self.assertEqual(reparsed.items[2].name, "Test Item 16002")


class WeaponTests(unittest.TestCase):
    def test_weapon_strings_offset(self):
        plain = make_record(
            item_id=17000,
            item_type=int(ItemType.WEAPON),
            strings=[
                StringEntry(kind=KIND_STRING, text="Ridill"),
                StringEntry(kind=KIND_STRING, text="a ridill"),
                StringEntry(kind=KIND_STRING, text="A famous sword."),
            ],
        )
        item = Item.from_plain(plain)
        self.assertTrue(item.is_weapon)
        self.assertIsNotNone(item.strings_block)
        self.assertTrue(item.strings_block.parsed)
        self.assertEqual(item.name, "Ridill")
        # Weapon fields survive the strings work.
        self.assertEqual(item.damage, 10)
        self.assertEqual(item.delay, 240)

        # Edit the strings and confirm both strings AND weapon fields
        # round-trip cleanly.
        item.apply_dict(
            {
                "damage": 55,
                "strings_block": {
                    "entries": [
                        {"text": "Custom Blade"},
                        {"text": "a custom blade"},
                        {"text": "Forged for a custom server."},
                    ]
                },
            }
        )
        rebuilt = item.to_plain()
        item2 = Item.from_plain(rebuilt)
        self.assertEqual(item2.name, "Custom Blade")
        self.assertEqual(item2.damage, 55)
        self.assertEqual(item2.delay, 240)


class GeneralItemTests(unittest.TestCase):
    """General items (BOOK, USABLE, etc.) put the strings block at a
    smaller offset than equipment, inside what the equipment layout
    would treat as its own header. The detector must find it."""

    def test_book_strings_at_earlier_offset(self):
        # Build a BOOK-shaped record: the small shared header at
        # 0x00-0x0D, then the strings block at absolute 0x14. Every
        # byte between 0x0E and 0x13 is zero — those are the general
        # item's activation/timer fields for our purposes.
        record = bytearray(RECORD_SIZE)
        struct.pack_into(
            "<IHHHHH",
            record, 0,
            5467,                    # id
            0,                       # flags
            1,                       # stack
            int(ItemType.BOOK),      # type
            0,                       # resource_id
            0,                       # valid_targets
        )
        entries = [
            StringEntry(kind=KIND_STRING, text="Emerald Sword Strategy Guide"),
            StringEntry(kind=KIND_STRING, text="a strategy guide"),
            StringEntry(kind=KIND_STRING, text="A dusty tome on swordsmanship."),
        ]
        block = StringsBlock(max_size=RECORD_SIZE - 0x14, entries=entries, parsed=True)
        strings_bytes = block.serialize()
        record[0x14 : 0x14 + len(strings_bytes)] = strings_bytes

        item = Item.from_plain(bytes(record))
        self.assertEqual(item.id, 5467)
        self.assertEqual(item.item_type_name, "BOOK")
        self.assertIsNotNone(item.strings_block)
        self.assertTrue(item.strings_block.parsed)
        self.assertEqual(item.strings_offset, 0x14)
        self.assertEqual(item.name, "Emerald Sword Strategy Guide")

        # Round-trip: edit the name, re-serialise, re-parse, confirm
        # the edit lands at the same absolute offset.
        item.apply_dict(
            {
                "strings_block": {
                    "entries": [
                        {"text": "Custom Server Codex"},
                        {"text": "a codex"},
                        {"text": "Compiled for the relaunch."},
                    ]
                }
            }
        )
        rebuilt = item.to_plain()
        item2 = Item.from_plain(rebuilt)
        self.assertEqual(item2.name, "Custom Server Codex")
        self.assertEqual(item2.strings_offset, 0x14)


class UnparseableTests(unittest.TestCase):
    def test_bad_strings_block_preserved_verbatim(self):
        header = struct.pack(
            _HEADER_FMT,
            18000, 0, 1, int(ItemType.ARMOR), 0, 0, 1,
            int(Slot.HEAD), int(Race.ALL), int(Job.WAR),
            0, 0, 0, 0, 0, 0, 0, b"\x00\x00",
        )
        tail = bytearray(RECORD_SIZE - len(header))
        # Wildly bogus strings block: count=999999 will make the parser
        # reject it.
        struct.pack_into("<II", tail, 0, 999999, 0xDEADBEEF)
        for i, b in enumerate(b"garbage tail marker"):
            tail[100 + i] = b
        plain = header + bytes(tail)

        item = Item.from_plain(plain)
        # No candidate offset gave us a valid strings block, so we
        # don't have one at all — nothing to edit, and nothing to
        # trample on save.
        self.assertIsNone(item.strings_block)
        # ASCII heuristic still surfaces the marker text so the user
        # can see there's something in there.
        self.assertIn("garbage tail marker", item.strings)

        # A save round-trip must preserve the record byte-for-byte
        # because we didn't touch anything we didn't understand.
        rebuilt = item.to_plain()
        self.assertEqual(rebuilt, plain)


if __name__ == "__main__":
    unittest.main()
