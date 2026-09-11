from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

from fastapi import HTTPException

from .config import DB_PATH
from .state import write_lock
from .utils import utc_now

SCHEMA_VERSION = 3


@contextmanager
def db(readonly: bool = False):
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        con.execute("PRAGMA synchronous=NORMAL")
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
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


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
          file_type TEXT NOT NULL DEFAULT 'pdf' CHECK(file_type IN ('pdf','epub')),
          page_count INTEGER NOT NULL DEFAULT 0,
          size INTEGER NOT NULL DEFAULT 0,
          mtime REAL NOT NULL DEFAULT 0,
          favorite INTEGER NOT NULL DEFAULT 0,
          archived INTEGER NOT NULL DEFAULT 0,
          read_state TEXT NOT NULL DEFAULT 'unread' CHECK(read_state IN ('unread','reading','read')),
          last_page INTEGER NOT NULL DEFAULT 1,
          minutes_spent REAL NOT NULL DEFAULT 0,
          last_opened TEXT,
          epub_location TEXT,
          epub_progress REAL NOT NULL DEFAULT 0,
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
    con.execute("CREATE INDEX IF NOT EXISTS idx_books_last_opened ON books(last_opened)")


def migrate_books_if_needed(con: sqlite3.Connection):
    if not table_exists(con, "books"):
        con.execute(books_table_sql())
        create_book_indexes(con)
        return

    cols = {r[1] for r in con.execute("PRAGMA table_info(books)").fetchall()}
    sql_row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='books'"
    ).fetchone()
    sql = (sql_row[0] if sql_row else "") or ""
    needs_rebuild = "missing_since" not in cols or "CHECK(category IN" in sql
    if not needs_rebuild:
        create_book_indexes(con)
        return

    tag_pairs: list[tuple[int, int]] = []
    if table_exists(con, "book_tags"):
        tag_pairs = [
            (r[0], r[1])
            for r in con.execute("SELECT book_id,tag_id FROM book_tags").fetchall()
        ]
        con.execute("DROP TABLE book_tags")

    con.execute(books_table_sql("books_v02"))
    old_cols = {r[1] for r in con.execute("PRAGMA table_info(books)").fetchall()}
    target_cols = [
        "id", "rel_path", "title", "custom_title", "category", "series", "volume",
        "cover_page", "file_type", "page_count", "size", "mtime", "favorite", "archived",
        "read_state", "last_page", "minutes_spent", "last_opened", "epub_location",
        "epub_progress", "added_at", "updated_at",
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
        con.executemany(
            "INSERT OR IGNORE INTO book_tags(book_id,tag_id) VALUES(?,?)", pairs
        )


def normalize_name(value: str | None, *, max_len: int, fallback: str | None = None) -> str:
    name = re.sub(r"\s+", " ", (value or "").strip())[:max_len]
    if name:
        return name
    if fallback is not None:
        return fallback
    raise HTTPException(400, "Value cannot be blank")


def resolve_category(con: sqlite3.Connection, name: str | None) -> sqlite3.Row | None:
    clean = normalize_name(name, max_len=120) if name is not None else ""
    if not clean:
        return None
    return con.execute(
        "SELECT id,name,sort_order,visible FROM categories WHERE name=? COLLATE NOCASE",
        (clean,),
    ).fetchone()


def ensure_category(con: sqlite3.Connection, name: str) -> str:
    clean = normalize_name(name, max_len=120, fallback="Uncategorized")
    row = resolve_category(con, clean)
    if row:
        return row["name"]
    order = con.execute(
        "SELECT COALESCE(MAX(sort_order),-1)+1 FROM categories"
    ).fetchone()[0]
    con.execute(
        "INSERT INTO categories(name,sort_order,visible,created_at) VALUES(?,?,1,?)",
        (clean, order, utc_now()),
    )
    return clean


def get_or_create_tag_id(con: sqlite3.Connection, name: str) -> int:
    clean = normalize_name(name, max_len=120)
    con.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (clean,))
    row = con.execute(
        "SELECT id FROM tags WHERE name=? COLLATE NOCASE", (clean,)
    ).fetchone()
    if not row:
        raise HTTPException(500, "Unable to create tag")
    return int(row[0])


def cleanup_orphan_tags(con: sqlite3.Connection) -> None:
    con.execute(
        "DELETE FROM tags WHERE NOT EXISTS (SELECT 1 FROM book_tags bt WHERE bt.tag_id=tags.id)"
    )


def calculate_read_state(
    current_state: str,
    *,
    completed: bool,
    started: bool,
) -> str:
    # Completed is sticky. Reopening an old page/chapter must not undo completion.
    if current_state == "read" or completed:
        return "read"
    if started:
        return "reading"
    return current_state or "unread"


def chunked(values: list[int], size: int = 800) -> Iterable[list[int]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def init_db():
    # WAL is a database-level setting: establish it once rather than on every write connection.
    boot = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    try:
        boot.execute("PRAGMA busy_timeout=30000")
        boot.execute("PRAGMA journal_mode=WAL")
        boot.execute("PRAGMA synchronous=NORMAL")
        boot.commit()
    finally:
        boot.close()

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

        user_version = int(con.execute("PRAGMA user_version").fetchone()[0])
        if user_version < SCHEMA_VERSION:
            book_cols = {r[1] for r in con.execute("PRAGMA table_info(books)").fetchall()}
            if "file_type" not in book_cols:
                con.execute("ALTER TABLE books ADD COLUMN file_type TEXT NOT NULL DEFAULT 'pdf'")
            if "epub_location" not in book_cols:
                con.execute("ALTER TABLE books ADD COLUMN epub_location TEXT")
            if "epub_progress" not in book_cols:
                con.execute("ALTER TABLE books ADD COLUMN epub_progress REAL NOT NULL DEFAULT 0")
            con.execute(
                "UPDATE books SET file_type=CASE WHEN LOWER(rel_path) LIKE '%.epub' THEN 'epub' ELSE 'pdf' END"
            )
            con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

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
        for r in con.execute(
            "SELECT DISTINCT category FROM books WHERE category IS NOT NULL AND TRIM(category)<>''"
        ).fetchall():
            ensure_category(con, r[0])
        if con.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            ensure_category(con, "Novel")
            ensure_category(con, "Manga")
        cleanup_orphan_tags(con)
        create_book_indexes(con)
        con.execute("PRAGMA foreign_keys=ON")


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
    for batch in chunked(ids):
        marks = ",".join("?" for _ in batch)
        rows = con.execute(
            f"SELECT bt.book_id,t.name FROM book_tags bt JOIN tags t ON t.id=bt.tag_id "
            f"WHERE bt.book_id IN ({marks}) ORDER BY t.name COLLATE NOCASE",
            batch,
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
    if d.get("file_type") == "epub":
        d["progress"] = round(float(d.get("epub_progress") or 0), 1)
    else:
        d["progress"] = (
            round((d["last_page"] / d["page_count"] * 100), 1)
            if d["page_count"]
            else 0
        )
    return d
