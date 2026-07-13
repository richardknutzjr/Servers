#!/usr/bin/env python3
"""Flask web UI for the FFXI item DAT editor.

Point it at an item DAT file and it serves a browser-based editor:
list items, tweak stats/slots/jobs/races, save the result to a new
file. Save-to-original is intentionally opt-in via --allow-in-place.

Run:

    ./server.py /path/to/item_armor.DAT
    # then browse to http://127.0.0.1:5000
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from ffxidat import ItemDat, ItemType, Job, RECORD_SIZE, Race, Slot


def _flag_options(flag_cls) -> list[dict]:
    """Build [{name, value}] for every single-bit member (skips ALL etc.)."""
    result = []
    for member in flag_cls:
        v = member.value
        if v > 0 and (v & (v - 1)) == 0:
            result.append({"name": member.name, "value": v})
    return result


def create_app(dat_path: Path, record_size: int, allow_in_place: bool) -> Flask:
    app = Flask(__name__)
    state = {"dat": ItemDat.load(dat_path, record_size=record_size)}
    lock = threading.Lock()

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            dat_path=str(dat_path),
            item_count=len(state["dat"].items),
            allow_in_place=allow_in_place,
        )

    @app.get("/api/items")
    def list_items():
        dat = state["dat"]
        offset = int(request.args.get("offset", 0))
        limit = int(request.args.get("limit", 100))
        query = request.args.get("q", "").strip().lower()
        items = dat.items
        if query:
            items = [
                it
                for it in items
                if query in it.name.lower() or query in str(it.id)
            ]
        window = items[offset : offset + limit]
        return jsonify(
            {
                "total": len(items),
                "offset": offset,
                "limit": limit,
                "items": [
                    {
                        "id": it.id,
                        "name": it.name,
                        "item_type_name": it.item_type_name,
                        "level": it.level,
                        "slot_names": it.slot_names,
                        "job_names": it.job_names,
                    }
                    for it in window
                ],
            }
        )

    def _find_index(item_id: int) -> int:
        for i, it in enumerate(state["dat"].items):
            if it.id == item_id:
                return i
        abort(404, description=f"no item with id {item_id}")

    @app.get("/api/items/<int:item_id>")
    def get_item(item_id: int):
        idx = _find_index(item_id)
        return jsonify(state["dat"].items[idx].to_dict())

    @app.put("/api/items/<int:item_id>")
    def put_item(item_id: int):
        payload = request.get_json(silent=True) or {}
        with lock:
            idx = _find_index(item_id)
            state["dat"].items[idx].apply_dict(payload)
            return jsonify(state["dat"].items[idx].to_dict())

    @app.post("/api/save")
    def save():
        payload = request.get_json(silent=True) or {}
        out_path_str = payload.get("path", "")
        with lock:
            if out_path_str:
                target = Path(out_path_str).expanduser()
                if target.resolve() == dat_path.resolve() and not allow_in_place:
                    abort(
                        400,
                        description=(
                            "refusing to overwrite the source DAT; restart with "
                            "--allow-in-place or pick a different path"
                        ),
                    )
            else:
                if not allow_in_place:
                    abort(
                        400,
                        description="no target path given and in-place save is disabled",
                    )
                target = dat_path
            state["dat"].save(target)
            return jsonify({"saved_to": str(target)})

    @app.get("/api/enums")
    def enums():
        return jsonify(
            {
                "slots": _flag_options(Slot),
                "races": _flag_options(Race),
                "jobs": _flag_options(Job),
                "item_types": [
                    {"name": t.name, "value": int(t)} for t in ItemType
                ],
            }
        )

    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(app.static_folder, filename)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FFXI item DAT web editor")
    parser.add_argument("path", help="path to an item_*.DAT file")
    parser.add_argument(
        "--record-size",
        type=lambda v: int(v, 0),
        default=RECORD_SIZE,
        help=f"record size in bytes (default 0x{RECORD_SIZE:X})",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument(
        "--allow-in-place",
        action="store_true",
        help="permit /api/save to overwrite the source DAT (off by default)",
    )
    args = parser.parse_args(argv)

    dat_path = Path(args.path).expanduser().resolve()
    if not dat_path.is_file():
        print(f"no such file: {dat_path}", file=sys.stderr)
        return 2

    app = create_app(dat_path, record_size=args.record_size, allow_in_place=args.allow_in_place)
    app.run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
