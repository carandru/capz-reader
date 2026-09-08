from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_NAME = os.getenv("APP_NAME", "NAS PDF Reader")
APP_VERSION = "0.1.0"
LIBRARY_ROOT = Path(os.getenv("LIBRARY_ROOT", "/library")).resolve()
DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
CACHE_DIR = Path(os.getenv("CACHE_DIR", "/cache")).resolve()
DB_PATH = DATA_DIR / "reader.sqlite3"
ALLOW_DELETE_FILES = os.getenv("ALLOW_DELETE_FILES", "false").lower() in {"1", "true", "yes"}
AUTO_SCAN_ON_START = os.getenv("AUTO_SCAN_ON_START", "true").lower() in {"1", "true", "yes"}
THUMB_WIDTH = int(os.getenv("THUMB_WIDTH", "320"))
PREVIEW_WIDTH = int(os.getenv("PREVIEW_WIDTH", "760"))
READER_WIDTH = int(os.getenv("READER_WIDTH", "1800"))
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "90"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() in {"1", "true", "yes"}

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
(CACHE_DIR / "thumbs").mkdir(parents=True, exist_ok=True)
(CACHE_DIR / "preview").mkdir(parents=True, exist_ok=True)
(CACHE_DIR / "pages").mkdir(parents=True, exist_ok=True)

ph = PasswordHasher()
write_lock = threading.RLock()
render_lock = threading.Semaphore(1)
scan_lock = threading.Lock()
scan_state: dict[str, Any] = {"running": False, "seen": 0, "added": 0, "updated": 0, "removed": 0, "error": None, "finished_at": None}
thumb_queue: queue.Queue[int] = queue.Queue(maxsize=5000)
queued_thumbs: set[int] = set()
queued_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def db(readonly: bool = False):
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    if not readonly:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=ON")
    try:
        yield con
        if not readonly:
            con.commit()
    except Exception:
        if not readonly:
            con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with write_lock, db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              username TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              expires_at REAL NOT NULL,
              FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS books (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              rel_path TEXT NOT NULL UNIQUE,
              title TEXT NOT NULL,
              custom_title INTEGER NOT NULL DEFAULT 0,
              category TEXT NOT NULL DEFAULT 'Dou' CHECK(category IN ('Novel','Manga','Dou')),
              series TEXT,
              volume TEXT,
              cover_page INTEGER NOT NULL DEFAULT 1,
              page_count INTEGER NOT NULL DEFAULT 0,
              size INTEGER NOT NULL DEFAULT 0,
              mtime REAL NOT NULL DEFAULT 0,
              favorite INTEGER NOT NULL DEFAULT 0,
              archived INTEGER NOT NULL DEFAULT 0,
              read_state TEXT NOT NULL DEFAULT 'unread' CHECK(read_state IN ('unread','reading','read')),
              last_page INTEGER NOT NULL DEFAULT 1,
              minutes_spent REAL NOT NULL DEFAULT 0,
              last_opened TEXT,
              added_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_books_category ON books(category);
            CREATE INDEX IF NOT EXISTS idx_books_series ON books(series);
            CREATE INDEX IF NOT EXISTS idx_books_title ON books(title);
            CREATE TABLE IF NOT EXISTS tags (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE COLLATE NOCASE
            );
            CREATE TABLE IF NOT EXISTS book_tags (
              book_id INTEGER NOT NULL,
              tag_id INTEGER NOT NULL,
              PRIMARY KEY(book_id, tag_id),
              FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE,
              FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS ignored_paths (
              rel_path TEXT PRIMARY KEY,
              ignored_at TEXT NOT NULL
            );
            """
        )


def safe_path(rel_path: str) -> Path:
    p = (LIBRARY_ROOT / rel_path).resolve()
    if LIBRARY_ROOT != p and LIBRARY_ROOT not in p.parents:
        raise HTTPException(400, "Invalid path")
    return p


def filename_title(path: Path) -> str:
    return path.stem.strip()


def natural_key(value: str | None):
    value = value or ""
    return [int(s) if s.isdigit() else s.casefold() for s in re.split(r"(\d+)", value)]


def guess_volume(title: str) -> str | None:
    patterns = [
        r"(?:vol(?:ume)?|เล่ม|part|ch(?:apter)?)\s*[._ -]*(\d+(?:\.\d+)?)\s*$",
        r"(?:\s|[-_])([0-9]{1,4})\s*$",
    ]
    for pat in patterns:
        m = re.search(pat, title, flags=re.I)
        if m:
            return m.group(1)
    return None


def pdf_page_count(path: Path) -> int:
    try:
        with fitz.open(path) as doc:
            return doc.page_count
    except Exception:
        return 0


def book_row(book_id: int) -> sqlite3.Row:
    with db(readonly=True) as con:
        row = con.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Book not found")
    return row


def tags_for_books(con: sqlite3.Connection, ids: list[int]) -> dict[int, list[str]]:
    out = {i: [] for i in ids}
    if not ids:
        return out
    marks = ",".join("?" for _ in ids)
    rows = con.execute(
        f"SELECT bt.book_id, t.name FROM book_tags bt JOIN tags t ON t.id=bt.tag_id WHERE bt.book_id IN ({marks}) ORDER BY t.name COLLATE NOCASE",
        ids,
    ).fetchall()
    for r in rows:
        out[r["book_id"]].append(r["name"])
    return out


def book_to_dict(row: sqlite3.Row, tags: list[str] | None = None) -> dict[str, Any]:
    d = dict(row)
    d["favorite"] = bool(d["favorite"])
    d["archived"] = bool(d["archived"])
    d["tags"] = tags or []
    d["progress"] = round((d["last_page"] / d["page_count"] * 100), 1) if d["page_count"] else 0
    return d


def render_page(book_id: int, page_num: int, width: int, kind: str) -> Path:
    row = book_row(book_id)
    src = safe_path(row["rel_path"])
    if not src.exists():
        raise HTTPException(404, "PDF file missing")
    page_num = max(1, min(page_num, max(row["page_count"], 1)))
    width = max(200, min(width, 2600))
    if kind == "thumb":
        out_dir = CACHE_DIR / "thumbs"
        outfile = out_dir / f"{book_id}-{page_num}-{width}.jpg"
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
            render_page(book_id, row["cover_page"], THUMB_WIDTH, "thumb")
        except Exception:
            pass
        finally:
            with queued_lock:
                queued_thumbs.discard(book_id)
            thumb_queue.task_done()


def perform_scan():
    global scan_state
    if not scan_lock.acquire(blocking=False):
        return
    scan_state = {"running": True, "seen": 0, "added": 0, "updated": 0, "removed": 0, "error": None, "finished_at": None}
    try:
        if not LIBRARY_ROOT.exists():
            raise RuntimeError(f"Library path not found: {LIBRARY_ROOT}")
        with db(readonly=True) as con:
            ignored = {r[0] for r in con.execute("SELECT rel_path FROM ignored_paths").fetchall()}
            existing = {r["rel_path"]: dict(r) for r in con.execute("SELECT * FROM books").fetchall()}
        seen: set[str] = set()
        for p in LIBRARY_ROOT.rglob("*"):
            if not p.is_file() or p.suffix.lower() != ".pdf":
                continue
            rel = p.relative_to(LIBRARY_ROOT).as_posix()
            if rel in ignored:
                continue
            seen.add(rel)
            scan_state["seen"] += 1
            st = p.stat()
            old = existing.get(rel)
            filename_name = filename_title(p)  # embedded PDF metadata is intentionally ignored
            title = old["title"] if old and old.get("custom_title") else filename_name
            if old and old["size"] == st.st_size and abs(old["mtime"] - st.st_mtime) < 0.001 and title == old["title"]:
                continue
            pages = pdf_page_count(p)
            parts = Path(rel).parts
            inferred_category = parts[0] if parts and parts[0] in {"Novel", "Manga", "Dou"} else (old["category"] if old else "Dou")
            # Category comes from the mounted root folder. The first directory
            # below the category is the series name; deeper folders can be arcs/parts
            # without changing the logical series. Files directly under a category
            # are standalone books.
            inferred_series = None
            if len(parts) >= 3 and parts[1] not in {"Novel", "Manga", "Dou"}:
                inferred_series = parts[1]
            volume = old["volume"] if old else guess_volume(filename_name)
            now = utc_now()
            with write_lock, db() as con:
                if old:
                    con.execute(
                        "UPDATE books SET title=?, page_count=?, size=?, mtime=?, updated_at=? WHERE rel_path=?",
                        (title, pages, st.st_size, st.st_mtime, now, rel),
                    )
                    book_id = old["id"]
                    scan_state["updated"] += 1
                else:
                    cur = con.execute(
                        "INSERT INTO books(rel_path,title,category,series,volume,page_count,size,mtime,added_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (rel, filename_name, inferred_category, inferred_series, volume, pages, st.st_size, st.st_mtime, now, now),
                    )
                    book_id = cur.lastrowid
                    scan_state["added"] += 1
            enqueue_thumb(int(book_id))
        missing = [rel for rel in existing if rel not in seen]
        if missing:
            with write_lock, db() as con:
                for rel in missing:
                    con.execute("DELETE FROM books WHERE rel_path=?", (rel,))
                    scan_state["removed"] += 1
        scan_state["finished_at"] = utc_now()
    except Exception as exc:
        scan_state["error"] = str(exc)
    finally:
        scan_state["running"] = False
        scan_lock.release()


def cleanup_sessions():
    with write_lock, db() as con:
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))


def auth_state(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get("nas_reader_session")
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db(readonly=True) as con:
        row = con.execute(
            "SELECT u.id,u.username,s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
            (token_hash,),
        ).fetchone()
    if not row or row["expires_at"] < time.time():
        return None
    return dict(row)


def require_user(request: Request):
    user = auth_state(request)
    if not user:
        raise HTTPException(401, "Login required")
    return user


def require_ajax(x_requested_with: str | None = Header(default=None)):
    if x_requested_with != "NASPDFReader":
        raise HTTPException(403, "Missing request guard")


class SetupBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=10, max_length=256)


class LoginBody(BaseModel):
    username: str
    password: str


class BookEdit(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    category: str | None = None
    series: str | None = Field(default=None, max_length=300)
    volume: str | None = Field(default=None, max_length=80)
    cover_page: int | None = Field(default=None, ge=1)
    favorite: bool | None = None
    archived: bool | None = None
    read_state: str | None = None
    tags: list[str] | None = None


class BulkBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)
    action: str
    value: Any = None


class ProgressBody(BaseModel):
    page: int = Field(ge=1)
    seconds: float = Field(default=0, ge=0, le=3600)


app = FastAPI(title=APP_NAME)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def startup():
    init_db()
    cleanup_sessions()
    t = threading.Thread(target=thumb_worker, name="thumb-worker", daemon=True)
    t.start()
    if AUTO_SCAN_ON_START:
        threading.Thread(target=perform_scan, name="startup-scan", daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text("utf-8")


@app.get("/reader/{book_id}", response_class=HTMLResponse)
def reader(book_id: int):
    return (STATIC_DIR / "reader.html").read_text("utf-8")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def sw():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/api/health")
def health():
    sources = {}
    for category in ("Novel", "Manga", "Dou"):
        root = LIBRARY_ROOT / category
        sources[category] = {"path": str(root), "mounted": root.exists()}
    return {"ok": True, "app": APP_NAME, "version": APP_VERSION, "library": str(LIBRARY_ROOT), "db": str(DB_PATH), "sources": sources}


@app.get("/api/auth/status")
def auth_status(request: Request):
    with db(readonly=True) as con:
        setup_required = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    user = auth_state(request)
    return {"setup_required": setup_required, "authenticated": bool(user), "username": user["username"] if user else None}


@app.post("/api/auth/setup", dependencies=[Depends(require_ajax)])
def setup(body: SetupBody, response: Response):
    with write_lock, db() as con:
        if con.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise HTTPException(409, "Setup already completed")
        con.execute("INSERT INTO users(id,username,password_hash,created_at) VALUES(1,?,?,?)", (body.username, ph.hash(body.password), utc_now()))
    return login(LoginBody(username=body.username, password=body.password), response)


@app.post("/api/auth/login", dependencies=[Depends(require_ajax)])
def login(body: LoginBody, response: Response):
    with db(readonly=True) as con:
        row = con.execute("SELECT * FROM users WHERE username=?", (body.username,)).fetchone()
    if not row:
        raise HTTPException(401, "Invalid username or password")
    try:
        ph.verify(row["password_hash"], body.password)
    except VerifyMismatchError:
        raise HTTPException(401, "Invalid username or password")
    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = time.time() + SESSION_DAYS * 86400
    with write_lock, db() as con:
        con.execute("INSERT OR REPLACE INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (token_hash, row["id"], expires))
    response.set_cookie("nas_reader_session", token, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/")
    return {"ok": True, "username": row["username"]}


@app.post("/api/auth/logout", dependencies=[Depends(require_ajax)])
def logout(request: Request, response: Response):
    token = request.cookies.get("nas_reader_session")
    if token:
        with write_lock, db() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie("nas_reader_session", path="/")
    return {"ok": True}


@app.get("/api/library")
def library(
    request: Request,
    q: str = "",
    category: str = "",
    series: str = "",
    sort: str = "title",
    show_archived: bool = False,
    favorites: bool = False,
):
    require_user(request)
    where = ["1=1"]
    params: list[Any] = []
    if q:
        where.append("(title LIKE ? OR rel_path LIKE ? OR series LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if category:
        where.append("category=?")
        params.append(category)
    if series:
        where.append("COALESCE(series,'')=?")
        params.append(series)
    if not show_archived:
        where.append("archived=0")
    if favorites:
        where.append("favorite=1")
    with db(readonly=True) as con:
        rows = con.execute(f"SELECT * FROM books WHERE {' AND '.join(where)}", params).fetchall()
        tag_map = tags_for_books(con, [r["id"] for r in rows])
        series_rows = con.execute("SELECT category,COALESCE(series,'') AS series,COUNT(*) n FROM books GROUP BY category,series ORDER BY series COLLATE NOCASE").fetchall()
        category_rows = con.execute("SELECT category,COUNT(*) n FROM books WHERE archived=0 GROUP BY category").fetchall()
    books = [book_to_dict(r, tag_map.get(r["id"], [])) for r in rows]
    if sort == "added":
        books.sort(key=lambda x: x["added_at"], reverse=True)
    elif sort == "modified":
        books.sort(key=lambda x: x["mtime"], reverse=True)
    elif sort == "progress":
        books.sort(key=lambda x: x["progress"], reverse=True)
    elif sort == "series":
        books.sort(key=lambda x: (natural_key(x["series"]), natural_key(x["volume"]), natural_key(x["title"])))
    else:
        books.sort(key=lambda x: natural_key(x["title"]))
    return {
        "books": books,
        "series": [{"category": r["category"], "series": r["series"], "count": r["n"]} for r in series_rows if r["series"]],
        "category_counts": {r["category"]: r["n"] for r in category_rows},
        "total_count": sum(r["n"] for r in category_rows),
        "count": len(books),
        "scan": scan_state,
        "allow_delete_files": ALLOW_DELETE_FILES,
    }


@app.get("/api/books/{book_id}")
def get_book(book_id: int, request: Request):
    require_user(request)
    row = book_row(book_id)
    with db(readonly=True) as con:
        tags = tags_for_books(con, [book_id]).get(book_id, [])
    return book_to_dict(row, tags)


@app.patch("/api/books/{book_id}", dependencies=[Depends(require_ajax)])
def edit_book(book_id: int, body: BookEdit, request: Request):
    require_user(request)
    row = book_row(book_id)
    updates = []
    params: list[Any] = []
    data = body.model_dump(exclude_unset=True)
    tags = data.pop("tags", None)
    if "category" in data and data["category"] not in {"Novel", "Manga", "Dou"}:
        raise HTTPException(400, "Invalid category")
    if "read_state" in data and data["read_state"] not in {"unread", "reading", "read"}:
        raise HTTPException(400, "Invalid read state")
    for key, val in data.items():
        if key == "title":
            updates.append("custom_title=1")
        if key in {"favorite", "archived"}:
            val = int(bool(val))
        if key == "series" and val is not None:
            val = val.strip() or None
        updates.append(f"{key}=?")
        params.append(val)
    if updates or tags is not None:
        with write_lock, db() as con:
            if updates:
                updates.append("updated_at=?")
                params.extend([utc_now(), book_id])
                con.execute(f"UPDATE books SET {', '.join(updates)} WHERE id=?", params)
            if tags is not None:
                con.execute("DELETE FROM book_tags WHERE book_id=?", (book_id,))
                for tag in {t.strip() for t in tags if t.strip()}:
                    con.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (tag,))
                    tag_id = con.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE", (tag,)).fetchone()[0]
                    con.execute("INSERT OR IGNORE INTO book_tags(book_id,tag_id) VALUES(?,?)", (book_id, tag_id))
    if body.cover_page is not None and body.cover_page != row["cover_page"]:
        clear_book_cache(book_id, thumbs_only=True)
        enqueue_thumb(book_id)
    return get_book(book_id, request)


@app.post("/api/bulk", dependencies=[Depends(require_ajax)])
def bulk(body: BulkBody, request: Request):
    require_user(request)
    ids = list(dict.fromkeys(body.ids))
    marks = ",".join("?" for _ in ids)
    action = body.action
    value = body.value
    with write_lock, db() as con:
        rows = con.execute(f"SELECT id,rel_path FROM books WHERE id IN ({marks})", ids).fetchall()
        if len(rows) != len(ids):
            raise HTTPException(404, "Some books no longer exist")
        if action == "category":
            if value not in {"Novel", "Manga", "Dou"}:
                raise HTTPException(400, "Invalid category")
            con.execute(f"UPDATE books SET category=?,updated_at=? WHERE id IN ({marks})", [value, utc_now(), *ids])
        elif action == "series":
            val = str(value).strip() if value is not None else ""
            con.execute(f"UPDATE books SET series=?,updated_at=? WHERE id IN ({marks})", [val or None, utc_now(), *ids])
        elif action == "favorite":
            con.execute(f"UPDATE books SET favorite=?,updated_at=? WHERE id IN ({marks})", [int(bool(value)), utc_now(), *ids])
        elif action == "archive":
            con.execute(f"UPDATE books SET archived=?,updated_at=? WHERE id IN ({marks})", [int(bool(value)), utc_now(), *ids])
        elif action == "read_state":
            if value not in {"unread", "reading", "read"}:
                raise HTTPException(400, "Invalid read state")
            con.execute(f"UPDATE books SET read_state=?,updated_at=? WHERE id IN ({marks})", [value, utc_now(), *ids])
        elif action == "add_tag":
            tag = str(value or "").strip()
            if not tag:
                raise HTTPException(400, "Tag required")
            con.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (tag,))
            tag_id = con.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE", (tag,)).fetchone()[0]
            con.executemany("INSERT OR IGNORE INTO book_tags(book_id,tag_id) VALUES(?,?)", [(i, tag_id) for i in ids])
        elif action == "remove_tag":
            tag = str(value or "").strip()
            tag_row = con.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE", (tag,)).fetchone()
            if tag_row:
                con.executemany("DELETE FROM book_tags WHERE book_id=? AND tag_id=?", [(i, tag_row[0]) for i in ids])
        elif action == "remove_library":
            con.executemany("INSERT OR REPLACE INTO ignored_paths(rel_path,ignored_at) VALUES(?,?)", [(r["rel_path"], utc_now()) for r in rows])
            con.execute(f"DELETE FROM books WHERE id IN ({marks})", ids)
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
            con.execute(f"DELETE FROM books WHERE id IN ({marks})", ids)
        elif action == "regen_preview":
            pass
        else:
            raise HTTPException(400, "Unknown bulk action")
    if action == "regen_preview":
        for i in ids:
            clear_book_cache(i)
            enqueue_thumb(i)
    return {"ok": True, "count": len(ids)}


@app.post("/api/books/{book_id}/progress", dependencies=[Depends(require_ajax)])
def save_progress(book_id: int, body: ProgressBody, request: Request):
    require_user(request)
    row = book_row(book_id)
    page = min(body.page, max(row["page_count"], 1))
    state = "read" if row["page_count"] and page >= row["page_count"] else ("reading" if page > 1 else row["read_state"])
    with write_lock, db() as con:
        con.execute(
            "UPDATE books SET last_page=?,read_state=?,minutes_spent=minutes_spent+?,last_opened=?,updated_at=? WHERE id=?",
            (page, state, body.seconds / 60.0, utc_now(), utc_now(), book_id),
        )
    return {"ok": True, "page": page, "read_state": state}


def clear_book_cache(book_id: int, thumbs_only: bool = False):
    for p in (CACHE_DIR / "thumbs").glob(f"{book_id}-*.jpg"):
        p.unlink(missing_ok=True)
    if not thumbs_only:
        shutil.rmtree(CACHE_DIR / "preview" / str(book_id), ignore_errors=True)
        shutil.rmtree(CACHE_DIR / "pages" / str(book_id), ignore_errors=True)


@app.get("/api/books/{book_id}/thumb")
def thumb(book_id: int, request: Request):
    require_user(request)
    row = book_row(book_id)
    outfile = render_page(book_id, row["cover_page"], THUMB_WIDTH, "thumb")
    return FileResponse(outfile, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/books/{book_id}/preview/{page_num}")
def preview(book_id: int, page_num: int, request: Request):
    require_user(request)
    outfile = render_page(book_id, page_num, PREVIEW_WIDTH, "preview")
    return FileResponse(outfile, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/books/{book_id}/page/{page_num}")
def reader_page(book_id: int, page_num: int, request: Request, width: int = READER_WIDTH):
    require_user(request)
    outfile = render_page(book_id, page_num, width, "page")
    return FileResponse(outfile, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/books/{book_id}/file")
def original_file(book_id: int, request: Request, download: bool = False):
    require_user(request)
    row = book_row(book_id)
    p = safe_path(row["rel_path"])
    if not p.exists():
        raise HTTPException(404, "PDF file missing")
    if download:
        return FileResponse(p, media_type="application/pdf", filename=p.name)
    return FileResponse(p, media_type="application/pdf")


@app.post("/api/scan", dependencies=[Depends(require_ajax)])
def scan(request: Request):
    require_user(request)
    if scan_state.get("running"):
        return {"ok": True, "already_running": True, "scan": scan_state}
    threading.Thread(target=perform_scan, name="manual-scan", daemon=True).start()
    return {"ok": True, "scan": scan_state}


@app.get("/api/scan/status")
def scan_status(request: Request):
    require_user(request)
    return scan_state


@app.get("/api/tags")
def list_tags(request: Request):
    require_user(request)
    with db(readonly=True) as con:
        rows = con.execute("SELECT t.name,COUNT(bt.book_id) n FROM tags t LEFT JOIN book_tags bt ON bt.tag_id=t.id GROUP BY t.id ORDER BY t.name COLLATE NOCASE").fetchall()
    return [{"name": r["name"], "count": r["n"]} for r in rows]
