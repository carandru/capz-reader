from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from . import state
from .auth import require_ajax, require_user
from .config import LIBRARY_ROOT
from .db import db
from .schemas import ImportBody
from .scanning import perform_scan, purge_missing, register_file
from .utils import SUPPORTED_BOOK_SUFFIXES, file_type_for_path, natural_key, safe_dir

router = APIRouter(prefix="/api", tags=["scan"], dependencies=[Depends(require_user)])


@router.post("/scan", dependencies=[Depends(require_ajax)])
def scan():
    if state.scan_state.get("running"):
        return {"ok": True, "already_running": True, "scan": state.scan_state}
    threading.Thread(target=perform_scan, name="manual-scan", daemon=True).start()
    return {"ok": True, "scan": state.scan_state}


@router.get("/scan/status")
def scan_status():
    return state.scan_state


@router.post("/missing/clean", dependencies=[Depends(require_ajax)])
def clean_missing():
    if state.scan_state.get("running"):
        raise HTTPException(409, "Wait for the current rescan to finish")
    return {"ok": True, "purged": purge_missing(force=True)}


@router.get("/import/browse")
def import_browse(path: str = ""):
    root = safe_dir(path)
    rel_root = "" if root == LIBRARY_ROOT else root.relative_to(LIBRARY_ROOT).as_posix()
    dirs: list[dict[str, str]] = []
    files: list[dict[str, Any]] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: natural_key(p.name))
    except (OSError, PermissionError) as exc:
        raise HTTPException(403, f"Folder is not readable: {exc}")
    with db(readonly=True) as con:
        indexed = {r[0] for r in con.execute("SELECT rel_path FROM books WHERE missing_since IS NULL").fetchall()}
    for p in entries:
        rel = p.relative_to(LIBRARY_ROOT).as_posix()
        if p.is_dir():
            dirs.append({"name": p.name, "path": rel})
        elif p.is_file() and p.suffix.lower() in SUPPORTED_BOOK_SUFFIXES:
            files.append(
                {
                    "name": p.name,
                    "path": rel,
                    "indexed": rel in indexed,
                    "file_type": file_type_for_path(p),
                }
            )
    parent = ""
    if rel_root:
        parent_path = root.relative_to(LIBRARY_ROOT).parent
        parent = "" if str(parent_path) == "." else parent_path.as_posix()
    return {"path": rel_root, "parent": parent, "dirs": dirs, "files": files}


@router.post("/import", dependencies=[Depends(require_ajax)])
def import_paths(body: ImportBody):
    if state.scan_state.get("running"):
        raise HTTPException(409, "Wait for the current rescan to finish")
    added = updated = restored = kept = 0
    errors: list[str] = []
    with state.scan_lock:
        for rel in list(dict.fromkeys(body.paths)):
            try:
                result = register_file(rel, restore_ignored=True, category_override=body.category)
                if result == "added":
                    added += 1
                elif result == "updated":
                    updated += 1
                elif result == "restored":
                    restored += 1
                else:
                    kept += 1
            except HTTPException as exc:
                errors.append(f"{rel}: {exc.detail}")
    return {
        "ok": not errors,
        "added": added,
        "updated": updated,
        "restored": restored,
        "kept": kept,
        "errors": errors[:20],
    }
