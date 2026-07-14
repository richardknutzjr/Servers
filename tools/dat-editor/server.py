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
from ffxidat import lsb_export


def _flat_backup_name(path: Path) -> str:
    """Produce a flat filename that preserves enough of ``path`` to
    identify where it came from, without creating deep subfolders in
    the backup dir. Drive letter dropped, path separators become
    underscores. ``C:\\Foo\\Bar\\73.DAT`` becomes ``Foo_Bar_73.DAT``.
    """
    _, tail = os.path.splitdrive(str(path))
    return (
        tail.lstrip("\\/")
        .replace("\\", "_")
        .replace("/", "_")
    )


_DEPLOY_BAT_TEMPLATE = r"""@echo off
setlocal enabledelayedexpansion
rem ---------------------------------------------------------------
rem  deploy.bat — apply generated SQL patches to the LSB database.
rem  Written once by ffxi-dat-editor's Deploy button; edit the
rem  MySQL connection block below and keep it customized.
rem ---------------------------------------------------------------

rem -- MySQL connection ------------------------------------------------
set MYSQL_EXE=C:\xampp\mysql\bin\mysql.exe
set DB_HOST=127.0.0.1
set DB_PORT=3306
set DB_USER=root
set DB_PASS=
set DB_NAME=xidb

rem -- Where the generated .sql files live (this script's own dir) ----
set SQL_DIR=%~dp0

echo Applying every *_lsb_patch.sql in %SQL_DIR% ...
for %%F in ("%SQL_DIR%*_lsb_patch.sql") do (
    echo   -- %%~nxF
    "%MYSQL_EXE%" -h %DB_HOST% -P %DB_PORT% -u %DB_USER% -p%DB_PASS% %DB_NAME% < "%%F"
    if errorlevel 1 (
        echo   FAILED to apply %%~nxF ^(errorlevel !errorlevel!^)
        exit /b 1
    )
)

echo All patches applied. Any -- TODO ^(needs Lua^) lines in the .sql
echo files still need a matching item script under
echo scripts/globals/items/^<sortname^>.lua on the server side.
endlocal
"""


def _looks_like_mesh_dat(head: bytes) -> bool:
    """A mesh/model DAT's first bytes are readable ASCII tags like
    ``mt_0`` or ``b401``; an item DAT's leading bytes are rotation-
    encoded and visually random. Reject anything with >=3 printable
    ASCII bytes in the first 8 raw bytes."""
    printable = sum(1 for b in head[:8] if 0x21 <= b < 0x7F)
    return printable >= 3


def _sample_indices(record_count: int) -> list[int]:
    """Pick a handful of records spread through the file. Guaranteed to
    include the first and last records so short files still sample OK."""
    if record_count <= 1:
        return [0]
    if record_count <= 8:
        return list(range(record_count))
    return [
        0,
        record_count // 8,
        record_count // 4,
        record_count // 2,
        (3 * record_count) // 4,
        (7 * record_count) // 8,
        record_count - 1,
    ]


def _dat_category_label(dominant_type: str) -> str:
    """Map an ItemType name to a human-friendly DAT category label."""
    mapping = {
        "WEAPON": "Weapons",
        "ARMOR": "Armor",
        "USABLE": "Usable items",
        "CRYSTAL": "Crystals",
        "CURRENCY": "Currency",
        "FURNISHING": "Furnishings",
        "PLANT": "Plants / gardens",
        "FLOWERPOT": "Flowerpots",
        "PUPPET_ITEM": "Puppet parts",
        "MANNEQUIN": "Mannequins",
        "BOOK": "Books / general items",
        "LINKSHELL": "Linkshells",
        "ITEM": "General items",
    }
    return mapping.get(dominant_type, dominant_type.title())


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

    def __init__(
        self,
        dat_path: Path | None,
        record_size: int,
        allow_in_place: bool,
        sql_dir: Path | None = None,
        dat_dir: Path | None = None,
        backup_dir: Path | None = None,
    ):
        self.record_size = record_size
        self.allow_in_place = allow_in_place
        self.source_path: Path | None = dat_path
        self.filename: str = dat_path.name if dat_path else ""
        self.dat: ItemDat | None = ItemDat.load(dat_path, record_size=record_size) if dat_path else None
        # Byte-level snapshot of each record as-loaded. Used to detect
        # which items the user has edited so SQL export only emits
        # rows for the ones that actually changed.
        self.original_record_bytes: list[bytes] = (
            [it.to_plain(self.record_size) for it in self.dat.items] if self.dat else []
        )
        # Deploy paths — where "Deploy edits" writes SQL, edited DATs,
        # and any pre-existing files it displaces. Defaults are matched
        # to the operator's server-relaunch tree; override at launch
        # with --sql-dir / --dat-dir / --backup-dir.
        self.sql_dir: Path | None = sql_dir
        self.dat_dir: Path | None = dat_dir
        self.backup_dir: Path | None = backup_dir
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
        self.original_record_bytes = [
            it.to_plain(self.record_size) for it in self.dat.items
        ]
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
            # Preserve the source path so Deploy can figure out the
            # correct <subpath> after ROM/ when writing the edited DAT.
            # load_from_bytes clears it (safe default for uploads);
            # restore it here since we opened by explicit local path.
            state.source_path = p
        return jsonify({"ok": True, "filename": state.filename, "items": len(state.dat.items), "path": str(p)})

    @app.post("/api/scan")
    def scan_folder():
        """Walk a directory tree for candidate item DATs.

        Returns files that:
          * are big enough to be an item DAT (>= 512 KB),
          * are divisible by the record size,
          * don't start with obvious non-item markers (mesh DATs
            begin with readable ASCII tags like "mt_" or "b###"),
          * sample cleanly as item records across multiple positions,
        and categorises each by the dominant item type across the
        samples so the user can tell "the weapon DAT" from "the armor
        DAT" from "the general items DAT" without opening every one.
        """
        payload = request.get_json(silent=True) or {}
        root = Path(str(payload.get("path", ""))).expanduser()
        if not root.is_dir():
            abort(400, description=f"not a directory: {root}")

        MIN_SIZE = 512 * 1024
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

            record_count = size // state.record_size
            if record_count < 5:
                continue

            try:
                with p.open("rb") as fh:
                    # Look at the raw (undecoded) first bytes. Model /
                    # mesh DATs start with readable ASCII tags; item
                    # DATs are byte-rotated so their leading bytes are
                    # visually random. Any printable ASCII run in the
                    # first 8 raw bytes means this isn't an item DAT.
                    head = fh.read(16)
                    if _looks_like_mesh_dat(head):
                        continue

                    # Sample records spread across the file so a single
                    # coincidentally-plausible record can't smuggle a
                    # non-item DAT through.
                    sample_indices = _sample_indices(record_count)
                    types_seen: dict[str, int] = {}
                    names_seen: list[str] = []
                    example_id = None
                    for idx in sample_indices:
                        fh.seek(idx * state.record_size)
                        raw = fh.read(state.record_size)
                        if len(raw) != state.record_size:
                            continue
                        try:
                            item = Item.from_plain(decode_bytes(raw))
                        except Exception:
                            continue
                        if item.item_type_name.startswith("UNKNOWN"):
                            continue
                        if not (1 <= item.id < 100000):
                            continue
                        types_seen[item.item_type_name] = types_seen.get(item.item_type_name, 0) + 1
                        names_seen.append(item.name)
                        if example_id is None:
                            example_id = item.id
            except OSError:
                continue

            # Require a majority of samples to look like real items.
            if sum(types_seen.values()) < max(3, len(_sample_indices(record_count)) * 3 // 4):
                continue

            dominant_type = max(types_seen, key=types_seen.get)
            label = _dat_category_label(dominant_type)
            results.append(
                {
                    "path": str(p),
                    "size_bytes": size,
                    "size_mb": round(size / (1024 * 1024), 1),
                    "records": record_count,
                    "category": label,
                    "dominant_type": dominant_type,
                    "sample_id": example_id,
                    "sample_name": names_seen[0] if names_seen else "",
                    "sample_type": dominant_type,
                    "sample_names": names_seen[:5],
                    "type_counts": types_seen,
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

    @app.get("/api/items/<int:item_id>/raw")
    def get_item_raw(item_id: int):
        """Return the raw plaintext bytes of the item's record as hex.

        Debug-only endpoint used to reverse-engineer field layouts for
        DAT variants the parser doesn't understand yet.
        """
        idx = _find_index(item_id)
        item = state.dat.items[idx]
        plain = item.to_plain(state.dat.record_size)
        return jsonify(
            {
                "id": item.id,
                "record_index": idx,
                "record_size": len(plain),
                "hex_header_128": plain[:128].hex(),
                "hex_first_512": plain[:512].hex(),
            }
        )

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

    def _edited_items() -> list[Item]:
        """Return every item whose bytes have changed since load."""
        dat = _require_dat()
        edited: list[Item] = []
        for i, item in enumerate(dat.items):
            current = item.to_plain(state.record_size)
            original = (
                state.original_record_bytes[i]
                if i < len(state.original_record_bytes)
                else None
            )
            if original is not None and current != original:
                edited.append(item)
        return edited

    @app.get("/api/edited")
    def list_edited():
        """Return a lightweight list of every item that's been edited."""
        edited = _edited_items()
        return jsonify(
            {
                "count": len(edited),
                "items": [
                    {
                        "id": it.id,
                        "name": it.name,
                        "item_type_name": it.item_type_name,
                    }
                    for it in edited
                ],
            }
        )

    @app.get("/api/export-sql")
    def export_sql():
        """Emit an LSB SQL patch for every edited item."""
        edited = _edited_items()
        sql = lsb_export.emit_patch(edited)
        buf = io.BytesIO(sql.encode("utf-8"))
        stem, _ = os.path.splitext(state.filename or "item.DAT")
        return send_file(
            buf,
            as_attachment=True,
            download_name=f"{stem}_lsb_patch.sql",
            mimetype="text/sql",
        )

    @app.get("/api/deploy/config")
    def deploy_config():
        """Report the configured deploy paths back to the UI."""
        return jsonify(
            {
                "sql_dir": str(state.sql_dir) if state.sql_dir else None,
                "dat_dir": str(state.dat_dir) if state.dat_dir else None,
                "backup_dir": str(state.backup_dir) if state.backup_dir else None,
                "source_path": str(state.source_path) if state.source_path else None,
                "dat_target": str(_dat_target_path()) if state.dat_dir else None,
            }
        )

    def _dat_target_path() -> Path | None:
        """Where the edited DAT will land under dat_dir.

        Resolves the target subpath in this order:
          1. If the source path contains a ``ROM`` segment (case-
             insensitive), use everything after it. e.g.
             ``C:\\...\\FFXI\\ROM\\286\\73.DAT`` -> ``286\\73.DAT``.
          2. Fallback: use the source path's parent-folder + filename.
             So a source at ``D:\\wherever\\286\\73.DAT`` still lands
             at ``<dat_dir>\\286\\73.DAT``.
          3. Last resort (upload, no source path): bare filename.
        """
        if state.dat_dir is None:
            return None
        subpath = Path(state.filename or "item.DAT")
        src = state.source_path
        if src is not None:
            parts = list(src.parts)
            found_rom_idx: int | None = None
            for i, part in enumerate(parts):
                if part.upper() == "ROM":
                    found_rom_idx = i
                    break
            if found_rom_idx is not None and found_rom_idx + 1 < len(parts):
                subpath = Path(*parts[found_rom_idx + 1 :])
            elif len(parts) >= 2:
                subpath = Path(parts[-2], parts[-1])
        return state.dat_dir / subpath

    def _timestamped_backup_dir() -> Path:
        """A unique subfolder under backup_dir stamped with the wall
        clock. Ensures every deploy has its own snapshot."""
        import datetime
        assert state.backup_dir is not None
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return state.backup_dir / stamp

    def _backup_and_write(target: Path, payload: bytes, backup_root: Path) -> str:
        """Write ``payload`` to ``target``. If ``target`` already
        exists, rename the prior file *in place* to
        ``<stem>_backup_<timestamp><ext>`` so the backup sits right
        next to the new file (matches the operator's mental model).
        Returns a status string.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_note = ""
        if target.exists():
            import datetime
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            renamed = target.with_name(f"{target.stem}_backup_{stamp}{target.suffix}")
            # Avoid overwriting an existing backup with the same second
            # (unlikely, but defensive).
            i = 1
            while renamed.exists():
                renamed = target.with_name(
                    f"{target.stem}_backup_{stamp}_{i}{target.suffix}"
                )
                i += 1
            target.rename(renamed)
            backup_note = f" (renamed prior version to {renamed.name})"
        target.write_bytes(payload)
        return f"wrote {target}{backup_note}"

    def _snapshot_source_dat(backup_root: Path) -> str | None:
        """Copy the currently-loaded source DAT (as-loaded, before any
        edits) into the backup root so there's always a rollback point.

        Only runs when we know the source path (i.e. the DAT was
        loaded via /api/open-path, not uploaded through the browser
        where we only got bytes without a path). Returns a note or None.
        """
        if state.source_path is None or not state.source_path.exists():
            return None
        backup_root.mkdir(parents=True, exist_ok=True)
        target = backup_root / f"source_{_flat_backup_name(state.source_path)}"
        target.write_bytes(state.source_path.read_bytes())
        return f"snapshotted source DAT to {target}"

    @app.post("/api/deploy")
    def deploy():
        """Write edited DAT + generated SQL to the configured paths,
        backing up any pre-existing file first. This is the primary
        "commit my changes" button — no browser download in between.
        """
        if state.dat is None:
            abort(400, description="no DAT loaded")
        if state.sql_dir is None or state.dat_dir is None or state.backup_dir is None:
            abort(
                400,
                description=(
                    "deploy paths not configured. Restart with --sql-dir / "
                    "--dat-dir / --backup-dir, or set the equivalents in the "
                    "environment (DAT_EDITOR_SQL_DIR / _DAT_DIR / _BACKUP_DIR)."
                ),
            )

        edited = _edited_items()

        with state.lock:
            dat_bytes = state.dat.to_bytes()
            sql_bytes = lsb_export.emit_patch(edited).encode("utf-8")

        backup_root = _timestamped_backup_dir()

        stem, ext = os.path.splitext(state.filename or "item.DAT")
        sql_target = state.sql_dir / f"{stem}_lsb_patch.sql"
        dat_target = _dat_target_path()
        assert dat_target is not None  # dat_dir is set, so this is fine

        try:
            source_note = _snapshot_source_dat(backup_root)
            sql_note = _backup_and_write(sql_target, sql_bytes, backup_root)
            dat_note = _backup_and_write(dat_target, dat_bytes, backup_root)
            bat_note = _write_deploy_bat_if_missing(state.sql_dir)
            lua_notes = _write_lua_stubs(edited, state.sql_dir, backup_root)
        except OSError as exc:
            abort(500, description=f"filesystem error: {exc}")

        notes = [n for n in (source_note, sql_note, dat_note, bat_note, *lua_notes) if n]
        backup_created = backup_root.exists() and any(backup_root.iterdir())
        return jsonify(
            {
                "ok": True,
                "edited_count": len(edited),
                "sql_path": str(sql_target),
                "dat_path": str(dat_target),
                "backup_root": str(backup_root) if backup_created else None,
                "notes": notes,
            }
        )

    def _write_lua_stubs(edited: list, sql_dir: Path, backup_root: Path) -> list[str]:
        """Emit a Lua stub file for every edited item whose description
        contains an effect that needs a script hook. Files land in
        ``sql_dir / lua_stubs/`` so they're easy to copy into
        ``scripts/globals/items/`` on the server."""
        stub_dir = sql_dir / "lua_stubs"
        stubs = lsb_export.emit_lua_stubs_for(edited)
        if not stubs:
            return []
        stub_dir.mkdir(parents=True, exist_ok=True)
        notes: list[str] = []
        for filename, contents in stubs:
            target = stub_dir / filename
            if target.exists():
                # Preserve prior version once so re-deploys don't
                # trample hand-edited stubs.
                backup_root.mkdir(parents=True, exist_ok=True)
                (backup_root / f"overwritten_lua_{filename}").write_bytes(target.read_bytes())
                notes.append(f"wrote {target} (prior version backed up)")
            else:
                notes.append(f"wrote Lua stub {target}")
            target.write_text(contents, encoding="utf-8")
        return notes


    def _write_deploy_bat_if_missing(sql_dir: Path) -> str:
        """Drop a template `deploy.bat` next to the SQL files the first
        time we deploy. Never overwrites an existing one — the operator
        is expected to fill in their MySQL credentials once and keep
        it customised."""
        bat_path = sql_dir / "deploy.bat"
        if bat_path.exists():
            return f"deploy.bat already present at {bat_path} (not touched)"
        bat_path.write_text(_DEPLOY_BAT_TEMPLATE, encoding="utf-8")
        return f"wrote deploy.bat template to {bat_path} — edit the MySQL creds at the top before first use"

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

    # Deploy paths — SQL, DAT, and backup destinations for the
    # "Deploy edits" button. Defaults match the operator's typical
    # server-relaunch layout under D:\; every one can be overridden
    # via CLI arg or the matching DAT_EDITOR_*_DIR env var.
    default_sql_dir = os.environ.get(
        "DAT_EDITOR_SQL_DIR",
        r"D:\server_relaunch\modules\custom\sql",
    )
    default_dat_dir = os.environ.get(
        "DAT_EDITOR_DAT_DIR",
        r"D:\server_relaunch\Custom DATs\Relaunch Custom DATs\ROM",
    )
    default_backup_dir = os.environ.get(
        "DAT_EDITOR_BACKUP_DIR",
        r"D:\server_relaunch\modules\backupdatsqls",
    )
    parser.add_argument(
        "--sql-dir",
        default=default_sql_dir,
        help=f"where Deploy writes the SQL patch (default: {default_sql_dir})",
    )
    parser.add_argument(
        "--dat-dir",
        default=default_dat_dir,
        help=(
            f"where Deploy writes the edited DAT, preserving any subpath "
            f"after ROM/ from the source (default: {default_dat_dir})"
        ),
    )
    parser.add_argument(
        "--backup-dir",
        default=default_backup_dir,
        help=(
            f"where Deploy copies any file it's about to overwrite, "
            f"under a timestamped subfolder (default: {default_backup_dir})"
        ),
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
        sql_dir=Path(args.sql_dir).expanduser() if args.sql_dir else None,
        dat_dir=Path(args.dat_dir).expanduser() if args.dat_dir else None,
        backup_dir=Path(args.backup_dir).expanduser() if args.backup_dir else None,
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
