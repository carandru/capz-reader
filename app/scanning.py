from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from . import state
from .config import LIBRARY_ROOT, MISSING_RETENTION_DAYS
from .db import chunked, db, ensure_category
from .epub import epub_spine_count
from .pdf import clear_book_cache, enqueue_thumb, pdf_page_count
from .utils import (
    SUPPORTED_BOOK_SUFFIXES,
    file_type_for_path,
    filename_title,
    guess_volume,
    inferred_series_for_rel,
    safe_path,
    source_name_for_rel,
    utc_now,
)


def _inspect_book(path, file_type: str) -> int:
    pages = pdf_page_count(path) if file_type == "pdf" else epub_spine_count(path)
    if pages <= 0:
        raise HTTPException(422, f"Unreadable {file_type.upper()} file")
    return pages


def register_file(rel: str, *, restore_ignored: bool = False, category_override: str | None = None) -> str:
    p = safe_path(rel)
    if not p.exists() or not p.is_file() or p.suffix.lower() not in SUPPORTED_BOOK_SUFFIXES:
        raise HTTPException(404, f"Supported book file not found: {rel}")
    rel = p.relative_to(LIBRARY_ROOT).as_posix()
    file_type = file_type_for_path(p)
    st = p.stat()

    # Fast no-op path: readonly lookup only, no write transaction and no thumbnail queue churn.
    with db(readonly=True) as con:
        ignored = con.execute("SELECT 1 FROM ignored_paths WHERE rel_path=?", (rel,)).fetchone() is not None
        old = con.execute("SELECT * FROM books WHERE rel_path=?", (rel,)).fetchone()
    if ignored and not restore_ignored:
        return "ignored"
    if old:
        unchanged = (
            old["size"] == st.st_size
            and abs(old["mtime"] - st.st_mtime) < 0.001
            and old["file_type"] == file_type
            and not old["missing_since"]
            and old["page_count"] > 0
            and category_override is None
            and not restore_ignored
        )
        if unchanged:
            return "kept"

    now = utc_now()
    old_id = int(old["id"]) if old else None
    changed = False
    restored = False
    pages = None
    if old:
        changed = old["size"] != st.st_size or abs(old["mtime"] - st.st_mtime) >= 0.001 or old["file_type"] != file_type
        restored = bool(old["missing_since"])
        if changed or restored or old["page_count"] <= 0:
            pages = _inspect_book(p, file_type)
        else:
            pages = int(old["page_count"])
    else:
        pages = _inspect_book(p, file_type)

    with state.write_lock, db() as con:
        if restore_ignored:
            con.execute("DELETE FROM ignored_paths WHERE rel_path=?", (rel,))
        elif con.execute("SELECT 1 FROM ignored_paths WHERE rel_path=?", (rel,)).fetchone():
            return "ignored"

        current = con.execute("SELECT * FROM books WHERE rel_path=?", (rel,)).fetchone()
        if current:
            title = current["title"] if current["custom_title"] else filename_title(p)
            category = current["category"]
            if category_override:
                category = ensure_category(con, category_override)
            con.execute(
                "UPDATE books SET title=?,category=?,file_type=?,page_count=?,size=?,mtime=?,missing_since=NULL,updated_at=? WHERE id=?",
                (title, category, file_type, pages, st.st_size, st.st_mtime, now, current["id"]),
            )
            old_id = int(current["id"])
        else:
            source = source_name_for_rel(rel)
            category = ensure_category(con, category_override or source)
            title = filename_title(p)
            series = inferred_series_for_rel(rel)
            volume = guess_volume(title)
            cur = con.execute(
                """
                INSERT INTO books(rel_path,title,category,series,volume,file_type,page_count,size,mtime,added_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (rel, title, category, series, volume, file_type, pages, st.st_size, st.st_mtime, now, now),
            )
            old_id = int(cur.lastrowid)

    if old:
        if changed:
            clear_book_cache(old_id)
        if file_type == "pdf" and (changed or restored):
            enqueue_thumb(old_id)
        return "restored" if restored else ("updated" if changed else "kept")

    if file_type == "pdf":
        enqueue_thumb(old_id)
    return "added"


def purge_missing(*, force: bool = False) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=MISSING_RETENTION_DAYS)
    with state.write_lock, db() as con:
        if force:
            rows = con.execute("SELECT id FROM books WHERE missing_since IS NOT NULL").fetchall()
        else:
            rows = con.execute(
                "SELECT id FROM books WHERE missing_since IS NOT NULL AND missing_since<=?",
                (cutoff.isoformat(),),
            ).fetchall()
        ids = [int(r[0]) for r in rows]
        for batch in chunked(ids):
            marks = ",".join("?" for _ in batch)
            con.execute(f"DELETE FROM books WHERE id IN ({marks})", batch)
    for book_id in ids:
        clear_book_cache(book_id)
    return len(ids)


def readable_source_dirs() -> tuple[list, list[str]]:
    if not LIBRARY_ROOT.exists() or not LIBRARY_ROOT.is_dir():
        raise RuntimeError(f"Library path not found: {LIBRARY_ROOT}")
    try:
        children = [p for p in LIBRARY_ROOT.iterdir() if p.is_dir()]
    except OSError as exc:
        raise RuntimeError(f"Library path is not readable: {exc}") from exc
    good: list = []
    skipped: list[str] = []
    for root in children:
        try:
            next(root.iterdir(), None)
            good.append(root)
        except (OSError, PermissionError):
            skipped.append(root.name)
    return good, skipped


def perform_scan():
    if not state.scan_lock.acquire(blocking=False):
        return
    state.reset_scan_state()
    try:
        roots, skipped = readable_source_dirs()
        state.scan_state["sources_skipped"] = skipped
        successful_sources: set[str] = set()
        seen: set[str] = set()
        seen_counts: Counter[str] = Counter()

        for root in roots:
            source_ok = True
            try:
                for p in root.rglob("*"):
                    if not p.is_file() or p.suffix.lower() not in SUPPORTED_BOOK_SUFFIXES:
                        continue
                    rel = p.relative_to(LIBRARY_ROOT).as_posix()
                    seen.add(rel)
                    seen_counts[root.name] += 1
                    state.scan_state["seen"] += 1
                    try:
                        result = register_file(rel)
                    except HTTPException as exc:
                        state.scan_state["unreadable"].append({"path": rel, "error": str(exc.detail)})
                        continue
                    if result == "added":
                        state.scan_state["added"] += 1
                    elif result == "updated":
                        state.scan_state["updated"] += 1
                    elif result == "restored":
                        state.scan_state["restored"] += 1
            except (OSError, PermissionError) as exc:
                source_ok = False
                state.scan_state["sources_skipped"].append(f"{root.name}: {exc}")
            if source_ok:
                successful_sources.add(root.name)
                with state.write_lock, db() as con:
                    ensure_category(con, root.name)

        with db(readonly=True) as con:
            existing = con.execute("SELECT id,rel_path,missing_since FROM books").fetchall()
        previous_counts: Counter[str] = Counter(
            source_name_for_rel(r["rel_path"]) for r in existing if not r["missing_since"]
        )

        # If a source that previously had many books is suddenly readable-but-empty,
        # treat it as a likely mount problem rather than marking the entire source missing.
        suspicious_sources = {
            src for src in successful_sources if previous_counts[src] >= 10 and seen_counts[src] == 0
        }
        state.scan_state["suspicious_sources"] = [
            {"source": src, "previous": previous_counts[src], "current": seen_counts[src]}
            for src in sorted(suspicious_sources)
        ]

        newly_missing: list[int] = []
        now = utc_now()
        with state.write_lock, db() as con:
            for row in existing:
                source = source_name_for_rel(row["rel_path"])
                if source not in successful_sources or source in suspicious_sources:
                    continue
                if row["rel_path"] not in seen and not row["missing_since"]:
                    con.execute(
                        "UPDATE books SET missing_since=?,updated_at=? WHERE id=?",
                        (now, now, row["id"]),
                    )
                    newly_missing.append(int(row["id"]))
        state.scan_state["missing"] = len(newly_missing)
        state.scan_state["purged"] = purge_missing(force=False)
        state.scan_state["finished_at"] = utc_now()
    except Exception as exc:
        state.scan_state["error"] = str(exc)
    finally:
        state.scan_state["running"] = False
        state.scan_lock.release()
