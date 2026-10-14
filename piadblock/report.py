"""Daily stats and heartbeat documents written into the git checkout.

The daily file is ``stats/YYYY-MM-DD.json`` at the repo root (on the Pi,
``/opt/piadblock/stats/YYYY-MM-DD.json``). ``YYYY-MM-DD`` is the UTC day.
``blocked_domains`` is every blocked name that day and its count, not the
40-line recent sample. ``top_blocked`` is that same map, sorted by count
descending and then by domain. Counts survive a process restart because the
server reloads the day's map from its state file.

Schema::

    {
      "date": "YYYY-MM-DD",
      "queries": 0,
      "blocked": 0,
      "blocked_domains": {"doubleclick.net": 2},
      "top_blocked": [{"domain": "doubleclick.net", "count": 2}]
    }

``heartbeat.json`` at the repo root is overwritten each hour::

    {"timestamp": "YYYY-MM-DDTHH:MM:SSZ", "uptime_seconds": 0, "version": "0.1.0"}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


def daily_stats_document(day: str, queries: int, blocked: int, domains: dict[str, int]) -> dict:
    """Build one day's stats file. Every domain with a positive count is included."""
    cleaned: dict[str, int] = {}
    for name, count in domains.items():
        try:
            number = int(count)
        except (TypeError, ValueError):
            continue
        if number > 0 and str(name).strip():
            cleaned[str(name)] = number
    ordered = dict(sorted(cleaned.items()))
    top = [
        {"domain": name, "count": count}
        for name, count in sorted(ordered.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "date": day,
        "queries": int(queries),
        "blocked": int(blocked),
        "blocked_domains": ordered,
        "top_blocked": top,
    }


def heartbeat_document(when: datetime, uptime_seconds: int, version: str) -> dict:
    moment = when.astimezone(timezone.utc).replace(microsecond=0)
    return {
        "timestamp": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "uptime_seconds": max(0, int(uptime_seconds)),
        "version": version,
    }


def dump_document(document: dict) -> str:
    return json.dumps(document, indent=2) + "\n"
