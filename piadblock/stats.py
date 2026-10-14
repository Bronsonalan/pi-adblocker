"""In-memory query counters, flushed to JSON for the ``stats`` command."""

from __future__ import annotations

import threading
import time
from collections import deque

from piadblock.dns import type_name
from piadblock.files import atomic_write


class Stats:
    """Thread-safe counters. The status page reads :meth:`snapshot` live."""

    def __init__(self, path: str = "", recent_limit: int = 40):
        self.path = path
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._counters = {
            "queries": 0,
            "blocked": 0,
            "cached": 0,
            "forwarded": 0,
            "errors": 0,
        }
        self._recent: deque[dict] = deque(maxlen=recent_limit)

    def bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def note_blocked(self, name: str, qtype: int, client: str) -> None:
        item = {
            "ts": time.time(),
            "name": name,
            "qtype": type_name(qtype),
            "client": client,
        }
        with self._lock:
            self._recent.appendleft(item)

    def snapshot(self) -> dict:
        with self._lock:
            data = dict(self._counters)
            recent = list(self._recent)
        data["started_at"] = self.started_at
        data["uptime_seconds"] = max(0, int(time.time() - self.started_at))
        data["recent_blocked"] = recent
        queries = data["queries"]
        data["blocked_percent"] = round(100.0 * data["blocked"] / queries, 1) if queries else 0.0
        return data

    def save(self) -> None:
        if not self.path:
            return
        import json

        try:
            atomic_write(self.path, json.dumps(self.snapshot(), indent=2) + "\n")
        except OSError:
            return
