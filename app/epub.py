from __future__ import annotations

import mimetypes
import sqlite3
import zipfile
from pathlib import Path
from posixpath import dirname as posix_dirname, join as posix_join, normpath as posix_normpath
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import fitz
from fastapi import HTTPException

from .config import CACHE_DIR, THUMB_WIDTH
from .utils import safe_path


def _epub_resolve(base_path: str, href: str) -> str:
    href = unquote((href or "").split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
    resolved = posix_normpath(posix_join(posix_dirname(base_path), href)).lstrip("/")
    if resolved.startswith("../"):
        raise ValueError("EPUB resource escapes archive root")
    return resolved


def epub_info(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"spine_count": 0, "cover_path": None, "cover_media": None}
    try:
        with zipfile.ZipFile(path) as zf:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
            rootfile = container.find(".//{*}rootfile")
            if rootfile is None or not rootfile.attrib.get("full-path"):
                return info
            opf_path = unquote(rootfile.attrib["full-path"]).lstrip("/")
            opf = ET.fromstring(zf.read(opf_path))
            manifest: dict[str, dict[str, str]] = {}
            for item in opf.findall(".//{*}manifest/{*}item"):
                item_id = item.attrib.get("id")
                href = item.attrib.get("href")
                if not item_id or not href:
                    continue
                try:
                    resolved = _epub_resolve(opf_path, href)
                except ValueError:
                    continue
                manifest[item_id] = {
                    "path": resolved,
                    "media": item.attrib.get("media-type", ""),
                    "properties": item.attrib.get("properties", ""),
                }
            spine_ids = [
                x.attrib.get("idref")
                for x in opf.findall(".//{*}spine/{*}itemref")
                if x.attrib.get("idref")
            ]
            info["spine_count"] = len([x for x in spine_ids if x in manifest])
            cover_item = next(
                (v for v in manifest.values() if "cover-image" in v.get("properties", "").split()),
                None,
            )
            if cover_item is None:
                cover_id = None
                for meta in opf.findall(".//{*}metadata/{*}meta"):
                    if (meta.attrib.get("name") or "").lower() == "cover":
                        cover_id = meta.attrib.get("content")
                        break
                if cover_id and cover_id in manifest:
                    cover_item = manifest[cover_id]
            if cover_item is None:
                cover_item = next(
                    (
                        v
                        for k, v in manifest.items()
                        if "cover" in k.lower() and v.get("media", "").startswith("image/")
                    ),
                    None,
                )
            if cover_item and cover_item["path"] in zf.namelist():
                info["cover_path"] = cover_item["path"]
                info["cover_media"] = (
                    cover_item.get("media")
                    or mimetypes.guess_type(cover_item["path"])[0]
                    or "image/jpeg"
                )
    except Exception:
        return info
    return info


def epub_spine_count(path: Path) -> int:
    return int(epub_info(path).get("spine_count") or 0)


def _write_resized_cover(data: bytes, outfile: Path, width: int) -> bool:
    """Use MuPDF already bundled for PDF support; do not add another image dependency."""
    try:
        pix = fitz.Pixmap(data)
        if pix.n - pix.alpha > 3:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        if pix.width > width:
            target_h = max(1, round(pix.height * width / max(pix.width, 1)))
            pix = fitz.Pixmap(pix, width, target_h)
        pix.save(str(outfile))
        return True
    except Exception:
        return False


def epub_cover_file(book_id: int, row: sqlite3.Row, width: int = THUMB_WIDTH) -> tuple[Path | None, str]:
    src = safe_path(row["rel_path"])
    if not src.exists():
        raise HTTPException(404, "EPUB file missing")
    info = epub_info(src)
    cover_path = info.get("cover_path")
    if not cover_path:
        return None, "image/svg+xml"
    stamp = f"{int(src.stat().st_mtime)}-{src.stat().st_size}-{width}"
    outfile = CACHE_DIR / "epub-covers" / f"{book_id}-{stamp}.jpg"
    if outfile.exists():
        return outfile, "image/jpeg"
    try:
        with zipfile.ZipFile(src) as zf:
            data = zf.read(str(cover_path))
        if _write_resized_cover(data, outfile, width):
            return outfile, "image/jpeg"
        # Last-resort compatibility fallback: cache the original cover bytes.
        media = str(info.get("cover_media") or "image/jpeg")
        suffix = Path(str(cover_path)).suffix.lower() or mimetypes.guess_extension(media) or ".img"
        raw_out = CACHE_DIR / "epub-covers" / f"{book_id}-{stamp}{suffix}"
        raw_out.write_bytes(data)
        return raw_out, media
    except Exception:
        return None, "image/svg+xml"
