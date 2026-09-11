from __future__ import annotations

import queue
import threading
from typing import Any

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
    "unreadable": [],
    "suspicious_sources": [],
    "sources_skipped": [],
    "error": None,
    "finished_at": None,
}

thumb_queue: queue.Queue[int] = queue.Queue(maxsize=5000)
queued_thumbs: set[int] = set()
queued_lock = threading.Lock()


def reset_scan_state() -> None:
    scan_state.clear()
    scan_state.update(
        {
            "running": True,
            "seen": 0,
            "added": 0,
            "updated": 0,
            "missing": 0,
            "restored": 0,
            "purged": 0,
            "unreadable": [],
            "suspicious_sources": [],
            "sources_skipped": [],
            "error": None,
            "finished_at": None,
        }
    )
