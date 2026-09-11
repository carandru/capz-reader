from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .auth import cleanup_sessions, protected_router as auth_protected_router, public_router as auth_public_router
from .config import APP_NAME, APP_VERSION, AUTO_SCAN_ON_START
from .db import init_db
from .diagnostics import router as diagnostics_router
from .library import router as library_router
from .media import router as media_router
from .pdf import cleanup_render_cache, thumb_worker
from .scan_api import router as scan_router
from .scanning import perform_scan

STATIC_DIR = Path(__file__).parent / "static"


def _cache_cleanup_worker() -> None:
    # Generated page cache cleanup is intentionally infrequent to stay light on NAS I/O.
    while True:
        try:
            cleanup_render_cache()
        except Exception:
            pass
        time.sleep(24 * 60 * 60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    cleanup_sessions()
    try:
        cleanup_render_cache()
    except Exception:
        pass
    threading.Thread(target=thumb_worker, name="thumb-worker", daemon=True).start()
    threading.Thread(target=_cache_cleanup_worker, name="render-cache-cleanup", daemon=True).start()
    if AUTO_SCAN_ON_START:
        threading.Thread(target=perform_scan, name="startup-scan", daemon=True).start()
    yield


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(auth_public_router)
app.include_router(auth_protected_router)
app.include_router(library_router)
app.include_router(media_router)
app.include_router(scan_router)
app.include_router(diagnostics_router)


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(
        (STATIC_DIR / "index.html").read_text("utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/reader/{book_id}", response_class=HTMLResponse)
def reader(book_id: int):
    return HTMLResponse(
        (STATIC_DIR / "reader.html").read_text("utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def sw():
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/api/health")
def health():
    # Public liveness only: do not expose NAS paths, source names, or DB location.
    return {"ok": True, "app": APP_NAME, "version": APP_VERSION}
