from __future__ import annotations

import queue
import shutil
import time
from pathlib import Path

import fitz
from fastapi import HTTPException

from .config import CACHE_DIR, RENDER_CACHE_DAYS, RENDER_CACHE_LIMIT_BYTES, THUMB_WIDTH
from .db import book_row
from .state import queued_lock, queued_thumbs, render_lock, thumb_queue
from .utils import safe_path


def pdf_page_count(path: Path) -> int:
    try:
        with fitz.open(path) as doc:
            return int(doc.page_count)
    except Exception:
        return 0


def clear_book_cache(book_id: int, thumbs_only: bool = False):
    for p in (CACHE_DIR / "thumbs").glob(f"{book_id}-*.jpg"):
        p.unlink(missing_ok=True)
    for p in (CACHE_DIR / "epub-covers").glob(f"{book_id}-*"):
        p.unlink(missing_ok=True)
    if not thumbs_only:
        shutil.rmtree(CACHE_DIR / "preview" / str(book_id), ignore_errors=True)
        shutil.rmtree(CACHE_DIR / "pages" / str(book_id), ignore_errors=True)



def render_page(book_id: int, page_num: int, width: int, kind: str):
    row = book_row(book_id)
    if row["file_type"] != "pdf":
        raise HTTPException(400, "Page rendering is only available for PDF books")
    src = safe_path(row["rel_path"])
    if not src.exists():
        raise HTTPException(404, "PDF file missing")
    page_num = max(1, min(page_num, max(row["page_count"], 1)))
    width = max(200, min(width, 2600))
    if kind == "thumb":
        outfile = CACHE_DIR / "thumbs" / f"{book_id}-{page_num}-{width}.jpg"
    elif kind == "preview":
        out_dir = CACHE_DIR / "preview" / str(book_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        outfile = out_dir / f"{page_num}-{width}.jpg"
    else:
        out_dir = CACHE_DIR / "pages" / str(book_id) / str(width)
        out_dir.mkdir(parents=True, exist_ok=True)
        outfile = out_dir / f"{page_num}.jpg"
    if outfile.exists() and outfile.stat().st_mtime >= src.stat().st_mtime:
        return outfile
    # Double-check after waiting for the single render slot: another request may have rendered it.
    with render_lock:
        if outfile.exists() and outfile.stat().st_mtime >= src.stat().st_mtime:
            return outfile
        try:
            with fitz.open(src) as doc:
                page = doc.load_page(page_num - 1)
                rect = page.rect
                zoom = width / max(rect.width, 1)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                pix.save(str(outfile))
        except Exception as exc:
            raise HTTPException(422, f"Unable to render PDF page: {exc}")
    return outfile


def enqueue_thumb(book_id: int):
    with queued_lock:
        if book_id in queued_thumbs:
            return
        queued_thumbs.add(book_id)
    try:
        thumb_queue.put_nowait(book_id)
    except queue.Full:
        with queued_lock:
            queued_thumbs.discard(book_id)


def thumb_worker():
    while True:
        book_id = thumb_queue.get()
        try:
            row = book_row(book_id)
            if not row["missing_since"] and row["file_type"] == "pdf":
                render_page(book_id, row["cover_page"], THUMB_WIDTH, "thumb")
        except Exception:
            pass
        finally:
            with queued_lock:
                queued_thumbs.discard(book_id)
            thumb_queue.task_done()


def cleanup_render_cache() -> dict[str, int]:
    """Clean only generated preview/page JPEGs. Never touches source books or thumbnails."""
    roots = [CACHE_DIR / "preview", CACHE_DIR / "pages"]
    now = time.time()
    cutoff = now - RENDER_CACHE_DAYS * 86400
    entries: list[tuple[float, int, Path]] = []
    removed = 0
    removed_bytes = 0
    total = 0
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.jpg"):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                try:
                    size = st.st_size
                    p.unlink()
                    removed += 1
                    removed_bytes += size
                except OSError:
                    pass
                continue
            total += st.st_size
            entries.append((st.st_mtime, st.st_size, p))
    if total > RENDER_CACHE_LIMIT_BYTES:
        for _, size, p in sorted(entries, key=lambda x: x[0]):
            if total <= RENDER_CACHE_LIMIT_BYTES:
                break
            try:
                p.unlink()
                total -= size
                removed += 1
                removed_bytes += size
            except OSError:
                pass
    # Best-effort empty-directory cleanup.
    for root in roots:
        if not root.exists():
            continue
        for d in sorted((x for x in root.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
    return {"removed": removed, "removed_bytes": removed_bytes, "remaining_bytes": max(0, total)}
