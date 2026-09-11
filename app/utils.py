from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from .config import LIBRARY_ROOT

SUPPORTED_BOOK_SUFFIXES = {".pdf", ".epub"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()



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


_THAI_LEADING_VOWEL_RE = re.compile(r"([เแโใไ])([ก-ฮ])")
_THAI_SECONDARY_MARK_RE = re.compile(r"[็่้๊๋์]")

def _human_text_key(value: str) -> str:
    """Portable fallback for server-side Thai-aware ordering.

    Browsers use Intl.Collator('th-TH') for the authoritative UI order.  On the
    server we avoid OS locale/PyICU dependencies and normalize Thai leading
    vowels behind their consonant so API/import ordering follows the same
    human expectation as closely as possible.
    """
    text = unicodedata.normalize("NFC", value).casefold()
    reordered = _THAI_LEADING_VOWEL_RE.sub(r"\2\1", text)
    # Tone/orthographic marks are secondary in Thai dictionary-style ordering;
    # ignoring them for the primary key prevents a tone mark from sorting ahead
    # of the actual vowel (e.g. ย่าง vs ยุทธ). Keep the full form as tie-breaker.
    primary = _THAI_SECONDARY_MARK_RE.sub("", reordered)
    return primary + "\0" + reordered

def natural_key(value: str | None):
    value = value or ""
    # Tag tuple parts so names beginning with digits can be compared safely
    # with names beginning with text on every supported Python version.
    return [
        (0, int(part)) if part.isdigit() else (1, _human_text_key(part))
        for part in re.split(r"(\d+)", value)
        if part != ""
    ]


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


def file_type_for_path(path: Path) -> str:
    return "epub" if path.suffix.lower() == ".epub" else "pdf"
