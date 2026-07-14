"""Tests for the LSB SQL exporter.

Cover: description parsing (stat regex + Lua-hint detection), and
end-to-end SQL generation for a synthesised item.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ffxidat.items import Item, ItemType  # noqa: E402
from ffxidat.flags import Job, Race, Slot  # noqa: E402
from ffxidat.strings import (  # noqa: E402
    KIND_INTEGER,
    KIND_STRING,
    StringEntry,
    StringsBlock,
    EQUIPMENT_STRINGS_SIZE,
)
from ffxidat import lsb_export  # noqa: E402


def _item_with_description(
    item_id: int,
    name: str,
    description: str,
    *,
    item_type: int = int(ItemType.ARMOR),
    level: int = 99,
    slots: int = int(Slot.HEAD),
) -> Item:
    item = Item(
        id=item_id,
        item_type=item_type,
        level=level,
        superior_level=0,
        slots=slots,
        races=int(Race.ALL),
        jobs=int(Job.WAR | Job.PLD),
        stack_size=1,
        flags=0x2000,
    )
    entries = [
        StringEntry(kind=KIND_STRING, text=name),
        StringEntry(kind=KIND_INTEGER, value=1),
        StringEntry(kind=KIND_STRING, text=name.lower()),
        StringEntry(kind=KIND_STRING, text=name.lower() + "s"),
        StringEntry(kind=KIND_STRING, text=description),
    ]
    item.strings_block = StringsBlock(
        max_size=EQUIPMENT_STRINGS_SIZE,
        entries=entries,
        parsed=True,
        format_version=2,
    )
    item.strings_max_size = EQUIPMENT_STRINGS_SIZE
    return item


class ParseDescriptionTests(unittest.TestCase):
    def test_basic_stats(self):
        mods, lua = lsb_export.parse_description(
            "DEF:123 HP+57 STR+26 DEX+29 VIT+29"
        )
        keys = {m.key: m.value for m in mods}
        self.assertEqual(keys.get("DEF"), 123)
        self.assertEqual(keys.get("HP"), 57)
        self.assertEqual(keys.get("STR"), 26)
        self.assertEqual(keys.get("DEX"), 29)
        self.assertEqual(keys.get("VIT"), 29)
        self.assertEqual(lua, [])

    def test_percent_and_negative(self):
        mods, _ = lsb_export.parse_description("Haste+8% Enmity-4")
        by_key = {m.key: m.value for m in mods}
        self.assertEqual(by_key["HASTE"], 8)
        self.assertEqual(by_key["ENMITY"], -4)

    def test_capacity_and_exp_boost(self):
        mods, lua = lsb_export.parse_description(
            "Capacity Points Boost +50%\nExperience Points Boost +50%\nAuto Reraise Effect"
        )
        by_key = {m.key: m.value for m in mods}
        self.assertEqual(by_key["CAPACITY_BONUS"], 50)
        self.assertEqual(by_key["EXP_BONUS"], 50)
        # Auto Reraise → Lua hint.
        self.assertTrue(any("Auto Reraise" in h for h in lua))

    def test_quoted_effect_becomes_lua_hint(self):
        _, lua = lsb_export.parse_description('"Aggressor" duration +16')
        self.assertTrue(any("Aggressor" in h for h in lua))

    def test_set_bonus_flagged_for_lua(self):
        _, lua = lsb_export.parse_description(
            "Set: Increases Accuracy,\nRanged Accuracy, and Magic Accuracy"
        )
        self.assertTrue(any("Set" in h for h in lua))

    def test_unknown_mod_falls_through_to_lua(self):
        mods, lua = lsb_export.parse_description("Frobnication +5")
        self.assertEqual(mods, [])
        self.assertTrue(any("Frobnication" in h for h in lua))


class EmitSqlTests(unittest.TestCase):
    def test_armor_emits_item_basic_equipment_and_mods(self):
        item = _item_with_description(
            26169,
            "Legendary Ring",
            "Capacity Points Boost +50%\nExperience Points Boost +50%\nAuto Reraise Effect",
            item_type=int(ItemType.ARMOR),
            level=1,
            slots=int(Slot.RING1 | Slot.RING2),
        )
        sql = lsb_export.emit_patch([item])
        self.assertIn("INSERT INTO item_basic", sql)
        self.assertIn("INSERT INTO item_equipment", sql)
        self.assertIn("DELETE FROM item_mods WHERE itemid = 26169", sql)
        # Both parsed mods land in item_mods.
        self.assertIn("(26169, 954, 50)", sql)  # CAPACITY_BONUS
        self.assertIn("(26169, 342, 50)", sql)  # EXP_BONUS
        # Auto Reraise becomes a TODO comment.
        self.assertIn("-- TODO (needs Lua): Auto Reraise Effect", sql)
        # Name is quoted correctly.
        self.assertIn("'Legendary Ring'", sql)
        # sortname is snake_case.
        self.assertIn("'legendary_ring'", sql)

    def test_weapon_emits_item_weapon_not_equipment(self):
        item = _item_with_description(
            18747,
            "Smash Cesti",
            "DMG:10 Delay:96 STR+3",
            item_type=int(ItemType.WEAPON),
            slots=int(Slot.MAIN),
        )
        item.damage = 10
        item.delay = 96
        item.skill = 1
        sql = lsb_export.emit_patch([item])
        self.assertIn("INSERT INTO item_weapon", sql)
        self.assertNotIn("INSERT INTO item_equipment", sql)
        self.assertIn("(18747, 8, 3)", sql)  # STR+3

    def test_apostrophe_in_name_is_escaped(self):
        item = _item_with_description(
            23040, "Pummeler's Mask +2", "DEF:123 STR+26"
        )
        sql = lsb_export.emit_patch([item])
        self.assertIn("'Pummeler\\'s Mask +2'", sql)
        # sortname strips the punctuation.
        self.assertIn("'pummeler_s_mask_2'", sql)


if __name__ == "__main__":
    unittest.main()
