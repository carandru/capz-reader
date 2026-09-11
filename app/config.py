from __future__ import annotations

import os
from pathlib import Path

APP_NAME = os.getenv("APP_NAME", "NAS PDF Reader")
APP_VERSION = "0.3.4"
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
RENDER_CACHE_DAYS = max(1, int(os.getenv("RENDER_CACHE_DAYS", "30")))
RENDER_CACHE_LIMIT_BYTES = max(256 * 1024 * 1024, int(float(os.getenv("RENDER_CACHE_LIMIT_GB", "8")) * 1024**3))

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
for child in ("thumbs", "preview", "pages", "epub-covers"):
    (CACHE_DIR / child).mkdir(parents=True, exist_ok=True)
