"""Round-trip tests for the item DAT reader/writer.

These tests never touch real game files. They synthesise records with
known field values, encode -> parse -> edit -> save -> parse, and assert
that every field survives unchanged.
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ffxidat import (  # noqa: E402
    Item,
    ItemDat,
    ItemType,
    Job,
    RECORD_SIZE,
    Race,
    Slot,
    decode_bytes,
    encode_bytes,
)
from ffxidat.items import _HEADER_FMT, extract_strings  # noqa: E402


def make_armor(item_id: int, name: bytes = b"Test Item") -> bytes:
    """Build a plaintext armor record with predictable field values."""
    header = struct.pack(
        _HEADER_FMT,
        item_id,           # id
        0x2000,            # flags
        1,                 # stack_size
        int(ItemType.ARMOR),
        0x1234,            # resource_id
        0x0011,            # valid_targets
        60,                # level
        int(Slot.HEAD | Slot.HANDS),
        int(Race.ALL),
        int(Job.WAR | Job.PLD | Job.DRK),
        99,                # superior_level
        0,                 # shield_size
        0,                 # max_charges
        0,                 # casting_time
        0,                 # use_delay
        0,                 # reuse_delay
        0x5678,            # model
        b"\xAB\xCD",       # unknown_26
    )
    tail = bytearray(RECORD_SIZE - len(header))
    # Embed a name-ish string so extract_strings has something to find.
    tail[100 : 100 + len(name)] = name
    # Non-zero payload past the "known" region to confirm it's preserved.
    tail[500:504] = b"\xDE\xAD\xBE\xEF"
    return header + bytes(tail)


class CipherTests(unittest.TestCase):
    def test_roundtrip_all_bytes(self):
        raw = bytes(range(256))
        self.assertEqual(decode_bytes(encode_bytes(raw)), raw)
        self.assertEqual(encode_bytes(decode_bytes(raw)), raw)

    def test_encoding_is_not_identity(self):
        raw = bytes(range(256))
        # Non-zero bytes must actually change under the transform. (0
        # rotates to 0, so it's the one exception.)
        self.assertNotEqual(encode_bytes(raw), raw)


class ItemParseTests(unittest.TestCase):
    def test_header_fields_parse(self):
        plain = make_armor(item_id=15001)
        item = Item.from_plain(plain)
        self.assertEqual(item.id, 15001)
        self.assertEqual(item.item_type, int(ItemType.ARMOR))
        self.assertEqual(item.item_type_name, "ARMOR")
        self.assertEqual(item.level, 60)
        self.assertEqual(item.superior_level, 99)
        self.assertEqual(sorted(item.slot_names), ["HANDS", "HEAD"])
        self.assertIn("WAR", item.job_names)
        self.assertIn("PLD", item.job_names)
        self.assertIn("DRK", item.job_names)
        self.assertEqual(item.model, 0x5678)
        self.assertEqual(item.unknown_26, b"\xAB\xCD")

    def test_tail_preserved_verbatim(self):
        plain = make_armor(item_id=15002)
        item = Item.from_plain(plain)
        rebuilt = item.to_plain()
        self.assertEqual(rebuilt, plain, "tail bytes must survive round-trip")

    def test_strings_extracted(self):
        plain = make_armor(item_id=15003, name=b"Bronze Cap")
        item = Item.from_plain(plain)
        self.assertIn("Bronze Cap", item.strings)
        self.assertEqual(item.name, "Bronze Cap")

    def test_extract_strings_ignores_short_runs(self):
        blob = b"\x00ab\x00Longer string here\x00\x01\x02"
        strings = extract_strings(blob, min_len=3)
        self.assertNotIn("ab", strings)
        self.assertIn("Longer string here", strings)


class EditTests(unittest.TestCase):
    def test_apply_dict_bitmask_names(self):
        item = Item.from_plain(make_armor(item_id=16000))
        item.apply_dict(
            {
                "level": 75,
                "slot_names": ["BODY"],
                "job_names": ["RDM", "BLM"],
                "race_names": ["HUME_M", "HUME_F"],
            }
        )
        self.assertEqual(item.level, 75)
        self.assertEqual(item.slot_names, ["BODY"])
        self.assertEqual(sorted(item.job_names), ["BLM", "RDM"])
        self.assertEqual(sorted(item.race_names), ["HUME_F", "HUME_M"])

    def test_full_file_roundtrip(self):
        records = [make_armor(item_id=i, name=f"Item{i:04d}".encode()) for i in range(20000, 20005)]
        dat = ItemDat.from_records(records)
        # Edit a field on one item.
        dat.items[2].apply_dict({"level": 42, "damage": 100, "slot_names": ["MAIN"]})
        # Serialise -> parse from bytes.
        encoded = dat.to_bytes()
        reparsed = ItemDat.from_records(
            [decode_bytes(encoded[i : i + RECORD_SIZE]) for i in range(0, len(encoded), RECORD_SIZE)]
        )
        self.assertEqual(reparsed.items[2].level, 42)
        self.assertEqual(reparsed.items[2].slot_names, ["MAIN"])
        # Other items untouched.
        self.assertEqual(reparsed.items[0].level, 60)
        self.assertEqual(reparsed.items[4].id, 20004)


class FileTests(unittest.TestCase):
    def test_load_and_save_disk(self):
        records = [make_armor(item_id=i) for i in (30001, 30002, 30003)]
        raw = b"".join(encode_bytes(r) for r in records)

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "ffxidat-test.DAT"
            tmp.write_bytes(raw)
            self._run_load_and_save(tmp, records)

    def _run_load_and_save(self, tmp, records):

        dat = ItemDat.load(tmp)
        self.assertEqual(len(dat.items), 3)
        self.assertEqual(dat.items[0].id, 30001)

        dat.items[1].apply_dict({"level": 25})
        dat.save()

        dat2 = ItemDat.load(tmp)
        self.assertEqual(dat2.items[1].level, 25)
        # And the untouched item is bit-for-bit identical to what we
        # loaded originally.
        self.assertEqual(dat2.items[0].to_plain(), records[0])


if __name__ == "__main__":
    unittest.main()
