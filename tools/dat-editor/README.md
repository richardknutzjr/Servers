# FFXI DAT Editor

A small Python + Flask editor for FFXI item DAT files, aimed at private
server operators who want to author custom gear for their relaunch.

It reads the retail 0xC00-byte item records (rotation-decoded), lets you
tweak the fields that matter for gear (item type, level, slots, jobs,
races, weapon damage/delay, etc.), and writes them back out. Bytes we
don't understand are preserved verbatim, so unknown regions of a record
survive a round-trip untouched.

## What it can edit

For each item you can change:

- **Name and description** (and log-name variants) — free-text edit in
  the web UI, `strings_block` field in JSON patches. New text lengths
  can differ from the originals; the offset table is recomputed on
  save. If a rewrite would overflow the strings block, the edit is
  rejected instead of silently corrupting the record.
- Basics: id, level to equip, item level, flags, stack size, valid
  targets, model id, resource id
- Slots (MAIN, SUB, RANGED, AMMO, HEAD, BODY, HANDS, LEGS, FEET, NECK,
  WAIST, EAR1/2, RING1/2, BACK) as checkboxes
- Race / job restrictions as checkboxes
- Weapon fields: damage, delay, DPS, skill, jug size
- Timings: casting time, use delay, reuse delay, max charges, shield
  size / ranged skill

## What it can't do (yet)

- **Icon data** is preserved but not decoded/re-encoded. Import a fresh
  icon with an external tool if you need to change art.
- **Non-English strings** decode as latin-1 for editing. If your DAT
  holds Shift-JIS or another codepage, the parser will still round-trip
  those bytes byte-identically as long as you don't touch them, but
  editing to a new Japanese name from the UI needs a follow-up.

## Install

```
python3 -m pip install -r requirements.txt
```

Python 3.10+; the only runtime dependency is Flask (for the web UI).
The CLI has no runtime deps beyond the standard library.

## Workflow

Always keep an untouched backup of your DAT before editing:

```
cp /path/to/ROM/xxx/xxx.DAT ~/dat-backups/
```

Then either browse and edit visually:

```
./server.py /path/to/item_armor.DAT
# then open http://127.0.0.1:5000
```

The web UI writes to a target path you choose (safer default). If you
want to overwrite the source file directly, pass `--allow-in-place`.

Or dump and patch from the shell:

```
# See what's in a DAT.
./cli.py dump /path/to/item_armor.DAT --limit 20

# Export every item to JSON.
./cli.py dump /path/to/item_armor.DAT --json > armor.json

# Edit armor.json in your editor, then feed it back in.
./cli.py patch /path/to/item_armor.DAT --from armor.json --out armor_custom.DAT
```

Patch JSON is just a list of item dicts. Only fields you include are
touched; every other field on an item is kept as-is:

```json
[
  { "id": 15001, "level": 75, "slot_names": ["HEAD"], "job_names": ["WAR", "PLD", "DRK"] },
  { "id": 15002, "damage": 100, "delay": 240 },
  {
    "id": 15003,
    "strings_block": {
      "entries": [
        { "text": "Void Crown" },
        { "text": "void crown" },
        { "text": "void crowns" },
        { "text": "DEF: 99 Lv 1 All Jobs. A crown of dark energy." }
      ]
    }
  }
]
```

The `strings_block.entries` list must keep the same length as the item's
existing entries — retail item records use a fixed slot count per item
type and the client indexes into it. You can change the *text* freely.

## File and record layout

Retail item DAT files hold fixed-size records back-to-back. The default
record size is `0xC00` (3072) bytes, which is what
`item_armor.DAT`, `item_weapon.DAT`, and friends use. If you're working
on a DAT with a different record size, pass `--record-size`.

Each record is byte-rotated: a byte `b` in the file is `((plain << 3) |
(plain >> 5)) & 0xFF`. `ffxidat.cipher` handles both directions.

The first `0x28` bytes of a decoded record are the equipment header:

| Offset | Size | Field           |
|--------|------|-----------------|
| 0x00   | 4    | id              |
| 0x04   | 2    | flags           |
| 0x06   | 2    | stack_size      |
| 0x08   | 2    | item_type       |
| 0x0A   | 2    | resource_id     |
| 0x0C   | 2    | valid_targets   |
| 0x0E   | 2    | level           |
| 0x10   | 2    | slots bitmask   |
| 0x12   | 2    | races bitmask   |
| 0x14   | 4    | jobs bitmask    |
| 0x18   | 2    | superior_level  |
| 0x1A   | 2    | shield_size     |
| 0x1C   | 1    | max_charges     |
| 0x1D   | 1    | casting_time    |
| 0x1E   | 2    | use_delay       |
| 0x20   | 4    | reuse_delay     |
| 0x24   | 2    | model           |
| 0x26   | 2    | unknown         |

For `item_type == WEAPON` there are five more fields immediately after:

| Offset | Size | Field    |
|--------|------|----------|
| 0x28   | 2    | damage   |
| 0x2A   | 2    | delay    |
| 0x2C   | 2    | dps      |
| 0x2E   | 1    | skill    |
| 0x2F   | 1    | jug_size |

After that comes the **strings block** (600 bytes for equipment, 592 for
weapons) and then the icon (2432 bytes) plus padding out to `0xC00`. The
strings block is a small `count / offset-table / entries` structure:

    offset   size   field
    0x00     4      count (number of entries)
    0x04     4      header_flag (usually 0x00000001)
    0x08     8*N    entries[N] of (u32 offset_from_block_start, u32 kind)

    for kind == 0 (string):
        u32 flag
        u32 length
        char data[length]
        u8  NUL

    for kind == 1 (integer):
        u32 value

Blocks that don't validate against this layout (e.g. non-item DAT
records) are left untouched — the tool falls back to preserving the raw
bytes so unknown formats round-trip byte-for-byte.

The icon region is preserved verbatim; edit art with an external tool.

## Tests

```
python3 -m unittest tests.test_roundtrip -v
```

The tests never touch real game files — they synthesise records with
known field values and confirm every field survives the encode → parse →
edit → save → parse round trip. No copyrighted data ships with this tool.

## Legal

This tool is for editing DAT files you already own on your own server.
Square Enix owns FFXI. See the repository root `README.md`.
