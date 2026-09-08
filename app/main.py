from __future__ import annotations

import hashlib
import os
import queue
import re
import secrets
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_NAME = os.getenv("APP_NAME", "NAS PDF Reader")
APP_VERSION = "0.2.0"
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
MISSING_RETENTION_DAYS = max(1, int(os.getenv("MISSING_RETENTION_DAYS", "14")))
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
scan_state: dict[str, Any] = {
    "running": False,
    "seen": 0,
    "added": 0,
    "updated": 0,
    "missing": 0,
    "restored": 0,
    "purged": 0,
    "sources_skipped": [],
    "error": None,
    "finished_at": None,
}
thumb_queue: queue.Queue[int] = queue.Queue(maxsize=5000)
queued_thumbs: set[int] = set()
queued_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def books_table_sql(name: str = "books") -> str:
    return f"""
        CREATE TABLE {name} (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          rel_path TEXT NOT NULL UNIQUE,
          title TEXT NOT NULL,
          custom_title INTEGER NOT NULL DEFAULT 0,
          category TEXT NOT NULL DEFAULT 'Uncategorized',
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
          missing_since TEXT,
          added_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
    """


def create_book_indexes(con: sqlite3.Connection):
    con.execute("CREATE INDEX IF NOT EXISTS idx_books_category ON books(category)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_books_series ON books(series)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_books_title ON books(title)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_books_missing ON books(missing_since)")


def migrate_books_if_needed(con: sqlite3.Connection):
    if not table_exists(con, "books"):
        con.execute(books_table_sql())
        create_book_indexes(con)
        return

    cols = {r[1] for r in con.execute("PRAGMA table_info(books)").fetchall()}
    sql_row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='books'").fetchone()
    sql = (sql_row[0] if sql_row else "") or ""
    needs_rebuild = "missing_since" not in cols or "CHECK(category IN" in sql
    if not needs_rebuild:
        create_book_indexes(con)
        return

    tag_pairs: list[tuple[int, int]] = []
    if table_exists(con, "book_tags"):
        tag_pairs = [(r[0], r[1]) for r in con.execute("SELECT book_id,tag_id FROM book_tags").fetchall()]
        con.execute("DROP TABLE book_tags")

    con.execute(books_table_sql("books_v02"))
    old_cols = {r[1] for r in con.execute("PRAGMA table_info(books)").fetchall()}
    target_cols = [
        "id", "rel_path", "title", "custom_title", "category", "series", "volume", "cover_page",
        "page_count", "size", "mtime", "favorite", "archived", "read_state", "last_page",
        "minutes_spent", "last_opened", "added_at", "updated_at",
    ]
    copy_cols = [c for c in target_cols if c in old_cols]
    cols_csv = ",".join(copy_cols)
    con.execute(f"INSERT INTO books_v02({cols_csv}) SELECT {cols_csv} FROM books")
    con.execute("DROP TABLE books")
    con.execute("ALTER TABLE books_v02 RENAME TO books")
    create_book_indexes(con)
    con.execute(
        """
        CREATE TABLE book_tags (
          book_id INTEGER NOT NULL,
          tag_id INTEGER NOT NULL,
          PRIMARY KEY(book_id, tag_id),
          FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE,
          FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
        """
    )
    if tag_pairs:
        valid_books = {r[0] for r in con.execute("SELECT id FROM books").fetchall()}
        valid_tags = {r[0] for r in con.execute("SELECT id FROM tags").fetchall()}
        pairs = [(b, t) for b, t in tag_pairs if b in valid_books and t in valid_tags]
        con.executemany("INSERT OR IGNORE INTO book_tags(book_id,tag_id) VALUES(?,?)", pairs)


def ensure_category(con: sqlite3.Connection, name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())[:120]
    if not name:
        name = "Uncategorized"
    row = con.execute("SELECT name FROM categories WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row[0]
    order = con.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM categories").fetchone()[0]
    con.execute(
        "INSERT INTO categories(name,sort_order,visible,created_at) VALUES(?,?,1,?)",
        (name, order, utc_now()),
    )
    return name


def init_db():
    with write_lock, db() as con:
        con.execute("PRAGMA foreign_keys=OFF")
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
            CREATE TABLE IF NOT EXISTS tags (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE COLLATE NOCASE
            );
            CREATE TABLE IF NOT EXISTS categories (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE COLLATE NOCASE,
              sort_order INTEGER NOT NULL DEFAULT 0,
              visible INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ignored_paths (
              rel_path TEXT PRIMARY KEY,
              ignored_at TEXT NOT NULL
            );
            """
        )
        migrate_books_if_needed(con)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS book_tags (
              book_id INTEGER NOT NULL,
              tag_id INTEGER NOT NULL,
              PRIMARY KEY(book_id, tag_id),
              FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE,
              FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE
            )
            """
        )
        for r in con.execute("SELECT DISTINCT category FROM books WHERE category IS NOT NULL AND TRIM(category)<>''").fetchall():
            ensure_category(con, r[0])
        if con.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            ensure_category(con, "Novel")
            ensure_category(con, "Manga")
        con.execute("PRAGMA foreign_keys=ON")


def safe_path(rel_path: str) -> Path:
    clean = (rel_path or "").replace("\\", "/").lstrip("/")
    p = (LIBRARY_ROOT / clean).resolve()
    if LIBRARY_ROOT != p and LIBRARY_ROOT not in p.parents:
        raise HTTPException(400, "Invalid path")
    return p


def safe_dir(rel_path: str) -> Path:
    p = safe_path(rel_path)
    if not p.exists() or not p.is_dir():
        raise HTTPException(404, "Folder not found")
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


def source_name_for_rel(rel: str) -> str:
    parts = Path(rel).parts
    return parts[0] if parts else "Uncategorized"


def inferred_series_for_rel(rel: str) -> str | None:
    parts = Path(rel).parts
    return parts[1] if len(parts) >= 3 else None


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
        f"SELECT bt.book_id,t.name FROM book_tags bt JOIN tags t ON t.id=bt.tag_id WHERE bt.book_id IN ({marks}) ORDER BY t.name COLLATE NOCASE",
        ids,
    ).fetchall()
    for r in rows:
        out[r["book_id"]].append(r["name"])
    return out


def book_to_dict(row: sqlite3.Row, tags: list[str] | None = None) -> dict[str, Any]:
    d = dict(row)
    d["favorite"] = bool(d["favorite"])
    d["archived"] = bool(d["archived"])
    d["missing"] = bool(d.get("missing_since"))
    d["tags"] = tags or []
    d["progress"] = round((d["last_page"] / d["page_count"] * 100), 1) if d["page_count"] else 0
    return d


def clear_book_cache(book_id: int, thumbs_only: bool = False):
    for p in (CACHE_DIR / "thumbs").glob(f"{book_id}-*.jpg"):
        p.unlink(missing_ok=True)
    if not thumbs_only:
        shutil.rmtree(CACHE_DIR / "preview" / str(book_id), ignore_errors=True)
        shutil.rmtree(CACHE_DIR / "pages" / str(book_id), ignore_errors=True)


def render_page(book_id: int, page_num: int, width: int, kind: str) -> Path:
    row = book_row(book_id)
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
            if not row["missing_since"]:
                render_page(book_id, row["cover_page"], THUMB_WIDTH, "thumb")
        except Exception:
            pass
        finally:
            with queued_lock:
                queued_thumbs.discard(book_id)
            thumb_queue.task_done()


def register_pdf(rel: str, *, restore_ignored: bool = False, category_override: str | None = None) -> str:
    p = safe_path(rel)
    if not p.exists() or not p.is_file() or p.suffix.lower() != ".pdf":
        raise HTTPException(404, f"PDF not found: {rel}")
    rel = p.relative_to(LIBRARY_ROOT).as_posix()
    st = p.stat()
    now = utc_now()
    with write_lock, db() as con:
        if restore_ignored:
            con.execute("DELETE FROM ignored_paths WHERE rel_path=?", (rel,))
        elif con.execute("SELECT 1 FROM ignored_paths WHERE rel_path=?", (rel,)).fetchone():
            return "ignored"

        old = con.execute("SELECT * FROM books WHERE rel_path=?", (rel,)).fetchone()
        if old:
            changed = old["size"] != st.st_size or abs(old["mtime"] - st.st_mtime) >= 0.001
            restored = bool(old["missing_since"])
            title = old["title"] if old["custom_title"] else filename_title(p)
            pages = pdf_page_count(p) if changed or restored or old["page_count"] <= 0 else old["page_count"]
            category = old["category"]
            if category_override:
                category = ensure_category(con, category_override)
            con.execute(
                "UPDATE books SET title=?,category=?,page_count=?,size=?,mtime=?,missing_since=NULL,updated_at=? WHERE id=?",
                (title, category, pages, st.st_size, st.st_mtime, now, old["id"]),
            )
            if changed:
                clear_book_cache(old["id"])
            enqueue_thumb(int(old["id"]))
            return "restored" if restored else ("updated" if changed else "kept")

        source = source_name_for_rel(rel)
        category = ensure_category(con, category_override or source)
        title = filename_title(p)
        series = inferred_series_for_rel(rel)
        volume = guess_volume(title)
        pages = pdf_page_count(p)
        cur = con.execute(
            """
            INSERT INTO books(rel_path,title,category,series,volume,page_count,size,mtime,added_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (rel, title, category, series, volume, pages, st.st_size, st.st_mtime, now, now),
        )
        book_id = int(cur.lastrowid)
    enqueue_thumb(book_id)
    return "added"


def purge_missing(*, force: bool = False) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=MISSING_RETENTION_DAYS)
    with write_lock, db() as con:
        if force:
            rows = con.execute("SELECT id FROM books WHERE missing_since IS NOT NULL").fetchall()
        else:
            rows = con.execute("SELECT id FROM books WHERE missing_since IS NOT NULL AND missing_since<=?", (cutoff.isoformat(),)).fetchall()
        ids = [r[0] for r in rows]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        con.execute(f"DELETE FROM books WHERE id IN ({marks})", ids)
    for book_id in ids:
        clear_book_cache(book_id)
    return len(ids)


def readable_source_dirs() -> tuple[list[Path], list[str]]:
    if not LIBRARY_ROOT.exists() or not LIBRARY_ROOT.is_dir():
        raise RuntimeError(f"Library path not found: {LIBRARY_ROOT}")
    try:
        children = [p for p in LIBRARY_ROOT.iterdir() if p.is_dir()]
    except OSError as exc:
        raise RuntimeError(f"Library path is not readable: {exc}") from exc
    good: list[Path] = []
    skipped: list[str] = []
    for root in children:
        try:
            next(root.iterdir(), None)
            good.append(root)
        except (OSError, PermissionError):
            skipped.append(root.name)
    return good, skipped


def perform_scan():
    global scan_state
    if not scan_lock.acquire(blocking=False):
        return
    scan_state = {
        "running": True,
        "seen": 0,
        "added": 0,
        "updated": 0,
        "missing": 0,
        "restored": 0,
        "purged": 0,
        "sources_skipped": [],
        "error": None,
        "finished_at": None,
    }
    try:
        roots, skipped = readable_source_dirs()
        scan_state["sources_skipped"] = skipped
        successful_sources: set[str] = set()
        seen: set[str] = set()

        for root in roots:
            source_ok = True
            try:
                for p in root.rglob("*"):
                    if not p.is_file() or p.suffix.lower() != ".pdf":
                        continue
                    rel = p.relative_to(LIBRARY_ROOT).as_posix()
                    seen.add(rel)
                    scan_state["seen"] += 1
                    result = register_pdf(rel)
                    if result == "added":
                        scan_state["added"] += 1
                    elif result == "updated":
                        scan_state["updated"] += 1
                    elif result == "restored":
                        scan_state["restored"] += 1
            except (OSError, PermissionError) as exc:
                source_ok = False
                scan_state["sources_skipped"].append(f"{root.name}: {exc}")
            if source_ok:
                successful_sources.add(root.name)
                with write_lock, db() as con:
                    ensure_category(con, root.name)

        with db(readonly=True) as con:
            existing = con.execute("SELECT id,rel_path,missing_since FROM books").fetchall()
        newly_missing: list[int] = []
        now = utc_now()
        with write_lock, db() as con:
            for row in existing:
                source = source_name_for_rel(row["rel_path"])
                if source not in successful_sources:
                    continue
                if row["rel_path"] not in seen and not row["missing_since"]:
                    con.execute("UPDATE books SET missing_since=?,updated_at=? WHERE id=?", (now, now, row["id"]))
                    newly_missing.append(row["id"])
        scan_state["missing"] = len(newly_missing)
        scan_state["purged"] = purge_missing(force=False)
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


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


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


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CategoryEdit(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    visible: bool | None = None
    sort_order: int | None = None


class CategoryDelete(BaseModel):
    move_to: str | None = None


class ImportBody(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=1000)
    category: str | None = None


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
    sources: dict[str, Any] = {}
    if LIBRARY_ROOT.exists():
        try:
            for root in LIBRARY_ROOT.iterdir():
                if root.is_dir():
                    try:
                        next(root.iterdir(), None)
                        readable = True
                    except (OSError, PermissionError):
                        readable = False
                    sources[root.name] = {"path": str(root), "mounted": True, "readable": readable}
        except OSError:
            pass
    return {
        "ok": True,
        "app": APP_NAME,
        "version": APP_VERSION,
        "library": str(LIBRARY_ROOT),
        "db": str(DB_PATH),
        "missing_retention_days": MISSING_RETENTION_DAYS,
        "sources": sources,
    }


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


@app.post("/api/auth/change-password", dependencies=[Depends(require_ajax)])
def change_password(body: ChangePasswordBody, request: Request):
    user = require_user(request)
    with db(readonly=True) as con:
        row = con.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not row:
        raise HTTPException(404, "User not found")
    try:
        ph.verify(row["password_hash"], body.current_password)
    except VerifyMismatchError:
        raise HTTPException(401, "Current password is incorrect")
    current_token = request.cookies.get("nas_reader_session")
    current_hash = hashlib.sha256(current_token.encode()).hexdigest() if current_token else ""
    with write_lock, db() as con:
        con.execute("UPDATE users SET password_hash=? WHERE id=?", (ph.hash(body.new_password), user["id"]))
        con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user["id"], current_hash))
    return {"ok": True}


@app.post("/api/auth/logout", dependencies=[Depends(require_ajax)])
def logout(request: Request, response: Response):
    token = request.cookies.get("nas_reader_session")
    if token:
        with write_lock, db() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie("nas_reader_session", path="/")
    return {"ok": True}


def categories_payload(con: sqlite3.Connection) -> list[dict[str, Any]]:
    counts = {r[0]: r[1] for r in con.execute("SELECT category,COUNT(*) FROM books WHERE missing_since IS NULL GROUP BY category").fetchall()}
    rows = con.execute("SELECT * FROM categories ORDER BY sort_order,name COLLATE NOCASE").fetchall()
    return [
        {"id": r["id"], "name": r["name"], "sort_order": r["sort_order"], "visible": bool(r["visible"]), "count": counts.get(r["name"], 0)}
        for r in rows
    ]


@app.get("/api/categories")
def list_categories(request: Request):
    require_user(request)
    with db(readonly=True) as con:
        return categories_payload(con)


@app.post("/api/categories", dependencies=[Depends(require_ajax)])
def create_category(body: CategoryCreate, request: Request):
    require_user(request)
    name = re.sub(r"\s+", " ", body.name.strip())
    with write_lock, db() as con:
        if con.execute("SELECT 1 FROM categories WHERE name=? COLLATE NOCASE", (name,)).fetchone():
            raise HTTPException(409, "Category already exists")
        ensure_category(con, name)
        return categories_payload(con)


@app.patch("/api/categories/{category_id}", dependencies=[Depends(require_ajax)])
def edit_category(category_id: int, body: CategoryEdit, request: Request):
    require_user(request)
    data = body.model_dump(exclude_unset=True)
    with write_lock, db() as con:
        row = con.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Category not found")
        new_name = row["name"]
        if "name" in data:
            new_name = re.sub(r"\s+", " ", data["name"].strip())
            dupe = con.execute("SELECT id FROM categories WHERE name=? COLLATE NOCASE AND id<>?", (new_name, category_id)).fetchone()
            if dupe:
                raise HTTPException(409, "Category already exists")
            con.execute("UPDATE books SET category=?,updated_at=? WHERE category=?", (new_name, utc_now(), row["name"]))
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


@app.post("/api/categories/{category_id}/delete", dependencies=[Depends(require_ajax)])
def delete_category(category_id: int, body: CategoryDelete, request: Request):
    require_user(request)
    with write_lock, db() as con:
        row = con.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Category not found")
        count = con.execute("SELECT COUNT(*) FROM books WHERE category=?", (row["name"],)).fetchone()[0]
        if count:
            if not body.move_to:
                raise HTTPException(409, f"Category contains {count} books; choose a destination first")
            target = con.execute("SELECT name FROM categories WHERE name=? COLLATE NOCASE", (body.move_to,)).fetchone()
            if not target or target[0].casefold() == row["name"].casefold():
                raise HTTPException(400, "Invalid destination category")
            con.execute("UPDATE books SET category=?,updated_at=? WHERE category=?", (target[0], utc_now(), row["name"]))
        con.execute("DELETE FROM categories WHERE id=?", (category_id,))
        return categories_payload(con)


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
    where = ["missing_since IS NULL"]
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
        series_rows = con.execute(
            "SELECT category,COALESCE(series,'') AS series,COUNT(*) n FROM books WHERE missing_since IS NULL GROUP BY category,series ORDER BY series COLLATE NOCASE"
        ).fetchall()
        categories = categories_payload(con)
        missing_count = con.execute("SELECT COUNT(*) FROM books WHERE missing_since IS NOT NULL").fetchone()[0]
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
        "categories": categories,
        "total_count": sum(c["count"] for c in categories),
        "count": len(books),
        "missing_count": missing_count,
        "missing_retention_days": MISSING_RETENTION_DAYS,
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
    updates: list[str] = []
    params: list[Any] = []
    data = body.model_dump(exclude_unset=True)
    tags = data.pop("tags", None)
    if "category" in data:
        with db(readonly=True) as con:
            cat = con.execute("SELECT name FROM categories WHERE name=? COLLATE NOCASE", (data["category"],)).fetchone()
        if not cat:
            raise HTTPException(400, "Invalid category")
        data["category"] = cat[0]
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


@app.post("/api/books/{book_id}/reset-title", dependencies=[Depends(require_ajax)])
def reset_title(book_id: int, request: Request):
    require_user(request)
    row = book_row(book_id)
    p = safe_path(row["rel_path"])
    title = filename_title(p)
    with write_lock, db() as con:
        con.execute("UPDATE books SET title=?,custom_title=0,updated_at=? WHERE id=?", (title, utc_now(), book_id))
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
            cat = con.execute("SELECT name FROM categories WHERE name=? COLLATE NOCASE", (str(value),)).fetchone()
            if not cat:
                raise HTTPException(400, "Invalid category")
            con.execute(f"UPDATE books SET category=?,updated_at=? WHERE id IN ({marks})", [cat[0], utc_now(), *ids])
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
    if action in {"regen_preview", "remove_library", "delete_files"}:
        for i in ids:
            clear_book_cache(i)
            if action == "regen_preview":
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


@app.post("/api/missing/clean", dependencies=[Depends(require_ajax)])
def clean_missing(request: Request):
    require_user(request)
    if scan_state.get("running"):
        raise HTTPException(409, "Wait for the current rescan to finish")
    return {"ok": True, "purged": purge_missing(force=True)}


@app.get("/api/import/browse")
def import_browse(request: Request, path: str = ""):
    require_user(request)
    root = safe_dir(path)
    rel_root = "" if root == LIBRARY_ROOT else root.relative_to(LIBRARY_ROOT).as_posix()
    dirs: list[dict[str, str]] = []
    pdfs: list[dict[str, Any]] = []
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
        elif p.is_file() and p.suffix.lower() == ".pdf":
            pdfs.append({"name": p.name, "path": rel, "indexed": rel in indexed})
    parent = ""
    if rel_root:
        parent_path = Path(rel_root).parent
        parent = "" if str(parent_path) == "." else parent_path.as_posix()
    return {"path": rel_root, "parent": parent, "dirs": dirs, "pdfs": pdfs}


@app.post("/api/import", dependencies=[Depends(require_ajax)])
def import_paths(body: ImportBody, request: Request):
    require_user(request)
    if scan_state.get("running"):
        raise HTTPException(409, "Wait for the current rescan to finish")
    added = updated = restored = kept = 0
    errors: list[str] = []
    with scan_lock:
        for rel in list(dict.fromkeys(body.paths)):
            try:
                result = register_pdf(rel, restore_ignored=True, category_override=body.category)
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
    return {"ok": not errors, "added": added, "updated": updated, "restored": restored, "kept": kept, "errors": errors[:20]}


@app.get("/api/tags")
def list_tags(request: Request):
    require_user(request)
    with db(readonly=True) as con:
        rows = con.execute("SELECT t.name,COUNT(bt.book_id) n FROM tags t LEFT JOIN book_tags bt ON bt.tag_id=t.id GROUP BY t.id ORDER BY t.name COLLATE NOCASE").fetchall()
    return [{"name": r["name"], "count": r["n"]} for r in rows]
