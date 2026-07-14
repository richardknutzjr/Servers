"""LandSandBoat SQL export for edited items.

Given the original DAT (as loaded) and the modified DAT (after user
edits), emit a SQL patch that updates an LSB server's items to match
the client-side changes. Covers:

  * item_basic     — name, sortname, stack size, flags
  * item_equipment — level, ilvl, jobs, slot, races (for armor & general
                     equipment) OR
  * item_weapon    — everything above plus damage/delay/dps/skill
  * item_mods      — parsed from the description text (STR+26, HP+57,
                     Haste+8%, etc.)

Effects that don't map to a single ``item_mods`` row (auto-reraise,
"Set:" bonuses, latent effects) come out as ``-- TODO`` comments so
the operator writes the Lua by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from .flags import Job, Race, Slot
from .items import Item, ItemType


# --------------------------------------------------------------------
# Modifier name → LSB mod ID mapping
# --------------------------------------------------------------------
# Values match `scripts/enum/modifier.lua` on LandSandBoat main as of
# late 2025. When something in a description doesn't map, the exporter
# emits a `-- TODO` line so it's obvious what needs Lua.

MOD_ID: dict[str, int] = {
    # Basic stats
    "DEF": 23, "ATT": 3, "RATT": 4, "ACC": 5, "RACC": 6, "ENMITY": 22,
    "HP": 1, "MP": 2,
    "STR": 8, "DEX": 9, "VIT": 10, "AGI": 11, "INT": 12, "MND": 13, "CHR": 14,
    # Elemental resists
    "FIRERES": 54, "ICERES": 55, "WINDRES": 56, "EARTHRES": 57,
    "THUNDERRES": 58, "WATERRES": 59, "LIGHTRES": 60, "DARKRES": 61,
    # Evasion / magic
    "EVA": 24, "MEVA": 388,
    "MATT": 28, "MACC": 29, "MDEF": 30, "MDMG": 386, "MAB": 291,
    # Common percentage / rate mods
    "HASTE": 384, "STORETP": 172, "SUBTLEBLOW": 289,
    "REGEN": 83, "REFRESH": 84, "CONVERT_MP_TO_HP": 233,
    # XP / capacity
    "EXP_BONUS": 342, "CAPACITY_BONUS": 954,
    # Craft
    "SKL_ALCHEMY": 168, "SKL_COOKING": 169, "SKL_GOLDSMITHING": 165,
    "SKL_LEATHERCRAFT": 163, "SKL_WOODWORKING": 161, "SKL_SMITHING": 164,
    "SKL_CLOTHCRAFT": 162, "SKL_BONECRAFT": 166, "SKL_FISHING": 167,
    "SKL_SYNERGY": 216,
}

# Regex aliases: strings that appear in item descriptions mapped to
# canonical keys in ``MOD_ID``. Order matters — more specific patterns
# come first.
_MOD_ALIASES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^DEF$", re.I), "DEF"),
    (re.compile(r"^HP$", re.I), "HP"),
    (re.compile(r"^MP$", re.I), "MP"),
    (re.compile(r"^STR$", re.I), "STR"),
    (re.compile(r"^DEX$", re.I), "DEX"),
    (re.compile(r"^VIT$", re.I), "VIT"),
    (re.compile(r"^AGI$", re.I), "AGI"),
    (re.compile(r"^INT$", re.I), "INT"),
    (re.compile(r"^MND$", re.I), "MND"),
    (re.compile(r"^CHR$", re.I), "CHR"),
    (re.compile(r"^Attack$", re.I), "ATT"),
    (re.compile(r"^Accuracy$", re.I), "ACC"),
    (re.compile(r"^Ranged\s*Att(?:ack)?$", re.I), "RATT"),
    (re.compile(r"^Ranged\s*Acc(?:uracy)?$", re.I), "RACC"),
    (re.compile(r"^Enmity$", re.I), "ENMITY"),
    (re.compile(r"^Evasion$", re.I), "EVA"),
    (re.compile(r"^Magic\s*Evasion$", re.I), "MEVA"),
    (re.compile(r"^Magic\s*Attack(?:\s*Bonus)?$", re.I), "MAB"),
    (re.compile(r"^Magic\s*Accuracy$", re.I), "MACC"),
    (re.compile(r"^Magic\s*Def(?:ense)?(?:\s*Bonus)?$", re.I), "MDEF"),
    (re.compile(r"^Magic\s*Damage$", re.I), "MDMG"),
    (re.compile(r"^Haste$", re.I), "HASTE"),
    (re.compile(r"^Store\s*TP$", re.I), "STORETP"),
    (re.compile(r"^Subtle\s*Blow$", re.I), "SUBTLEBLOW"),
    (re.compile(r"^Regen$", re.I), "REGEN"),
    (re.compile(r"^Refresh$", re.I), "REFRESH"),
    (re.compile(r"^Fire\s*Res(?:ist)?$", re.I), "FIRERES"),
    (re.compile(r"^Ice\s*Res(?:ist)?$", re.I), "ICERES"),
    (re.compile(r"^Wind\s*Res(?:ist)?$", re.I), "WINDRES"),
    (re.compile(r"^Earth\s*Res(?:ist)?$", re.I), "EARTHRES"),
    (re.compile(r"^(?:Thunder|Lightning)\s*Res(?:ist)?$", re.I), "THUNDERRES"),
    (re.compile(r"^Water\s*Res(?:ist)?$", re.I), "WATERRES"),
    (re.compile(r"^Light\s*Res(?:ist)?$", re.I), "LIGHTRES"),
    (re.compile(r"^Dark\s*Res(?:ist)?$", re.I), "DARKRES"),
    (re.compile(r"^Experience\s*Points?(?:\s*Boost)?$", re.I), "EXP_BONUS"),
    (re.compile(r"^Capacity\s*Points?(?:\s*Boost)?$", re.I), "CAPACITY_BONUS"),
]

# Description line patterns that don't map to item_mods and need Lua.
_LUA_HINTS: list[re.Pattern[str]] = [
    re.compile(r"Auto\s*Reraise", re.I),
    re.compile(r"^Set\s*:", re.I),
    re.compile(r"^Latent\s*Effect", re.I),
    re.compile(r"Additional\s*Effect", re.I),
    re.compile(r"Enhances\s+", re.I),
    re.compile(r"Occ(?:asionally)?\s+", re.I),
    re.compile(r"Aftermath", re.I),
]


@dataclass
class ParsedMod:
    key: str          # canonical modifier key
    value: int        # integer value (percent shown as int; +50% = 50)
    mod_id: Optional[int] = None
    raw: str = ""     # original text fragment for context / TODO comments


@dataclass
class ItemExport:
    item: Item
    description_lines: list[str]
    mods: list[ParsedMod]
    lua_hints: list[str]


# --------------------------------------------------------------------
# Description parsing
# --------------------------------------------------------------------

# Splits a description into logical fragments. The client shows the
# description on multiple lines with \n and space separators; each
# fragment usually corresponds to one mod.
_FRAGMENT_SPLIT = re.compile(r"[\n\r]+")

# Matches "Name+value", "Name -value", or "Name:value" with optional
# % sign. Skips quoted names (like `"Aggressor" duration +16`) since
# those are script effects, not passive mods.
_MOD_PATTERN = re.compile(
    r"([A-Za-z][A-Za-z\.\s]*?)\s*"
    r"(?:([+\-])|:)\s*"
    r"(\d+)\s*"
    r"(%?)"
)


def parse_description(text: str) -> tuple[list[ParsedMod], list[str]]:
    """Return (mods, lua_hints) for a raw description string."""
    mods: list[ParsedMod] = []
    lua_hints: list[str] = []

    for line in _FRAGMENT_SPLIT.split(text):
        line = line.strip()
        if not line:
            continue

        # If the line looks like something that needs Lua, remember it
        # verbatim and skip mod parsing on it.
        if any(pat.search(line) for pat in _LUA_HINTS):
            lua_hints.append(line)
            continue

        # Skip lines that start with a quoted name (script-driven).
        if line.startswith('"'):
            lua_hints.append(line)
            continue

        for match in _MOD_PATTERN.finditer(line):
            name, sign, value_str, pct = match.groups()
            name = name.strip().strip(":")
            if not name:
                continue
            value = int(value_str)
            # sign is None when the separator was ':' (base stat form).
            if sign == "-":
                value = -value

            key = None
            for pattern, canonical in _MOD_ALIASES:
                if pattern.match(name):
                    key = canonical
                    break
            if key is None:
                # Fall back to uppercase-trimmed name; it becomes a TODO
                # so the operator can decide.
                lua_hints.append(f"{name} {sign}{value_str}{pct}")
                continue

            mods.append(
                ParsedMod(
                    key=key,
                    value=value,
                    mod_id=MOD_ID.get(key),
                    raw=match.group(0).strip(),
                )
            )

    return mods, lua_hints


# --------------------------------------------------------------------
# Item categorisation for SQL
# --------------------------------------------------------------------

def is_equipment(item: Item) -> bool:
    """Whether this item slots into item_equipment / item_weapon."""
    if item.item_type == int(ItemType.WEAPON):
        return True
    if item.item_type == int(ItemType.ARMOR):
        return True
    # Some general items are also equippable in narrow cases; skip for
    # this pass — the operator can add them by hand.
    return False


def build_export(item: Item) -> ItemExport:
    mods, lua = parse_description(item.description or "")
    return ItemExport(
        item=item,
        description_lines=[
            l for l in _FRAGMENT_SPLIT.split(item.description or "") if l.strip()
        ],
        mods=mods,
        lua_hints=lua,
    )


# --------------------------------------------------------------------
# SQL emission
# --------------------------------------------------------------------

def _sql_str(s: str) -> str:
    """Quote a string for SQL, using MySQL-safe escaping."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _sortname(name: str) -> str:
    """LSB's sortname is a lowercase snake_case-ish form of the name."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return s or "custom_item"


def emit_item_basic(item: Item) -> str:
    name = item.name
    sortname = _sortname(name)
    stack = max(1, item.stack_size)
    flags = item.flags
    return (
        f"INSERT INTO item_basic (itemid, subid, name, sortname, "
        f"stackSize, flags, ah, NoSale, BaseSell) VALUES\n"
        f"  ({item.id}, 0, {_sql_str(name)}, {_sql_str(sortname)}, "
        f"{stack}, {flags}, 0, 0, 0)\n"
        f"ON DUPLICATE KEY UPDATE "
        f"name=VALUES(name), sortname=VALUES(sortname), "
        f"stackSize=VALUES(stackSize), flags=VALUES(flags);"
    )


def emit_item_equipment(item: Item) -> str:
    if item.item_type == int(ItemType.WEAPON):
        return emit_item_weapon(item)
    # Armor uses item_equipment.
    return (
        f"INSERT INTO item_equipment (itemid, name, level, ilvl, jobs, "
        f"MId, shieldSize, scriptType, slot, rslot, su_level) VALUES\n"
        f"  ({item.id}, {_sql_str(item.name)}, {item.level}, "
        f"{item.superior_level}, {item.jobs}, 0, {item.shield_size}, 0, "
        f"{item.slots}, 0, 0)\n"
        f"ON DUPLICATE KEY UPDATE "
        f"level=VALUES(level), ilvl=VALUES(ilvl), jobs=VALUES(jobs), "
        f"slot=VALUES(slot), shieldSize=VALUES(shieldSize);"
    )


def emit_item_weapon(item: Item) -> str:
    return (
        f"INSERT INTO item_weapon (itemid, name, skill, subskill, ilvl, "
        f"dmgType, hit, delay, dmg, unlock_index, jobs, dmgType_offhand, "
        f"delay_offhand, dmg_offhand) VALUES\n"
        f"  ({item.id}, {_sql_str(item.name)}, {item.skill}, 0, "
        f"{item.superior_level}, 0, 0, {item.delay}, {item.damage}, 0, "
        f"{item.jobs}, 0, 0, 0)\n"
        f"ON DUPLICATE KEY UPDATE "
        f"skill=VALUES(skill), ilvl=VALUES(ilvl), delay=VALUES(delay), "
        f"dmg=VALUES(dmg), jobs=VALUES(jobs);"
    )


def emit_item_mods(item: Item, mods: Iterable[ParsedMod]) -> str:
    mods = list(mods)
    lines = [f"DELETE FROM item_mods WHERE itemid = {item.id};"]
    if not mods:
        lines.append(f"-- no item_mods rows parsed for {item.name}")
        return "\n".join(lines)
    lines.append("INSERT INTO item_mods (itemid, modId, value) VALUES")
    values = [
        f"  ({item.id}, {m.mod_id}, {m.value})"
        f"    -- {m.key} from {m.raw!r}"
        for m in mods
        if m.mod_id is not None
    ]
    if not values:
        lines.append(f"-- (no known mods matched; see TODOs below)")
        return "\n".join(lines[:-1])
    lines.append(",\n".join(values) + ";")
    return "\n".join(lines)


def emit_sql_for_item(export: ItemExport) -> str:
    """Emit a full block of SQL for a single edited item."""
    item = export.item
    lines: list[str] = []
    lines.append(f"-- ------------------------------------------------------------")
    lines.append(f"-- #{item.id}  {item.name!r}  ({item.item_type_name})")
    for d in export.description_lines[:6]:
        lines.append(f"--   {d}")
    lines.append(f"-- ------------------------------------------------------------")
    lines.append(emit_item_basic(item))
    if is_equipment(item):
        lines.append(emit_item_equipment(item))
    lines.append(emit_item_mods(item, export.mods))
    for todo in export.lua_hints:
        lines.append(f"-- TODO (needs Lua): {todo}")
    lines.append("")
    return "\n".join(lines)


def emit_patch(edited_items: Iterable[Item]) -> str:
    """Emit the entire SQL patch for every edited item."""
    header = [
        "-- ================================================================",
        "-- Generated by ffxi-dat-editor",
        "-- Drop this file into your LSB server's sql/ folder (or execute",
        "-- it directly against your zoning DB) after backing up first.",
        "-- Effects marked -- TODO (needs Lua) require a Lua script hook",
        "-- (scripts/globals/items/<sortname>.lua) — the editor can't",
        "-- generate those safely from tooltip text alone.",
        "-- ================================================================",
        "",
    ]
    body = [emit_sql_for_item(build_export(it)) for it in edited_items]
    return "\n".join(header) + "\n".join(body)
