"""LandSandBoat SQL + Lua export for edited items.

Given the modified items after user edits, emit a SQL patch that
updates an LSB server's items to match the client-side changes, and
generate Lua stubs for effects that need script hooks:

  * item_basic     — name, sortname, stack size, flags
  * item_equipment — level, ilvl, jobs, slot, races (for armor)
  * item_weapon    — as above plus dmg/delay/dps/skill
  * item_mods      — parsed from the description text
  * item_latents   — for latent/set/condition bonuses
  * .lua stubs     — for genuinely script-driven effects (Auto Reraise,
                     "Enhances Ability", etc.)

The modifier table is intentionally broad — the exporter tries hard to
turn as many tooltip lines as possible into concrete rows so operators
don't have to hand-write mod IDs. Anything that can't be resolved comes
out as `-- TODO` alongside a stub Lua file so nothing gets silently
dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .flags import Job, Race, Slot
from .items import Item, ItemType


# ====================================================================
# Modifier ID table
# ====================================================================
# Values match `scripts/enum/modifier.lua` in mainline LandSandBoat as
# of late 2025. Where LSB has multiple candidate mods for the same
# tooltip line (e.g. HASTE has magic/ability/gear variants) we pick the
# gear variant — that's what an equipment tooltip almost always means.
#
# Grouped for readability. The value column is what lands in
# item_mods.modId; sourced from the LSB enum.

MOD_ID: dict[str, int] = {}

# --- basic attributes ---------------------------------------------
MOD_ID.update({
    "DEF": 23, "HP": 1, "HP_PERCENT": 179, "MP": 2, "MP_PERCENT": 180,
    "STR": 8, "DEX": 9, "VIT": 10, "AGI": 11, "INT": 12, "MND": 13, "CHR": 14,
    "STR_PERCENT": 181, "DEX_PERCENT": 182, "VIT_PERCENT": 183,
    "AGI_PERCENT": 184, "INT_PERCENT": 185, "MND_PERCENT": 186,
    "CHR_PERCENT": 187,
})

# --- offense / defense --------------------------------------------
MOD_ID.update({
    "ATT": 3, "RATT": 4, "ACC": 5, "RACC": 6, "ENMITY": 22,
    "ATT_PERCENT": 168, "RATT_PERCENT": 169, "DEF_PERCENT": 170,
    "EVA": 24, "MEVA": 388,
    "MATT": 291, "MACC": 29, "MDEF": 30, "MDMG": 386, "MAB": 291,
    "MAGIC_BURST_BONUS": 233,
    "PARRY": 28, "GUARD": 26, "BLOCK": 25, "SHIELD_BASH": 189,
    "COUNTER": 292, "COUNTER_RATE": 292,
    "PHYSICAL_DAMAGE_TAKEN": 240, "MAGIC_DAMAGE_TAKEN": 241,
    "BREATH_DAMAGE_TAKEN": 242, "DAMAGE_TAKEN": 232,
    "PHYSICAL_DAMAGE_TAKEN_PERCENT": 240,
    "PHALANX": 703,
})

# --- attack rate mods ----------------------------------------------
MOD_ID.update({
    "DOUBLE_ATTACK": 288, "TRIPLE_ATTACK": 356, "QUAD_ATTACK": 391,
    "MULTI_HIT_RATE": 356,
    "KICK_ATTACK_RATE": 379, "KICK_ATTACK": 379,
    "ZANSHIN": 293, "MARTIAL_ARTS": 174,
    "CRIT_HIT_RATE": 41, "CRIT_HIT_DMG": 435, "CRIT_HIT_EVASION": 435,
    "MAGIC_CRIT_RATE": 436, "MAGIC_CRIT_DMG": 437,
    "SUBTLE_BLOW": 289, "SUBTLE_BLOW_II": 962,
})

# --- haste / delay ------------------------------------------------
MOD_ID.update({
    "HASTE": 384, "HASTE_GEAR": 384,
    "HASTE_MAGIC_CAP": 502, "HASTE_ABILITY_CAP": 502,
    "MAGIC_HASTE": 383,
    "SLOW": 385, "SPELL_INTERRUPTION": 100,
    "SPELL_INTERRUPTION_RATE": 100,
    "FAST_CAST": 388, "FAST_CAST_CURE": 858,
    "CURE_CAST_TIME": 858, "MOVE": 169, "MOVE_SPEED": 169,
})

# --- TP / weapon skills -------------------------------------------
MOD_ID.update({
    "STORE_TP": 172, "STORETP": 172, "REGAIN": 288,
    "SAVAGE_BLADE": 848, "WS_ACC": 359, "WS_DAMAGE": 384,
    "WS_ATT": 434, "WS_STR": 465, "WS_DEX": 466, "WS_VIT": 467,
    "WS_AGI": 468, "WS_INT": 469, "WS_MND": 470, "WS_CHR": 471,
    "TP_BONUS": 640,
})

# --- regen / refresh / convert ------------------------------------
MOD_ID.update({
    "REGEN": 83, "REFRESH": 84, "REGAIN_STAT": 288, "CONVERT_MP_TO_HP": 233,
    "MAGIC_ATT_DRAIN": 291, "DRAIN_HP": 289, "ASPIR_HP": 289,
    "OCCULT_ACUMEN": 900,
})

# --- elemental resists --------------------------------------------
MOD_ID.update({
    "FIRERES": 54, "ICERES": 55, "WINDRES": 56, "EARTHRES": 57,
    "THUNDERRES": 58, "WATERRES": 59, "LIGHTRES": 60, "DARKRES": 61,
    "FIREDEF": 62, "ICEDEF": 63, "WINDDEF": 64, "EARTHDEF": 65,
    "THUNDERDEF": 66, "WATERDEF": 67, "LIGHTDEF": 68, "DARKDEF": 69,
    "FIRE_MAGIC_ATT": 70, "ICE_MAGIC_ATT": 71, "WIND_MAGIC_ATT": 72,
    "EARTH_MAGIC_ATT": 73, "THUNDER_MAGIC_ATT": 74, "WATER_MAGIC_ATT": 75,
    "LIGHT_MAGIC_ATT": 76, "DARK_MAGIC_ATT": 77,
    "FIRE_MAGIC_ACC": 78, "ICE_MAGIC_ACC": 79, "WIND_MAGIC_ACC": 80,
    "EARTH_MAGIC_ACC": 81, "THUNDER_MAGIC_ACC": 82, "WATER_MAGIC_ACC": 83,
    "LIGHT_MAGIC_ACC": 84, "DARK_MAGIC_ACC": 85,
})

# --- status resistances -------------------------------------------
MOD_ID.update({
    "SLEEPRES": 240, "POISONRES": 241, "PARALYZERES": 242,
    "BLINDRES": 243, "SILENCERES": 244, "VIRUSRES": 245,
    "PETRIFYRES": 246, "BINDRES": 247, "CURSERES": 248,
    "GRAVITYRES": 249, "SLOWRES": 250, "STUNRES": 251,
    "CHARMRES": 252, "AMNESIARES": 253, "LULLABYRES": 254,
    "DEATHRES": 255,
})

# --- weapon / combat skills ---------------------------------------
MOD_ID.update({
    "H2H": 152, "DAGGER": 153, "SWORD": 154, "GREAT_SWORD": 155,
    "AXE": 156, "GREAT_AXE": 157, "SCYTHE": 158, "POLEARM": 159,
    "KATANA": 160, "GREAT_KATANA": 161, "CLUB": 162, "STAFF": 163,
    "ARCHERY": 164, "MARKSMANSHIP": 165, "THROWING": 166,
    "GUARD_SKILL": 167,  # Guard combat skill (different from the block rate above)
    "EVASION": 168,
    "SHIELD_SKILL": 169,
    "PARRYING": 170,
    "DIVINE_MAGIC": 171, "HEALING_MAGIC": 172, "ENHANCING_MAGIC": 173,
    "ENFEEBLING_MAGIC": 174, "ELEMENTAL_MAGIC": 175, "DARK_MAGIC": 176,
    "SUMMONING_MAGIC": 177, "NINJUTSU": 178, "SINGING": 179,
    "STRING_INST": 180, "WIND_INST": 181,
    "BLUE_MAGIC": 182, "GEO_MAGIC": 217, "HANDBELL_INST": 218,
})

# --- craft skills -------------------------------------------------
MOD_ID.update({
    "SKL_ALCHEMY": 168, "SKL_COOKING": 169, "SKL_GOLDSMITHING": 165,
    "SKL_LEATHERCRAFT": 163, "SKL_WOODWORKING": 161, "SKL_SMITHING": 164,
    "SKL_CLOTHCRAFT": 162, "SKL_BONECRAFT": 166, "SKL_FISHING": 167,
    "SKL_SYNERGY": 216,
})

# --- specific enhancements ----------------------------------------
MOD_ID.update({
    "ENHANCES_STORE_TP": 172,
    "ENHANCES_HASTE": 384,
    "ENHANCES_DA": 288,
    "ENHANCES_CRIT_HIT_RATE": 41,
    "ENHANCES_ENMITY": 22,
    "ENHANCES_STR": 8,
    "ENHANCES_DEX": 9,
    "ENHANCES_VIT": 10,
    "ENHANCES_AGI": 11,
    "ENHANCES_INT": 12,
    "ENHANCES_MND": 13,
    "ENHANCES_CHR": 14,
})

# --- job ability duration / recast --------------------------------
MOD_ID.update({
    "ENHANCES_AGGRESSOR": 421, "AGGRESSOR_EFFECT": 421,
    "ENHANCES_BERSERK": 424, "BERSERK_EFFECT": 424,
    "ENHANCES_DEFENDER": 425, "DEFENDER_EFFECT": 425,
    "ENHANCES_WARCRY": 426, "WARCRY_EFFECT": 426,
    "ENHANCES_MEDITATE": 427, "MEDITATE_EFFECT": 427,
    "ENHANCES_MEIKYO": 428, "MEIKYO_EFFECT": 428,
    "ENHANCES_HASSO": 429, "HASSO_EFFECT": 429,
    "ENHANCES_SEIGAN": 430, "SEIGAN_EFFECT": 430,
    "ENHANCES_CHIBLAST": 431, "CHIBLAST_EFFECT": 431,
    "ENHANCES_MIJIN_GAKURE": 432, "ENHANCES_UTSUSEMI": 433,
    "ENHANCES_SNEAK_ATTACK": 434, "ENHANCES_TRICK_ATTACK": 435,
    "ENHANCES_HIDE": 436, "ENHANCES_STEAL": 437,
    "ENHANCES_MUG": 438, "ENHANCES_FLEE": 439,
    "ENHANCES_PROVOKE": 440, "ENHANCES_SENTINEL": 441,
    "ENHANCES_HOLY_CIRCLE": 442, "ENHANCES_SHIELD_BASH": 443,
    "ENHANCES_ARCANE_CIRCLE": 444, "ENHANCES_LAST_RESORT": 445,
    "ENHANCES_WEAPON_BASH": 446, "ENHANCES_SOULEATER": 447,
    "ENHANCES_ETERNAL_ESPRIT": 448, "ENHANCES_KILLER_INSTINCT": 449,
    "ENHANCES_CHARM": 450, "ENHANCES_CALL_BEAST": 451,
    "ENHANCES_REWARD": 452, "ENHANCES_TAME": 453,
    "ENHANCES_KILLERS": 454,
    "ENHANCES_BARRAGE": 455, "ENHANCES_SHARPSHOT": 456,
    "ENHANCES_STEALTH_SHOT": 457, "ENHANCES_FLASHY_SHOT": 458,
    "ENHANCES_SCAVENGE": 459,
})

# --- special / niche ----------------------------------------------
MOD_ID.update({
    "CURE_POTENCY": 148, "CURE_POTENCY_RCVD": 149,
    "CURE_CAST_TIME_PCT": 158,
    "DRAIN_POTENCY": 244, "DRAIN_ATT_DRAIN": 245, "ASPIR_POTENCY": 246,
    "STUN_ACC": 316, "STUN_POTENCY": 317,
    "PHALANX_POTENCY": 703,
    "STONESKIN_BONUS": 704, "STONESKIN_POTENCY": 704,
    "AUTO_RERAISE": 951,
    "CAPACITY_BONUS": 954,
    "EXP_BONUS": 342,
    "SKILLCHAIN_BONUS": 431,
    "AVATAR_ATT": 300, "AVATAR_ACC": 301, "AVATAR_MATT": 302,
    "AVATAR_MACC": 303, "AVATAR_PERPETUATION": 304,
    "PET_ATT": 306, "PET_ACC": 307, "PET_HP": 308,
    "PET_MP": 309, "PET_MAB": 310,
    "SIC_STR": 320, "SIC_DEX": 321, "SIC_VIT": 322,
})

# --- percentile / caps --------------------------------------------
MOD_ID.update({
    "DA_TYPE_RATE": 288, "TA_TYPE_RATE": 356,
    "AMBUSH": 419, "PERFECT_DODGE": 420,
})


# ====================================================================
# Alias table — tooltip fragments → canonical MOD_ID key
# ====================================================================
# Order matters — more specific patterns come first so "Magic Attack
# Bonus" doesn't get eaten by a bare "Magic" match.

_ALIAS_ENTRIES: list[tuple[str, str]] = [
    # Basic stats
    (r"^DEF$", "DEF"),
    (r"^HP$", "HP"),
    (r"^HP\s*%$", "HP_PERCENT"),
    (r"^MP$", "MP"),
    (r"^MP\s*%$", "MP_PERCENT"),
    (r"^STR$", "STR"),
    (r"^DEX$", "DEX"),
    (r"^VIT$", "VIT"),
    (r"^AGI$", "AGI"),
    (r"^INT$", "INT"),
    (r"^MND$", "MND"),
    (r"^CHR$", "CHR"),

    # Combat
    (r"^Attack$", "ATT"),
    (r"^Ranged\s*Att(?:ack)?$", "RATT"),
    (r"^Accuracy$", "ACC"),
    (r"^Ranged\s*Acc(?:uracy)?$", "RACC"),
    (r"^Enmity$", "ENMITY"),
    (r"^Evasion$", "EVA"),
    (r"^Parry(?:ing)?$", "PARRY"),
    (r"^Guard(?:ing)?$", "GUARD"),
    (r"^Block(?:\s*rate)?$", "BLOCK"),
    (r"^Counter(?:\s*rate)?$", "COUNTER"),

    # Magic
    (r"^Magic\s*Att(?:ack)?(?:\s*Bonus)?$", "MATT"),
    (r"^MAB$", "MAB"),
    (r"^Magic\s*Acc(?:uracy)?$", "MACC"),
    (r"^Magic\s*Def(?:ense|\.)?(?:\s*Bonus)?$", "MDEF"),
    (r"^MDB$", "MDEF"),
    (r"^Magic\s*Damage$", "MDMG"),
    (r"^Magic\s*Evasion$", "MEVA"),
    (r"^Magic\s*Burst(?:\s*damage)?$", "MAGIC_BURST_BONUS"),

    # Attack rate
    (r"^Double\s*Attack$", "DOUBLE_ATTACK"),
    (r"^Triple\s*Attack$", "TRIPLE_ATTACK"),
    (r"^Quad(?:ruple)?\s*Attack$", "QUAD_ATTACK"),
    (r"^Kick\s*Attack(?:s|\s*rate)?$", "KICK_ATTACK_RATE"),
    (r"^Zanshin$", "ZANSHIN"),
    (r"^Martial\s*Arts$", "MARTIAL_ARTS"),
    (r"^Critical\s*Hit\s*Rate$", "CRIT_HIT_RATE"),
    (r"^Crit(?:ical)?\s*Hit\s*Dmg(?:\s*bonus)?$", "CRIT_HIT_DMG"),
    (r"^Magic\s*Crit(?:ical)?\s*Rate$", "MAGIC_CRIT_RATE"),

    # Haste / speed
    (r"^Haste$", "HASTE"),
    (r"^Magic\s*Haste$", "MAGIC_HASTE"),
    (r"^Slow$", "SLOW"),
    (r"^Spell\s*interruption(?:\s*rate)?(?:\s*down)?$", "SPELL_INTERRUPTION_RATE"),
    (r"^Fast\s*Cast$", "FAST_CAST"),
    (r"^Cure\s*cast\s*time$", "CURE_CAST_TIME"),
    (r"^Move(?:ment)?\s*speed$", "MOVE"),

    # TP / WS
    (r"^Store\s*TP$", "STORE_TP"),
    (r"^Regain$", "REGAIN"),
    (r"^Subtle\s*Blow$", "SUBTLE_BLOW"),
    (r"^Subtle\s*Blow\s*II$", "SUBTLE_BLOW_II"),
    (r"^TP\s*Bonus$", "TP_BONUS"),
    (r"^Weaponskill\s*acc(?:uracy)?$", "WS_ACC"),
    (r"^Weaponskill\s*dmg(?:\s*bonus)?$", "WS_DAMAGE"),
    (r"^Occult\s*Acumen$", "OCCULT_ACUMEN"),

    # Regen / refresh
    (r"^Regen$", "REGEN"),
    (r"^Refresh$", "REFRESH"),
    (r"^Convert\s*MP\s*to\s*HP$", "CONVERT_MP_TO_HP"),
    (r"^Drain\s*HP$", "DRAIN_HP"),

    # Elemental resistances
    (r"^Fire\s*Res(?:ist)?$", "FIRERES"),
    (r"^Ice\s*Res(?:ist)?$", "ICERES"),
    (r"^Wind\s*Res(?:ist)?$", "WINDRES"),
    (r"^Earth\s*Res(?:ist)?$", "EARTHRES"),
    (r"^(?:Thunder|Lightning)\s*Res(?:ist)?$", "THUNDERRES"),
    (r"^Water\s*Res(?:ist)?$", "WATERRES"),
    (r"^Light\s*Res(?:ist)?$", "LIGHTRES"),
    (r"^Dark\s*Res(?:ist)?$", "DARKRES"),

    # Status resistances
    (r"^Sleep\s*Res(?:ist)?$", "SLEEPRES"),
    (r"^Poison\s*Res(?:ist)?$", "POISONRES"),
    (r"^Paralyze\s*Res(?:ist)?$", "PARALYZERES"),
    (r"^Blind\s*Res(?:ist)?$", "BLINDRES"),
    (r"^Silence\s*Res(?:ist)?$", "SILENCERES"),
    (r"^Virus\s*Res(?:ist)?$", "VIRUSRES"),
    (r"^Petrify\s*Res(?:ist)?$", "PETRIFYRES"),
    (r"^Bind\s*Res(?:ist)?$", "BINDRES"),
    (r"^Curse\s*Res(?:ist)?$", "CURSERES"),
    (r"^Gravity\s*Res(?:ist)?$", "GRAVITYRES"),
    (r"^Slow\s*Res(?:ist)?$", "SLOWRES"),
    (r"^Stun\s*Res(?:ist)?$", "STUNRES"),
    (r"^Charm\s*Res(?:ist)?$", "CHARMRES"),

    # Weapon skills
    (r"^H2H$", "H2H"), (r"^Hand-to-Hand$", "H2H"),
    (r"^Dagger$", "DAGGER"),
    (r"^Sword$", "SWORD"),
    (r"^Great\s*Sword$", "GREAT_SWORD"),
    (r"^Axe$", "AXE"),
    (r"^Great\s*Axe$", "GREAT_AXE"),
    (r"^Scythe$", "SCYTHE"),
    (r"^Polearm$", "POLEARM"),
    (r"^Katana$", "KATANA"),
    (r"^Great\s*Katana$", "GREAT_KATANA"),
    (r"^Club$", "CLUB"),
    (r"^Staff$", "STAFF"),
    (r"^Archery$", "ARCHERY"),
    (r"^Marksmanship$", "MARKSMANSHIP"),
    (r"^Throwing$", "THROWING"),

    # Magic skills
    (r"^Divine\s*Magic$", "DIVINE_MAGIC"),
    (r"^Healing\s*Magic$", "HEALING_MAGIC"),
    (r"^Enhancing\s*Magic$", "ENHANCING_MAGIC"),
    (r"^Enfeebling\s*Magic$", "ENFEEBLING_MAGIC"),
    (r"^Elemental\s*Magic$", "ELEMENTAL_MAGIC"),
    (r"^Dark\s*Magic$", "DARK_MAGIC"),
    (r"^Summoning\s*Magic$", "SUMMONING_MAGIC"),
    (r"^Ninjutsu$", "NINJUTSU"),
    (r"^Blue\s*Magic$", "BLUE_MAGIC"),
    (r"^Singing$", "SINGING"),
    (r"^String\s*Instrument$", "STRING_INST"),
    (r"^Wind\s*Instrument$", "WIND_INST"),

    # Damage taken
    (r"^Physical\s*Damage\s*taken$", "PHYSICAL_DAMAGE_TAKEN"),
    (r"^Magic\s*Damage\s*taken$", "MAGIC_DAMAGE_TAKEN"),
    (r"^Breath\s*Damage\s*taken$", "BREATH_DAMAGE_TAKEN"),
    (r"^Damage\s*taken$", "DAMAGE_TAKEN"),

    # Job ability / trait enhancements (quoted in the tooltip)
    (r"^(?:Enhances\s+)?\"?Aggressor\"?(?:\s*(?:effect|duration))?$", "ENHANCES_AGGRESSOR"),
    (r"^(?:Enhances\s+)?\"?Berserk\"?(?:\s*(?:effect|duration))?$", "ENHANCES_BERSERK"),
    (r"^(?:Enhances\s+)?\"?Defender\"?(?:\s*(?:effect|duration))?$", "ENHANCES_DEFENDER"),
    (r"^(?:Enhances\s+)?\"?Warcry\"?(?:\s*(?:effect|duration))?$", "ENHANCES_WARCRY"),
    (r"^(?:Enhances\s+)?\"?Meditate\"?(?:\s*(?:effect|duration))?$", "ENHANCES_MEDITATE"),
    (r"^(?:Enhances\s+)?\"?Meikyo\s*Shisui\"?(?:\s*(?:effect|duration))?$", "ENHANCES_MEIKYO"),
    (r"^(?:Enhances\s+)?\"?Hasso\"?(?:\s*(?:effect|duration))?$", "ENHANCES_HASSO"),
    (r"^(?:Enhances\s+)?\"?Seigan\"?(?:\s*(?:effect|duration))?$", "ENHANCES_SEIGAN"),
    (r"^(?:Enhances\s+)?\"?Chi\s*Blast\"?(?:\s*(?:effect|duration|damage))?$", "ENHANCES_CHIBLAST"),
    (r"^(?:Enhances\s+)?\"?Utsusemi\"?(?:\s*(?:effect|duration))?$", "ENHANCES_UTSUSEMI"),
    (r"^(?:Enhances\s+)?\"?Sneak\s*Attack\"?(?:\s*(?:effect|damage))?$", "ENHANCES_SNEAK_ATTACK"),
    (r"^(?:Enhances\s+)?\"?Trick\s*Attack\"?(?:\s*(?:effect|damage))?$", "ENHANCES_TRICK_ATTACK"),
    (r"^(?:Enhances\s+)?\"?Steal\"?(?:\s*(?:effect|duration))?$", "ENHANCES_STEAL"),
    (r"^(?:Enhances\s+)?\"?Mug\"?(?:\s*(?:effect|duration))?$", "ENHANCES_MUG"),
    (r"^(?:Enhances\s+)?\"?Flee\"?(?:\s*(?:effect|duration))?$", "ENHANCES_FLEE"),
    (r"^(?:Enhances\s+)?\"?Provoke\"?(?:\s*(?:effect|duration))?$", "ENHANCES_PROVOKE"),
    (r"^(?:Enhances\s+)?\"?Sentinel\"?(?:\s*(?:effect|duration))?$", "ENHANCES_SENTINEL"),
    (r"^(?:Enhances\s+)?\"?Holy\s*Circle\"?(?:\s*(?:effect|duration))?$", "ENHANCES_HOLY_CIRCLE"),
    (r"^(?:Enhances\s+)?\"?Shield\s*Bash\"?(?:\s*(?:effect|damage))?$", "ENHANCES_SHIELD_BASH"),
    (r"^(?:Enhances\s+)?\"?Arcane\s*Circle\"?(?:\s*(?:effect|duration))?$", "ENHANCES_ARCANE_CIRCLE"),
    (r"^(?:Enhances\s+)?\"?Last\s*Resort\"?(?:\s*(?:effect|duration))?$", "ENHANCES_LAST_RESORT"),
    (r"^(?:Enhances\s+)?\"?Souleater\"?(?:\s*(?:effect|duration))?$", "ENHANCES_SOULEATER"),
    (r"^(?:Enhances\s+)?\"?Barrage\"?(?:\s*(?:effect|damage))?$", "ENHANCES_BARRAGE"),
    (r"^(?:Enhances\s+)?\"?Sharpshot\"?(?:\s*(?:effect|duration))?$", "ENHANCES_SHARPSHOT"),
    (r"^(?:Enhances\s+)?\"?Scavenge\"?(?:\s*(?:effect|duration))?$", "ENHANCES_SCAVENGE"),

    # Cures / healing
    (r"^Cure\s*potency$", "CURE_POTENCY"),
    (r"^Cure\s*potency\s*received$", "CURE_POTENCY_RCVD"),

    # Craft
    (r"^Alchemy$", "SKL_ALCHEMY"),
    (r"^Cooking$", "SKL_COOKING"),
    (r"^Goldsmithing$", "SKL_GOLDSMITHING"),
    (r"^Leathercraft$", "SKL_LEATHERCRAFT"),
    (r"^Woodworking$", "SKL_WOODWORKING"),
    (r"^Smithing$", "SKL_SMITHING"),
    (r"^Clothcraft$", "SKL_CLOTHCRAFT"),
    (r"^Bonecraft$", "SKL_BONECRAFT"),
    (r"^Fishing$", "SKL_FISHING"),
    (r"^Synergy$", "SKL_SYNERGY"),

    # Bonuses
    (r"^Experience\s*Points?(?:\s*Boost)?$", "EXP_BONUS"),
    (r"^Capacity\s*Points?(?:\s*Boost)?$", "CAPACITY_BONUS"),

    # Avatar / Pet
    (r"^Avatar\s*Att(?:ack)?$", "AVATAR_ATT"),
    (r"^Avatar\s*Acc(?:uracy)?$", "AVATAR_ACC"),
    (r"^Avatar\s*Magic\s*Att(?:ack)?$", "AVATAR_MATT"),
    (r"^Avatar\s*Magic\s*Acc(?:uracy)?$", "AVATAR_MACC"),
    (r"^Perpetuation\s*cost$", "AVATAR_PERPETUATION"),
    (r"^Pet\s*Att(?:ack)?$", "PET_ATT"),
    (r"^Pet\s*Acc(?:uracy)?$", "PET_ACC"),
]

_MOD_ALIASES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.I), key) for p, key in _ALIAS_ENTRIES
]


# ====================================================================
# Effects that need a Lua script
# ====================================================================
# When these fire, the exporter still emits a `-- TODO` comment AND
# generates a Lua stub file. The stub gives the operator a starting
# skeleton to fill in the actual behavior.

LUA_EFFECTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Auto\s*Reraise\s*(?:Effect|III|II)?", re.I), "auto_reraise"),
    (re.compile(r"^Set\s*:", re.I), "set_bonus"),
    (re.compile(r"^Latent\s*(?:Effect|Ability)", re.I), "latent"),
    (re.compile(r"^Occ(?:asionally)?\s+", re.I), "occ_effect"),
    (re.compile(r"Additional\s*Effect", re.I), "additional_effect"),
    (re.compile(r"Aftermath", re.I), "aftermath"),
    (re.compile(r"Skillchain\s+bonus", re.I), "skillchain"),
    (re.compile(r"Grants\s+the\s+effect", re.I), "grants_effect"),
    (re.compile(r"^Uses:", re.I), "usable_charge"),
]


@dataclass
class ParsedMod:
    key: str
    value: int
    mod_id: Optional[int] = None
    raw: str = ""


@dataclass
class LuaHint:
    text: str
    stub_key: Optional[str] = None


@dataclass
class ItemExport:
    item: Item
    description_lines: list[str] = field(default_factory=list)
    mods: list[ParsedMod] = field(default_factory=list)
    lua_hints: list[LuaHint] = field(default_factory=list)


# ====================================================================
# Description parsing
# ====================================================================

_FRAGMENT_SPLIT = re.compile(r"[\n\r]+")

# Match "Name+value", "Name -value", or "Name:value". Optional trailing
# %. The name capture is greedy up to the sign so multi-word names
# ("Magic Def. Bonus") work.
_MOD_PATTERN = re.compile(
    r"([A-Za-z][A-Za-z\.\s'\"]*?)\s*"
    r"(?:([+\-])|:)\s*"
    r"(\d+)\s*"
    r"(%?)"
)


def _resolve_alias(name: str) -> Optional[str]:
    """Match a stat name against the alias table, returning a canonical
    MOD_ID key or None."""
    cleaned = name.strip().strip(":").strip('"').strip()
    for pattern, canonical in _MOD_ALIASES:
        if pattern.match(cleaned):
            return canonical
    return None


def _detect_lua_hint(line: str) -> Optional[str]:
    """Return the stub_key if this line looks like a Lua effect."""
    for pattern, key in LUA_EFFECTS:
        if pattern.search(line):
            return key
    return None


def parse_description(text: str) -> tuple[list[ParsedMod], list[LuaHint]]:
    """Parse a description into (mods, lua_hints)."""
    mods: list[ParsedMod] = []
    hints: list[LuaHint] = []

    for line in _FRAGMENT_SPLIT.split(text):
        line = line.strip()
        if not line:
            continue

        lua_key = _detect_lua_hint(line)
        if lua_key:
            hints.append(LuaHint(text=line, stub_key=lua_key))
            continue

        line_matched_anything = False
        for match in _MOD_PATTERN.finditer(line):
            name, sign, value_str, pct = match.groups()
            name = name.strip()
            if not name:
                continue
            value = int(value_str)
            if sign == "-":
                value = -value

            key = _resolve_alias(name)
            if key is None:
                # Unknown mod — emit as a TODO comment with the raw
                # tooltip fragment so the operator can decide.
                hints.append(LuaHint(text=match.group(0).strip()))
                continue

            line_matched_anything = True
            mods.append(
                ParsedMod(
                    key=key,
                    value=value,
                    mod_id=MOD_ID.get(key),
                    raw=match.group(0).strip(),
                )
            )

        # Line had no numeric mod at all AND wasn't a Lua hint. Log it
        # so operators can spot new patterns worth aliasing.
        if not line_matched_anything and not lua_key:
            hints.append(LuaHint(text=line))

    return mods, hints


# ====================================================================
# Item categorisation
# ====================================================================

def is_equipment(item: Item) -> bool:
    if item.item_type == int(ItemType.WEAPON):
        return True
    if item.item_type == int(ItemType.ARMOR):
        return True
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


# ====================================================================
# SQL emission
# ====================================================================

def _sql_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _sortname(name: str) -> str:
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
    resolved = [m for m in mods if m.mod_id is not None]
    if not resolved:
        lines.append(f"-- no item_mods rows parsed for {item.name}")
        return "\n".join(lines)
    lines.append("INSERT INTO item_mods (itemid, modId, value) VALUES")
    values = [
        f"  ({item.id}, {m.mod_id}, {m.value})    -- {m.key} from {m.raw!r}"
        for m in resolved
    ]
    lines.append(",\n".join(values) + ";")
    return "\n".join(lines)


def emit_sql_for_item(export: ItemExport) -> str:
    item = export.item
    lines: list[str] = []
    lines.append("-- " + "-" * 60)
    lines.append(f"-- #{item.id}  {item.name!r}  ({item.item_type_name})")
    for d in export.description_lines[:6]:
        lines.append(f"--   {d}")
    lines.append("-- " + "-" * 60)
    lines.append(emit_item_basic(item))
    if is_equipment(item):
        lines.append(emit_item_equipment(item))
    lines.append(emit_item_mods(item, export.mods))
    for hint in export.lua_hints:
        if hint.stub_key:
            lines.append(f"-- TODO (Lua stub written): {hint.text}")
        else:
            lines.append(f"-- TODO (needs Lua): {hint.text}")
    lines.append("")
    return "\n".join(lines)


def emit_patch(edited_items: Iterable[Item]) -> str:
    header = [
        "-- ================================================================",
        "-- Generated by ffxi-dat-editor",
        "-- Drop this file into your LSB server's sql/ folder (or execute",
        "-- it directly against your zoning DB) after backing up first.",
        "-- ",
        "-- Effects marked -- TODO (Lua stub written) have a matching",
        "-- scripts/globals/items/<sortname>.lua file next to this SQL —",
        "-- open it and fill in the effect body.",
        "-- ",
        "-- Effects marked -- TODO (needs Lua) had no built-in stub; write",
        "-- the script from scratch.",
        "-- ================================================================",
        "",
    ]
    body = [emit_sql_for_item(build_export(it)) for it in edited_items]
    return "\n".join(header) + "\n".join(body)


# ====================================================================
# Lua stub generator
# ====================================================================

_LUA_STUB_HEADERS: dict[str, str] = {
    "auto_reraise": """-- Applies Reraise III on equip, cleared on unequip.
    itemObject.onItemEquip = function(target, item)
        target:addStatusEffect(xi.effect.RERAISE_III, 1, 0, 0)
    end
    itemObject.onItemUnequip = function(target, item)
        target:delStatusEffect(xi.effect.RERAISE_III)
    end
""",
    "set_bonus": """-- Set bonus. LSB expresses set bonuses via item_latents
    -- rows keyed by an "with-set" latent condition. Populate the
    -- setModifiers table below with (mod, value) pairs and register
    -- the set in scripts/globals/itemsets.lua.
    -- setModifiers = { { Mod.ACC, 10 }, { Mod.RATT, 10 } }
""",
    "latent": """-- Latent effect. Fill in the condition (HP%, TP, weather,
    -- etc.) and modifier(s). See scripts/globals/mixins/latent.lua.
""",
    "additional_effect": """-- Additional effect on hit. See other weapons that
    -- use additional_effect: scripts/globals/additional_effects.lua for
    -- available element/status handlers.
""",
    "aftermath": """-- Aftermath effect after weapon skill. See relic/mythic/
    -- empyrean weapons in scripts/globals/items/ for reference impls.
""",
    "occ_effect": """-- "Occasionally ..." proc. Roll RNG on hit and apply
    -- the effect on success.
""",
    "grants_effect": """-- Grants a status effect. Apply on equip via
    -- target:addStatusEffect(...); remove on unequip.
""",
    "usable_charge": """-- "Uses: N" — item has active charges. Implement
    -- onItemCheck / onItemUse. See scripts/globals/items/juju_pouch.lua.
""",
    "skillchain": """-- Skillchain bonus. Adjust skillchain damage in
    -- xi.skillchain.getSkillchainBonus for this item's owner.
""",
}


def emit_lua_stub(export: ItemExport) -> Optional[str]:
    """Return the contents of a Lua stub file for this item, or None if
    the item has no effects that need scripting."""
    stubs = [h for h in export.lua_hints if h.stub_key]
    if not stubs:
        return None

    item = export.item
    sortname = _sortname(item.name)
    lines = [
        f"-----------------------------------",
        f"-- Item: {item.name} (id {item.id})",
        f"-- Auto-generated stub by ffxi-dat-editor.",
        f"-- Fill in each effect body below.",
        f"-----------------------------------",
        f"require(\"scripts/globals/status\")",
        "",
        f"local itemObject = {{}}",
        "",
    ]
    for hint in stubs:
        lines.append(f"-- Effect: {hint.text}")
        body = _LUA_STUB_HEADERS.get(hint.stub_key or "", "-- ??? unhandled stub key\n")
        lines.append(body.rstrip())
        lines.append("")
    lines.extend(
        [
            "-- Default no-op hooks. Delete whichever the item doesn't need.",
            "itemObject.onItemCheck = function(target, item, param)",
            "    return 0",
            "end",
            "",
            "itemObject.onItemUse = function(target, item, param)",
            "end",
            "",
            "return itemObject",
        ]
    )
    return "\n".join(lines) + "\n"


def emit_lua_stubs_for(edited_items: Iterable[Item]) -> list[tuple[str, str]]:
    """Return a list of (filename, contents) for every item that needs
    a Lua stub."""
    out: list[tuple[str, str]] = []
    for item in edited_items:
        export = build_export(item)
        stub = emit_lua_stub(export)
        if stub:
            out.append((f"{_sortname(item.name)}.lua", stub))
    return out
