"""Bitmask enums for equipment slots, races, and jobs.

Values match the layout used by the retail item DAT files and mirror
what LandSandBoat uses in scripts/globals/items.lua and job/race enums.
"""

from __future__ import annotations

from enum import IntFlag


class Slot(IntFlag):
    MAIN = 1 << 0
    SUB = 1 << 1
    RANGED = 1 << 2
    AMMO = 1 << 3
    HEAD = 1 << 4
    BODY = 1 << 5
    HANDS = 1 << 6
    LEGS = 1 << 7
    FEET = 1 << 8
    NECK = 1 << 9
    WAIST = 1 << 10
    EAR1 = 1 << 11
    EAR2 = 1 << 12
    RING1 = 1 << 13
    RING2 = 1 << 14
    BACK = 1 << 15


class Race(IntFlag):
    HUME_M = 1 << 0
    HUME_F = 1 << 1
    ELVAAN_M = 1 << 2
    ELVAAN_F = 1 << 3
    TARU_M = 1 << 4
    TARU_F = 1 << 5
    MITHRA = 1 << 6
    GALKA = 1 << 7
    ALL = 0xFF


class Job(IntFlag):
    NONE = 1 << 0
    WAR = 1 << 1
    MNK = 1 << 2
    WHM = 1 << 3
    BLM = 1 << 4
    RDM = 1 << 5
    THF = 1 << 6
    PLD = 1 << 7
    DRK = 1 << 8
    BST = 1 << 9
    BRD = 1 << 10
    RNG = 1 << 11
    SAM = 1 << 12
    NIN = 1 << 13
    DRG = 1 << 14
    SMN = 1 << 15
    BLU = 1 << 16
    COR = 1 << 17
    PUP = 1 << 18
    DNC = 1 << 19
    SCH = 1 << 20
    GEO = 1 << 21
    RUN = 1 << 22


def _is_single_bit(v: int) -> bool:
    return v > 0 and (v & (v - 1)) == 0


def bitmask_to_names(value: int, flag_cls) -> list[str]:
    """Return the names of every single-bit flag set in ``value``.

    Aggregate members like ``Race.ALL`` are skipped so callers get a
    minimal, canonical list.
    """
    return [
        m.name
        for m in flag_cls
        if m.name and _is_single_bit(m.value) and (value & m.value)
    ]


def names_to_bitmask(names, flag_cls) -> int:
    """Combine a list of flag names back into a bitmask."""
    result = 0
    for name in names:
        result |= int(flag_cls[name])
    return result
