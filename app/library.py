from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from . import state
from .auth import require_ajax, require_user
from .config import ALLOW_DELETE_FILES, MISSING_RETENTION_DAYS
from .db import (
    book_row,
    book_to_dict,
    calculate_read_state,
    chunked,
    cleanup_orphan_tags,
    db,
    ensure_category,
    get_or_create_tag_id,
    normalize_name,
    resolve_category,
    tags_for_books,
)
from .pdf import clear_book_cache, enqueue_thumb
from .schemas import BookEdit, BulkBody, CategoryCreate, CategoryDelete, CategoryEdit, ProgressBody
from .series import series_summary, sort_books
from .utils import filename_title, safe_path, utc_now

router = APIRouter(prefix="/api", tags=["library"], dependencies=[Depends(require_user)])


def categories_payload(con: sqlite3.Connection) -> list[dict[str, Any]]:
    counts = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT category,COUNT(*) FROM books WHERE missing_since IS NULL GROUP BY category"
        ).fetchall()
    }
    rows = con.execute("SELECT * FROM categories ORDER BY sort_order,name COLLATE NOCASE").fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "sort_order": r["sort_order"],
            "visible": bool(r["visible"]),
            "count": counts.get(r["name"], 0),
        }
        for r in rows
    ]


def get_book_payload(book_id: int) -> dict[str, Any]:
    row = book_row(book_id)
    with db(readonly=True) as con:
        tags = tags_for_books(con, [book_id]).get(book_id, [])
    return book_to_dict(row, tags)


@router.get("/categories")
def list_categories():
    with db(readonly=True) as con:
        return categories_payload(con)


@router.post("/categories", dependencies=[Depends(require_ajax)])
def create_category(body: CategoryCreate):
    name = normalize_name(body.name, max_len=120)
    with state.write_lock, db() as con:
        if resolve_category(con, name):
            raise HTTPException(409, "Category already exists")
        ensure_category(con, name)
        return categories_payload(con)


@router.patch("/categories/{category_id}", dependencies=[Depends(require_ajax)])
def edit_category(category_id: int, body: CategoryEdit):
    data = body.model_dump(exclude_unset=True)
    with state.write_lock, db() as con:
        row = con.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Category not found")
        new_name = row["name"]
        if "name" in data:
            new_name = normalize_name(data["name"], max_len=120)
            dupe = resolve_category(con, new_name)
            if dupe and int(dupe["id"]) != category_id:
                raise HTTPException(409, "Category already exists")
            con.execute(
                "UPDATE books SET category=?,updated_at=? WHERE category=?",
                (new_name, utc_now(), row["name"]),
            )
        con.execute(
            "UPDATE categories SET name=?,visible=?,sort_order=? WHERE id=?",
            (
                new_name,
                int(data.get("visible", bool(row["visible"]))),
                int(data.get("sort_order", row["sort_order"])),
                category_id,
            ),
        )
        return categories_payload(con)


@router.post("/categories/{category_id}/delete", dependencies=[Depends(require_ajax)])
def delete_category(category_id: int, body: CategoryDelete):
    with state.write_lock, db() as con:
        row = con.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Category not found")
        count = con.execute("SELECT COUNT(*) FROM books WHERE category=?", (row["name"],)).fetchone()[0]
        if count:
            if not body.move_to:
                raise HTTPException(409, f"Category contains {count} books; choose a destination first")
            target = resolve_category(con, body.move_to)
            if not target or target["name"].casefold() == row["name"].casefold():
                raise HTTPException(400, "Invalid destination category")
            con.execute(
                "UPDATE books SET category=?,updated_at=? WHERE category=?",
                (target["name"], utc_now(), row["name"]),
            )
        con.execute("DELETE FROM categories WHERE id=?", (category_id,))
        return categories_payload(con)


@router.get("/library")
def library(
    q: str = "",
    category: str = "",
    series: str = "",
    sort: str = "title",
    show_archived: bool = False,
    favorites: bool = False,
):
    where = ["b.missing_since IS NULL"]
    params: list[Any] = []
    if q:
        where.append(
            "(b.title LIKE ? OR b.series LIKE ? OR EXISTS ("
            "SELECT 1 FROM book_tags bt JOIN tags t ON t.id=bt.tag_id "
            "WHERE bt.book_id=b.id AND t.name LIKE ?))"
        )
        like = f"%{q}%"
        params.extend([like, like, like])
    if category:
        where.append("b.category=?")
        params.append(category)
    if series:
        where.append("COALESCE(b.series,'')=?")
        params.append(series)
    if not show_archived:
        where.append("b.archived=0")
    if favorites:
        where.append("b.favorite=1")
    with db(readonly=True) as con:
        rows = con.execute(f"SELECT b.* FROM books b WHERE {' AND '.join(where)}", params).fetchall()
        tag_map = tags_for_books(con, [r["id"] for r in rows])
        series_rows = series_summary(con)
        categories = categories_payload(con)
        missing_count = con.execute("SELECT COUNT(*) FROM books WHERE missing_since IS NOT NULL").fetchone()[0]
    full_books = [book_to_dict(r, tag_map.get(r["id"], [])) for r in rows]
    sort_books(full_books, sort)
    # Compact the network payload after sorting, while keeping enough metadata for cards/Series/Continue Reading.
    keep = {
        "id", "title", "category", "series", "volume", "file_type", "page_count",
        "favorite", "archived", "read_state", "last_page", "last_opened", "progress",
        "tags", "added_at", "mtime",
    }
    books = [{k: b.get(k) for k in keep} for b in full_books]
    return {
        "books": books,
        "series": series_rows,
        "categories": categories,
        "total_count": sum(c["count"] for c in categories),
        "count": len(books),
        "missing_count": missing_count,
        "missing_retention_days": MISSING_RETENTION_DAYS,
        "scan": state.scan_state,
        "allow_delete_files": ALLOW_DELETE_FILES,
    }


@router.get("/books/{book_id}")
def get_book(book_id: int):
    return get_book_payload(book_id)


@router.patch("/books/{book_id}", dependencies=[Depends(require_ajax)])
def edit_book(book_id: int, body: BookEdit):
    row = book_row(book_id)
    updates: list[str] = []
    params: list[Any] = []
    data = body.model_dump(exclude_unset=True)
    tags = data.pop("tags", None)
    if "category" in data:
        with db(readonly=True) as con:
            cat = resolve_category(con, data["category"])
        if not cat:
            raise HTTPException(400, "Invalid category")
        data["category"] = cat["name"]
    if "read_state" in data and data["read_state"] not in {"unread", "reading", "read"}:
        raise HTTPException(400, "Invalid read state")
    for key, val in data.items():
        if key == "title":
            val = normalize_name(val, max_len=500)
            updates.append("custom_title=1")
        if key in {"favorite", "archived"}:
            val = int(bool(val))
        if key == "series" and val is not None:
            val = val.strip() or None
        updates.append(f"{key}=?")
        params.append(val)
    if updates or tags is not None:
        with state.write_lock, db() as con:
            if updates:
                updates.append("updated_at=?")
                params.extend([utc_now(), book_id])
                con.execute(f"UPDATE books SET {', '.join(updates)} WHERE id=?", params)
            if tags is not None:
                con.execute("DELETE FROM book_tags WHERE book_id=?", (book_id,))
                normalized = sorted({normalize_name(t, max_len=120) for t in tags if (t or "").strip()}, key=str.casefold)
                for tag in normalized:
                    tag_id = get_or_create_tag_id(con, tag)
                    con.execute(
                        "INSERT OR IGNORE INTO book_tags(book_id,tag_id) VALUES(?,?)",
                        (book_id, tag_id),
                    )
                cleanup_orphan_tags(con)
    if body.cover_page is not None and body.cover_page != row["cover_page"]:
        clear_book_cache(book_id, thumbs_only=True)
        enqueue_thumb(book_id)
    return get_book_payload(book_id)


@router.post("/books/{book_id}/reset-title", dependencies=[Depends(require_ajax)])
def reset_title(book_id: int):
    row = book_row(book_id)
    p = safe_path(row["rel_path"])
    title = filename_title(p)
    with state.write_lock, db() as con:
        con.execute(
            "UPDATE books SET title=?,custom_title=0,updated_at=? WHERE id=?",
            (title, utc_now(), book_id),
        )
    return get_book_payload(book_id)


def _strict_bool(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1):
        return int(value)
    raise HTTPException(400, "Expected a boolean value")


@router.post("/bulk", dependencies=[Depends(require_ajax)])
def bulk(body: BulkBody):
    ids = list(dict.fromkeys(body.ids))
    action = body.action
    value = body.value
    now = utc_now()
    with state.write_lock, db() as con:
        rows = []
        for batch in chunked(ids):
            marks = ",".join("?" for _ in batch)
            rows.extend(con.execute(f"SELECT id,rel_path FROM books WHERE id IN ({marks})", batch).fetchall())
        if len(rows) != len(ids):
            raise HTTPException(404, "Some books no longer exist")

        if action == "category":
            cat = resolve_category(con, str(value) if value is not None else None)
            if not cat:
                raise HTTPException(400, "Invalid category")
            for batch in chunked(ids):
                marks = ",".join("?" for _ in batch)
                con.execute(
                    f"UPDATE books SET category=?,updated_at=? WHERE id IN ({marks})",
                    [cat["name"], now, *batch],
                )
        elif action == "series":
            val = str(value).strip() if value is not None else ""
            for batch in chunked(ids):
                marks = ",".join("?" for _ in batch)
                con.execute(
                    f"UPDATE books SET series=?,updated_at=? WHERE id IN ({marks})",
                    [val or None, now, *batch],
                )
        elif action == "favorite":
            val = _strict_bool(value)
            for batch in chunked(ids):
                marks = ",".join("?" for _ in batch)
                con.execute(
                    f"UPDATE books SET favorite=?,updated_at=? WHERE id IN ({marks})",
                    [val, now, *batch],
                )
        elif action == "archive":
            val = _strict_bool(value)
            for batch in chunked(ids):
                marks = ",".join("?" for _ in batch)
                con.execute(
                    f"UPDATE books SET archived=?,updated_at=? WHERE id IN ({marks})",
                    [val, now, *batch],
                )
        elif action == "read_state":
            if value not in {"unread", "reading", "read"}:
                raise HTTPException(400, "Invalid read state")
            for batch in chunked(ids):
                marks = ",".join("?" for _ in batch)
                con.execute(
                    f"UPDATE books SET read_state=?,updated_at=? WHERE id IN ({marks})",
                    [value, now, *batch],
                )
        elif action == "add_tag":
            tag_id = get_or_create_tag_id(con, str(value or ""))
            con.executemany(
                "INSERT OR IGNORE INTO book_tags(book_id,tag_id) VALUES(?,?)",
                [(i, tag_id) for i in ids],
            )
        elif action == "remove_tag":
            tag = normalize_name(str(value or ""), max_len=120)
            tag_row = con.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE", (tag,)).fetchone()
            if tag_row:
                con.executemany(
                    "DELETE FROM book_tags WHERE book_id=? AND tag_id=?",
                    [(i, tag_row[0]) for i in ids],
                )
                cleanup_orphan_tags(con)
        elif action == "remove_library":
            con.executemany(
                "INSERT OR REPLACE INTO ignored_paths(rel_path,ignored_at) VALUES(?,?)",
                [(r["rel_path"], now) for r in rows],
            )
            for batch in chunked(ids):
                marks = ",".join("?" for _ in batch)
                con.execute(f"DELETE FROM books WHERE id IN ({marks})", batch)
        elif action == "delete_files":
            if not ALLOW_DELETE_FILES:
                raise HTTPException(403, "Physical deletion is disabled")
            failures = []
            for r in rows:
                try:
                    p = safe_path(r["rel_path"])
                    if p.exists():
                        p.unlink()
                except Exception as exc:
                    failures.append(f"{r['rel_path']}: {exc}")
            if failures:
                raise HTTPException(500, {"message": "Some files could not be deleted", "errors": failures[:10]})
            for batch in chunked(ids):
                marks = ",".join("?" for _ in batch)
                con.execute(f"DELETE FROM books WHERE id IN ({marks})", batch)
        elif action == "regen_preview":
            pass
        else:
            raise HTTPException(400, "Unknown bulk action")

    if action in {"regen_preview", "remove_library", "delete_files"}:
        for i in ids:
            clear_book_cache(i)
            if action == "regen_preview":
                enqueue_thumb(i)
    return {"ok": True, "count": len(ids)}


@router.post("/books/{book_id}/progress", dependencies=[Depends(require_ajax)])
def save_progress(book_id: int, body: ProgressBody):
    row = book_row(book_id)
    now = utc_now()
    if row["file_type"] == "epub":
        chapter = min(body.page or row["last_page"] or 1, max(row["page_count"], 1))
        progress = float(body.epub_progress if body.epub_progress is not None else row["epub_progress"] or 0)
        progress = max(0.0, min(progress, 100.0))
        state_name = calculate_read_state(
            row["read_state"], completed=progress >= 99.5, started=progress > 0.1 or chapter > 1
        )
        with state.write_lock, db() as con:
            con.execute(
                "UPDATE books SET last_page=?,epub_location=?,epub_progress=?,read_state=?,minutes_spent=minutes_spent+?,last_opened=?,updated_at=? WHERE id=?",
                (chapter, body.epub_location, progress, state_name, body.seconds / 60.0, now, now, book_id),
            )
        return {"ok": True, "page": chapter, "progress": progress, "read_state": state_name}

    if body.page is None:
        raise HTTPException(400, "PDF progress requires a page")
    page = min(body.page, max(row["page_count"], 1))
    state_name = calculate_read_state(
        row["read_state"], completed=bool(row["page_count"] and page >= row["page_count"]), started=page > 1
    )
    with state.write_lock, db() as con:
        con.execute(
            "UPDATE books SET last_page=?,read_state=?,minutes_spent=minutes_spent+?,last_opened=?,updated_at=? WHERE id=?",
            (page, state_name, body.seconds / 60.0, now, now, book_id),
        )
    return {"ok": True, "page": page, "read_state": state_name}


@router.get("/tags")
def list_tags():
    with db(readonly=True) as con:
        rows = con.execute(
            "SELECT t.name,COUNT(bt.book_id) n FROM tags t JOIN book_tags bt ON bt.tag_id=t.id GROUP BY t.id ORDER BY t.name COLLATE NOCASE"
        ).fetchall()
    return [{"name": r["name"], "count": r["n"]} for r in rows]
