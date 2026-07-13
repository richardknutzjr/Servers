#!/usr/bin/env python3
"""CLI for inspecting and batch-editing FFXI item DAT files.

Common workflow:

    # See what's in a DAT.
    ./cli.py dump /path/to/item_armor.DAT --limit 5

    # Export every item to JSON so you can edit stats in your editor.
    ./cli.py dump /path/to/item_armor.DAT --json > armor.json

    # Patch a DAT from JSON (list of item dicts).
    ./cli.py patch /path/to/item_armor.DAT --from armor.json --out armor_custom.DAT

    # Raw cipher access, useful for hex-diffing single records.
    ./cli.py decode /path/to/item_armor.DAT --record 12 > record12.bin
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ffxidat import ItemDat, RECORD_SIZE, decode_bytes, encode_bytes


def _cmd_dump(args: argparse.Namespace) -> int:
    dat = ItemDat.load(args.path, record_size=args.record_size)
    items = dat.items[: args.limit] if args.limit else dat.items
    if args.json:
        json.dump([item.to_dict() for item in items], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    for item in items:
        print(
            f"#{item.id:<6} type={item.item_type_name:<10} lvl={item.level:<3} "
            f"slots={','.join(item.slot_names) or '-':<20} jobs={','.join(item.job_names) or '-'} "
            f"name={item.name!r}"
        )
    return 0


def _cmd_patch(args: argparse.Namespace) -> int:
    dat = ItemDat.load(args.path, record_size=args.record_size)
    patch_data = json.loads(Path(args.from_json).read_text())
    if not isinstance(patch_data, list):
        raise SystemExit("patch JSON must be a list of item dicts")

    by_id = {item.id: item for item in dat.items}
    applied = 0
    for entry in patch_data:
        item_id = int(entry["id"])
        target = by_id.get(item_id)
        if target is None:
            print(f"warn: item id {item_id} not present in DAT; skipping", file=sys.stderr)
            continue
        target.apply_dict(entry)
        applied += 1

    out_path = Path(args.out) if args.out else Path(args.path)
    dat.save(out_path)
    print(f"patched {applied} item(s); wrote {out_path}")
    return 0


def _cmd_decode(args: argparse.Namespace) -> int:
    raw = Path(args.path).read_bytes()
    if args.record is not None:
        start = args.record * args.record_size
        end = start + args.record_size
        if end > len(raw):
            raise SystemExit(f"record {args.record} out of range")
        sys.stdout.buffer.write(decode_bytes(raw[start:end]))
    else:
        sys.stdout.buffer.write(decode_bytes(raw))
    return 0


def _cmd_encode(args: argparse.Namespace) -> int:
    raw = Path(args.path).read_bytes()
    sys.stdout.buffer.write(encode_bytes(raw))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FFXI item DAT editor")
    parser.add_argument(
        "--record-size",
        type=lambda v: int(v, 0),
        default=RECORD_SIZE,
        help=f"record size in bytes (default 0x{RECORD_SIZE:X})",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dump = sub.add_parser("dump", help="list or export items")
    p_dump.add_argument("path")
    p_dump.add_argument("--limit", type=int, help="only show the first N items")
    p_dump.add_argument("--json", action="store_true", help="emit JSON")
    p_dump.set_defaults(func=_cmd_dump)

    p_patch = sub.add_parser("patch", help="apply a JSON patch to a DAT")
    p_patch.add_argument("path")
    p_patch.add_argument("--from", dest="from_json", required=True, help="patch JSON file")
    p_patch.add_argument("--out", help="output DAT path (default: overwrite input)")
    p_patch.set_defaults(func=_cmd_patch)

    p_dec = sub.add_parser("decode", help="write the rotation-decoded bytes to stdout")
    p_dec.add_argument("path")
    p_dec.add_argument("--record", type=int, help="only decode this record index")
    p_dec.set_defaults(func=_cmd_decode)

    p_enc = sub.add_parser("encode", help="write the rotation-encoded bytes to stdout")
    p_enc.add_argument("path")
    p_enc.set_defaults(func=_cmd_encode)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
