from __future__ import annotations

import sqlite3
from typing import Any

from .utils import natural_key


def series_summary(con: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = con.execute(
        "SELECT category,COALESCE(series,'') AS series,COUNT(*) n "
        "FROM books WHERE missing_since IS NULL "
        "GROUP BY category,series ORDER BY series COLLATE NOCASE"
    ).fetchall()
    return [
        {"category": r["category"], "series": r["series"], "count": r["n"]}
        for r in rows
        if r["series"]
    ]


def sort_books(books: list[dict[str, Any]], sort: str) -> None:
    """Apply the exact ordering used by the Library/Series UI in place."""
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
