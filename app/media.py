from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse

from .auth import require_user
from .config import PREVIEW_WIDTH, READER_WIDTH, THUMB_WIDTH
from .db import book_row
from .epub import epub_cover_file
from .pdf import render_page
from .utils import safe_path

router = APIRouter(prefix="/api/books", tags=["media"], dependencies=[Depends(require_user)])


def _private_file_response(path: Path, *, media_type: str, max_age: int) -> FileResponse:
    return FileResponse(
        path,
        media_type=media_type,
        headers={"Cache-Control": f"private, max-age={max_age}"},
    )


def _render_response(book_id: int, page_num: int, width: int, kind: str, max_age: int) -> FileResponse:
    outfile = render_page(book_id, page_num, width, kind)
    return _private_file_response(outfile, media_type="image/jpeg", max_age=max_age)


@router.get("/{book_id}/thumb")
def thumb(book_id: int):
    row = book_row(book_id)
    if row["file_type"] == "epub":
        outfile, media = epub_cover_file(book_id, row)
        if outfile:
            return _private_file_response(outfile, media_type=media, max_age=86400)
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="440" viewBox="0 0 320 440"><rect width="320" height="440" fill="#1f2937"/><text x="160" y="205" text-anchor="middle" fill="#f9fafb" font-family="sans-serif" font-size="40">EPUB</text><text x="160" y="245" text-anchor="middle" fill="#9ca3af" font-family="sans-serif" font-size="16">No cover</text></svg>'
        return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=86400"})
    return _render_response(book_id, row["cover_page"], THUMB_WIDTH, "thumb", 86400)


@router.get("/{book_id}/preview/{page_num}")
def preview(book_id: int, page_num: int, width: int = PREVIEW_WIDTH):
    return _render_response(book_id, page_num, width, "preview", 86400)


@router.get("/{book_id}/page/{page_num}")
def reader_page(book_id: int, page_num: int, width: int = READER_WIDTH):
    return _render_response(book_id, page_num, width, "page", 604800)


@router.get("/{book_id}/file")
def original_file(book_id: int, download: bool = False):
    row = book_row(book_id)
    p = safe_path(row["rel_path"])
    if not p.exists():
        raise HTTPException(404, "Book file missing")
    media = "application/epub+zip" if row["file_type"] == "epub" else "application/pdf"
    headers = {"Cache-Control": "private, no-cache"}
    if download:
        return FileResponse(p, media_type=media, filename=p.name, headers=headers)
    return FileResponse(p, media_type=media, headers=headers)
