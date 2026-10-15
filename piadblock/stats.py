"""In-memory query counters, flushed to JSON for the ``stats`` command.

``recent_blocked`` is a short sample for the status page. The day's
``days[YYYY-MM-DD].domains`` map counts every blocked name and is reloaded
from this file when the process starts, so a restart does not zero the day.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path

from piadblock.dns import type_name
from piadblock.files import atomic_write
from piadblock.forward import UPSTREAM_REASONS


def utc_day(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


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
        self._reasons = {reason: 0 for reason in UPSTREAM_REASONS}
        self._days: dict[str, dict] = {}
        self._load_days()

    def bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount
            bucket = self._bucket_locked()
            if name == "queries":
                bucket["queries"] += amount
            elif name == "blocked":
                bucket["blocked"] += amount

    def note_upstream(self, reason: str) -> None:
        """Count one upstream attempt. A later server can still answer the query."""
        if reason not in UPSTREAM_REASONS:
            return
        with self._lock:
            self._reasons[reason] = self._reasons.get(reason, 0) + 1
            reasons = self._bucket_locked()["upstream_errors"]
            reasons[reason] = int(reasons.get(reason, 0)) + 1

    def note_blocked(self, name: str, qtype: int, client: str) -> None:
        domain = (name or "").lower().rstrip(".")
        item = {
            "ts": time.time(),
            "name": name,
            "qtype": type_name(qtype),
            "client": client,
        }
        with self._lock:
            self._recent.appendleft(item)
            if not domain:
                return
            domains = self._bucket_locked()["domains"]
            domains[domain] = int(domains.get(domain, 0)) + 1

    def snapshot(self) -> dict:
        with self._lock:
            data = dict(self._counters)
            recent = list(self._recent)
            days = {
                day: {
                    "queries": int(bucket["queries"]),
                    "blocked": int(bucket["blocked"]),
                    "domains": dict(bucket["domains"]),
                    "upstream_errors": _reason_map(bucket.get("upstream_errors")),
                }
                for day, bucket in self._days.items()
            }
            reasons = _reason_map(self._reasons)
        data["started_at"] = self.started_at
        data["uptime_seconds"] = max(0, int(time.time() - self.started_at))
        data["recent_blocked"] = recent
        data["upstream_errors"] = reasons
        data["days"] = days
        data["today"] = _today_document(days)
        queries = data["queries"]
        data["blocked_percent"] = round(100.0 * data["blocked"] / queries, 1) if queries else 0.0
        return data

    def save(self) -> None:
        if not self.path:
            return
        try:
            atomic_write(self.path, json.dumps(self.snapshot(), indent=2) + "\n")
            os.chmod(self.path, 0o644)
        except OSError:
            return

    def _bucket_locked(self) -> dict:
        day = utc_day()
        bucket = self._days.get(day)
        if bucket is None:
            bucket = {
                "queries": 0,
                "blocked": 0,
                "domains": {},
                "upstream_errors": _reason_map({}),
            }
            self._days[day] = bucket
        bucket.setdefault("upstream_errors", _reason_map({}))
        return bucket

    def _load_days(self) -> None:
        if not self.path:
            return
        try:
            raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            return
        days = raw.get("days") if isinstance(raw, dict) else None
        if not isinstance(days, dict):
            return
        loaded: dict[str, dict] = {}
        for day, bucket in days.items():
            if not isinstance(day, str) or not isinstance(bucket, dict):
                continue
            domains_raw = bucket.get("domains") if isinstance(bucket.get("domains"), dict) else {}
            domains: dict[str, int] = {}
            for name, count in domains_raw.items():
                try:
                    number = int(count)
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    domains[str(name)] = number
            try:
                queries = int(bucket.get("queries", 0))
                blocked = int(bucket.get("blocked", 0))
            except (TypeError, ValueError):
                continue
            loaded[day] = {
                "queries": queries,
                "blocked": blocked,
                "domains": domains,
                "upstream_errors": _reason_map(bucket.get("upstream_errors")),
            }
        self._days = loaded


def _reason_map(raw) -> dict[str, int]:
    found: dict[str, int] = {}
    if isinstance(raw, dict):
        for name, count in raw.items():
            if name not in UPSTREAM_REASONS:
                continue
            try:
                found[str(name)] = max(0, int(count))
            except (TypeError, ValueError):
                continue
    return {reason: found.get(reason, 0) for reason in UPSTREAM_REASONS}


def _today_document(days: dict) -> dict:
    from piadblock.report import daily_stats_document

    day = utc_day()
    bucket = days.get(day) or {
        "queries": 0,
        "blocked": 0,
        "domains": {},
        "upstream_errors": _reason_map({}),
    }
    domains = bucket.get("domains") if isinstance(bucket.get("domains"), dict) else {}
    return daily_stats_document(
        day,
        int(bucket.get("queries", 0)),
        int(bucket.get("blocked", 0)),
        {str(name): int(count) for name, count in domains.items()},
        _reason_map(bucket.get("upstream_errors")),
    )
