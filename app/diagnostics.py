from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .auth import require_user
from .config import APP_NAME, APP_VERSION, DB_PATH, LIBRARY_ROOT, MISSING_RETENTION_DAYS

router = APIRouter(prefix="/api", tags=["diagnostics"], dependencies=[Depends(require_user)])


@router.get("/diagnostics")
def diagnostics():
    sources: dict[str, Any] = {}
    if LIBRARY_ROOT.exists():
        try:
            for root in LIBRARY_ROOT.iterdir():
                if not root.is_dir():
                    continue
                try:
                    next(root.iterdir(), None)
                    readable = True
                except (OSError, PermissionError):
                    readable = False
                sources[root.name] = {
                    "path": str(root),
                    "mounted": True,
                    "readable": readable,
                }
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
