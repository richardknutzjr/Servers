#!/usr/bin/env python3
"""Flask web UI for the FFXI item DAT editor.

Two ways to launch it:

  # Zero-config (used by the .exe build): pick a free port, open the
  # browser, and let the user upload a DAT through the web UI.
  ./server.py

  # Or explicit for development: pin a port, load a DAT from disk on
  # startup, allow save-in-place.
  ./server.py --path /path/to/item_armor.DAT --port 5000 --allow-in-place
"""

from __future__ import annotations

import argparse
import io
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

from ffxidat import ItemDat, ItemType, Job, RECORD_SIZE, Race, Slot
from ffxidat.cipher import decode_bytes
from ffxidat.items import Item


def _flag_options(flag_cls) -> list[dict]:
    result = []
    for member in flag_cls:
        v = member.value
        if v > 0 and (v & (v - 1)) == 0:
            result.append({"name": member.name, "value": v})
    return result


class EditorState:
    """In-memory state for the currently-loaded DAT.

    A single ``EditorState`` is shared across all requests; there is no
    multi-user story — this is a local desktop tool.
    """

    def __init__(self, dat_path: Path | None, record_size: int, allow_in_place: bool):
        self.record_size = record_size
        self.allow_in_place = allow_in_place
        self.source_path: Path | None = dat_path
        self.filename: str = dat_path.name if dat_path else ""
        self.dat: ItemDat | None = ItemDat.load(dat_path, record_size=record_size) if dat_path else None
        self.lock = threading.Lock()

    def load_from_bytes(self, filename: str, raw: bytes) -> None:
        # Split into records and parse. We don't decode() here; ItemDat
        # does that via from_records/from_plain. Use load-alike path.
        if len(raw) % self.record_size:
            raise ValueError(
                f"file size {len(raw)} is not a multiple of record size "
                f"{self.record_size}; try a different --record-size"
            )
        from ffxidat.cipher import decode_bytes
        records = [
            decode_bytes(raw[i : i + self.record_size])
            for i in range(0, len(raw), self.record_size)
        ]
        self.dat = ItemDat.from_records(records, record_size=self.record_size)
        self.filename = secure_filename(filename) or "item.DAT"
        # Uploaded DATs never overwrite the local path; force download.
        self.source_path = None


def _resource_dir() -> Path:
    """Directory that holds ``templates/`` and ``static/``.

    Regular runs: the folder next to server.py. Under a PyInstaller
    build the same folders live under sys._MEIPASS.
    """
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def create_app(state: EditorState) -> Flask:
    base = _resource_dir()
    app = Flask(
        __name__,
        template_folder=str(base / "templates"),
        static_folder=str(base / "static"),
    )
    # Retail item_armor.DAT is ~40 MB, item_weapon ~25 MB, item_general
    # ~15 MB. 128 MB gives comfortable headroom for future patches and
    # any custom-server DATs.
    app.config["MAX_CONTENT_LENGTH"] = 128 * 1024 * 1024

    @app.after_request
    def _no_cache(resp):
        # Every launch of the .exe gets a fresh random port so URLs
        # differ, but browsers still cache static assets aggressively
        # by path. Disable caching so a redeployed .exe never boots
        # into a UI made of stale JS/CSS from a previous install.
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        return jsonify(
            {
                "loaded": state.dat is not None,
                "filename": state.filename,
                "item_count": len(state.dat.items) if state.dat else 0,
                "has_source_path": state.source_path is not None,
                "allow_in_place": state.allow_in_place,
                "record_size": state.record_size,
            }
        )

    @app.post("/api/open")
    def open_upload():
        if "file" not in request.files:
            abort(400, description="expected a multipart 'file' field")
        f = request.files["file"]
        if not f.filename:
            abort(400, description="no filename")
        with state.lock:
            try:
                state.load_from_bytes(f.filename, f.read())
            except ValueError as exc:
                abort(400, description=str(exc))
        return jsonify({"ok": True, "filename": state.filename, "items": len(state.dat.items)})

    @app.post("/api/open-path")
    def open_local_path():
        """Load a DAT directly from a local filesystem path.

        Only useful when the server runs on the user's own machine
        (i.e. the .exe). It's still gated by having Flask bound to
        127.0.0.1 so only local processes can reach it.
        """
        payload = request.get_json(silent=True) or {}
        p = Path(str(payload.get("path", ""))).expanduser()
        if not p.is_file():
            abort(400, description=f"not a file: {p}")
        with state.lock:
            try:
                state.load_from_bytes(p.name, p.read_bytes())
            except ValueError as exc:
                abort(400, description=str(exc))
        return jsonify({"ok": True, "filename": state.filename, "items": len(state.dat.items), "path": str(p)})

    @app.post("/api/scan")
    def scan_folder():
        """Walk a directory tree for candidate item DATs.

        Returns files whose size is a multiple of the record size, is
        larger than the "definitely not an item DAT" threshold, and
        whose first record decodes into a plausible item header. Sorted
        biggest-first because the interesting DATs are the big ones.
        """
        payload = request.get_json(silent=True) or {}
        root = Path(str(payload.get("path", ""))).expanduser()
        if not root.is_dir():
            abort(400, description=f"not a directory: {root}")

        MIN_SIZE = 512 * 1024  # 512 KB — retail item DATs are megabytes
        MAX_RESULTS = 100
        results = []
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() != ".dat":
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size < MIN_SIZE or size % state.record_size != 0:
                continue
            try:
                with p.open("rb") as fh:
                    first = fh.read(state.record_size)
                item = Item.from_plain(decode_bytes(first))
            except Exception:
                continue
            # Heuristic: item IDs are 16-bit at retail, item type must
            # be one we recognise. Rules out non-item DATs that happen
            # to align on 3072-byte boundaries.
            if item.item_type_name.startswith("UNKNOWN"):
                continue
            if not (1 <= item.id < 100000):
                continue
            results.append(
                {
                    "path": str(p),
                    "size_bytes": size,
                    "size_mb": round(size / (1024 * 1024), 1),
                    "records": size // state.record_size,
                    "sample_id": item.id,
                    "sample_name": item.name,
                    "sample_type": item.item_type_name,
                }
            )
            if len(results) >= MAX_RESULTS:
                break
        results.sort(key=lambda r: r["size_bytes"], reverse=True)
        return jsonify({"root": str(root), "results": results})

    def _require_dat() -> ItemDat:
        if state.dat is None:
            abort(400, description="no DAT loaded; POST /api/open first")
        return state.dat

    @app.get("/api/items")
    def list_items():
        dat = _require_dat()
        offset = int(request.args.get("offset", 0))
        limit = int(request.args.get("limit", 100))
        query = request.args.get("q", "").strip().lower()
        items = dat.items
        if query:
            items = [
                it for it in items
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
        dat = _require_dat()
        for i, it in enumerate(dat.items):
            if it.id == item_id:
                return i
        abort(404, description=f"no item with id {item_id}")

    @app.get("/api/items/<int:item_id>")
    def get_item(item_id: int):
        idx = _find_index(item_id)
        return jsonify(state.dat.items[idx].to_dict())

    @app.put("/api/items/<int:item_id>")
    def put_item(item_id: int):
        payload = request.get_json(silent=True) or {}
        with state.lock:
            idx = _find_index(item_id)
            try:
                state.dat.items[idx].apply_dict(payload)
                # Trigger serialization once as a sanity check — catches
                # strings-block overflow before it lands on disk.
                state.dat.items[idx].to_plain(state.dat.record_size)
            except ValueError as exc:
                abort(400, description=str(exc))
            return jsonify(state.dat.items[idx].to_dict())

    @app.get("/api/download")
    def download():
        """Send the current in-memory DAT to the browser as a download."""
        dat = _require_dat()
        with state.lock:
            payload = dat.to_bytes()
        stem, ext = os.path.splitext(state.filename or "item.DAT")
        suggested = f"{stem}_edited{ext or '.DAT'}"
        buf = io.BytesIO(payload)
        return send_file(
            buf,
            as_attachment=True,
            download_name=suggested,
            mimetype="application/octet-stream",
        )

    @app.post("/api/save")
    def save():
        """Dev endpoint: write to a specific path on the local machine.

        Only useful when running the server yourself with --path. The
        one-click .exe build steers users to /api/download instead.
        """
        payload = request.get_json(silent=True) or {}
        out_path_str = payload.get("path", "")
        dat = _require_dat()
        with state.lock:
            if out_path_str:
                target = Path(out_path_str).expanduser()
                if (
                    state.source_path is not None
                    and target.resolve() == state.source_path.resolve()
                    and not state.allow_in_place
                ):
                    abort(
                        400,
                        description=(
                            "refusing to overwrite the source DAT; restart with "
                            "--allow-in-place or pick a different path"
                        ),
                    )
            else:
                if state.source_path is None or not state.allow_in_place:
                    abort(
                        400,
                        description="no target path given and in-place save is disabled",
                    )
                target = state.source_path
            dat.save(target)
            return jsonify({"saved_to": str(target)})

    @app.get("/api/enums")
    def enums():
        return jsonify(
            {
                "slots": _flag_options(Slot),
                "races": _flag_options(Race),
                "jobs": _flag_options(Job),
                "item_types": [{"name": t.name, "value": int(t)} for t in ItemType],
            }
        )

    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(app.static_folder, filename)

    return app


def _pick_free_port(preferred: int = 0) -> int:
    """Return an available TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", preferred))
        return s.getsockname()[1]


def _run_server_in_thread(app: Flask, host: str, port: int) -> None:
    """Start Flask's dev server on a daemon thread so we can open the browser."""

    def _serve():
        # use_reloader=False is critical inside a thread.
        app.run(host=host, port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=_serve, daemon=True)
    t.start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FFXI item DAT web editor")
    parser.add_argument(
        "--path",
        help="optional: preload a DAT from disk (dev convenience). Without "
        "this, use the web UI to upload one.",
    )
    parser.add_argument(
        "--record-size",
        type=lambda v: int(v, 0),
        default=RECORD_SIZE,
        help=f"record size in bytes (default 0x{RECORD_SIZE:X})",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="port to listen on (0 = pick a free one, default)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="don't auto-open a browser tab",
    )
    parser.add_argument(
        "--allow-in-place",
        action="store_true",
        help="permit /api/save to overwrite the source DAT (off by default)",
    )
    args = parser.parse_args(argv)

    dat_path: Path | None = None
    if args.path:
        p = Path(args.path).expanduser().resolve()
        if not p.is_file():
            print(f"no such file: {p}", file=sys.stderr)
            return 2
        dat_path = p

    state = EditorState(
        dat_path=dat_path,
        record_size=args.record_size,
        allow_in_place=args.allow_in_place,
    )
    app = create_app(state)

    port = args.port or _pick_free_port()
    url = f"http://{args.host}:{port}"

    _run_server_in_thread(app, host=args.host, port=port)
    # Give Flask a moment to bind before opening the browser.
    time.sleep(0.4)
    print()
    print(f"  FFXI DAT Editor is running at  {url}")
    print("  Close this window when you're done.")
    print()
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
