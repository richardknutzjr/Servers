"""Tests for the item strings block parser/writer."""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ffxidat.strings import (  # noqa: E402
    EQUIPMENT_STRINGS_SIZE,
    KIND_INTEGER,
    KIND_STRING,
    StringEntry,
    StringsBlock,
)


def synth_block(entries: list[StringEntry], header_flag: int = 1) -> bytes:
    """Build a strings-block byte buffer in the layout the parser targets."""
    count = len(entries)
    out = bytearray(EQUIPMENT_STRINGS_SIZE)
    struct.pack_into("<II", out, 0, count, header_flag)
    cursor = 8 + count * 8
    for i, entry in enumerate(entries):
        entry_offset = cursor
        if entry.kind == KIND_INTEGER:
            struct.pack_into("<I", out, cursor, entry.value)
            cursor += 4
        else:
            payload = entry.text.encode("latin-1")
            struct.pack_into("<II", out, cursor, entry.flag, len(payload))
            cursor += 8
            out[cursor : cursor + len(payload)] = payload
            cursor += len(payload)
            out[cursor] = 0
            cursor += 1
        struct.pack_into("<II", out, 8 + i * 8, entry_offset, entry.kind)
    return bytes(out)


class ParseTests(unittest.TestCase):
    def test_string_entries_roundtrip(self):
        entries = [
            StringEntry(kind=KIND_STRING, text="Bronze Cap"),
            StringEntry(kind=KIND_STRING, text="bronze cap"),
            StringEntry(kind=KIND_STRING, text="bronze caps"),
            StringEntry(kind=KIND_STRING, text="A cap made of bronze."),
        ]
        raw = synth_block(entries)
        block = StringsBlock.parse(raw, EQUIPMENT_STRINGS_SIZE)
        self.assertTrue(block.parsed)
        self.assertEqual(len(block.entries), 4)
        self.assertEqual(block.entries[0].text, "Bronze Cap")
        self.assertEqual(block.entries[3].text, "A cap made of bronze.")

        rebuilt = block.serialize()
        self.assertEqual(rebuilt, raw)

    def test_mixed_integer_entry(self):
        entries = [
            StringEntry(kind=KIND_STRING, text="Custom Ring"),
            StringEntry(kind=KIND_STRING, text="Adds +{0} to STR."),
            StringEntry(kind=KIND_INTEGER, value=15),
        ]
        raw = synth_block(entries)
        block = StringsBlock.parse(raw, EQUIPMENT_STRINGS_SIZE)
        self.assertTrue(block.parsed)
        self.assertEqual(block.entries[2].kind, KIND_INTEGER)
        self.assertEqual(block.entries[2].value, 15)
        self.assertEqual(block.serialize(), raw)

    def test_apply_dict_edits_text(self):
        entries = [
            StringEntry(kind=KIND_STRING, text="Bronze Cap"),
            StringEntry(kind=KIND_STRING, text="a bronze cap"),
        ]
        block = StringsBlock.parse(synth_block(entries), EQUIPMENT_STRINGS_SIZE)
        block.apply_dict(
            {
                "entries": [
                    {"text": "Void Crown"},
                    {"text": "a void crown"},
                ]
            }
        )
        self.assertEqual(block.entries[0].text, "Void Crown")
        # Round-trip the edited block and re-parse.
        rebuilt = block.serialize()
        reparsed = StringsBlock.parse(rebuilt, EQUIPMENT_STRINGS_SIZE)
        self.assertTrue(reparsed.parsed)
        self.assertEqual(reparsed.entries[0].text, "Void Crown")
        self.assertEqual(reparsed.entries[1].text, "a void crown")

    def test_apply_dict_rejects_count_change(self):
        entries = [StringEntry(kind=KIND_STRING, text="One")]
        block = StringsBlock.parse(synth_block(entries), EQUIPMENT_STRINGS_SIZE)
        with self.assertRaises(ValueError):
            block.apply_dict({"entries": [{"text": "A"}, {"text": "B"}]})

    def test_overflow_raises(self):
        # Start with a modest block, then blow the string up past the
        # block's max size to prove serialize() catches overflow.
        entries = [StringEntry(kind=KIND_STRING, text="short")]
        block = StringsBlock.parse(synth_block(entries), EQUIPMENT_STRINGS_SIZE)
        block.entries[0].text = "Y" * EQUIPMENT_STRINGS_SIZE
        with self.assertRaises(ValueError):
            block.serialize()

    def test_padding_after_last_entry_is_zeroed(self):
        entries = [StringEntry(kind=KIND_STRING, text="A")]
        block = StringsBlock.parse(synth_block(entries), EQUIPMENT_STRINGS_SIZE)
        out = block.serialize()
        # First bytes have real data, the tail must be zeros.
        self.assertEqual(len(out), EQUIPMENT_STRINGS_SIZE)
        # Find where the string's terminator sits and check the rest.
        tail = out[100:]  # comfortably past any real payload
        self.assertEqual(tail, b"\x00" * len(tail))


class FallbackTests(unittest.TestCase):
    def test_garbage_stays_raw(self):
        # A block whose "count" would exceed our cap. Parser must
        # refuse and hand the raw bytes back untouched on serialize.
        garbage = bytearray(EQUIPMENT_STRINGS_SIZE)
        struct.pack_into("<II", garbage, 0, 99999, 0xDEADBEEF)
        for i, b in enumerate(b"opaque tail data"):
            garbage[100 + i] = b

        block = StringsBlock.parse(bytes(garbage), EQUIPMENT_STRINGS_SIZE)
        self.assertFalse(block.parsed)
        self.assertEqual(block.serialize(), bytes(garbage))

    def test_apply_dict_no_op_when_unparsed(self):
        garbage = bytes([0xFF] * EQUIPMENT_STRINGS_SIZE)
        block = StringsBlock.parse(garbage, EQUIPMENT_STRINGS_SIZE)
        self.assertFalse(block.parsed)
        # apply_dict must not raise; it's a no-op on unparsed blocks so
        # the caller's edits are silently ignored (safer than pretending
        # we understand the layout).
        block.apply_dict({"entries": [{"text": "changed"}]})
        self.assertEqual(block.serialize(), garbage)


if __name__ == "__main__":
    unittest.main()
