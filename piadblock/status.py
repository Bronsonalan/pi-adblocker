"""Tiny status page served with ``http.server``."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from typing import Callable
from urllib.parse import urlparse

from piadblock.crt import LookupFeed, format_local_time
from piadblock.stats import Stats


def format_count(value: object) -> str:
    """Thousands separators for a count shown on the page."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = 0
    return f"{number:,}"


def format_recent_time(ts: object) -> str:
    """Page clock for one blocked query. A bad timestamp is blank, not the raw epoch."""
    try:
        return format_local_time(float(ts))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _reason_line(snapshot: dict) -> str:
    reasons = snapshot.get("upstream_errors") if isinstance(snapshot.get("upstream_errors"), dict) else {}
    names = ("timeout", "servfail", "refused", "socket", "truncation")
    return ", ".join(f"{name} {int(reasons.get(name, 0) or 0)}" for name in names)


def render_status_page(snapshot: dict, details: dict) -> bytes:
    """HTML for ``/``. Numbers come from the live stats snapshot."""
    queries = snapshot.get("queries", 0)
    blocked = snapshot.get("blocked", 0)
    percent = snapshot.get("blocked_percent", 0)
    today = snapshot.get("today") if isinstance(snapshot.get("today"), dict) else {}
    recent = snapshot.get("recent_blocked") or []
    rows = []
    for item in recent:
        rows.append(
            "<tr><td>{ts}</td><td>{name}</td><td>{qtype}</td><td>{client}</td></tr>".format(
                ts=escape(format_recent_time(item.get("ts"))),
                name=escape(str(item.get("name", ""))),
                qtype=escape(str(item.get("qtype", ""))),
                client=escape(str(item.get("client", ""))),
            )
        )
    recent_html = (
        "<table><thead><tr><th>Time</th><th>Domain</th><th>Type</th><th>Client</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        if rows
        else "<p>No blocked queries yet.</p>"
    )
    upstream = ", ".join(escape(str(item)) for item in details.get("upstream") or [])
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>piadblock</title>
<style>
  body {{ font-family: sans-serif; margin: 2rem; color: #1c1c1c; background: #f7f6f3; }}
  h1 {{ font-size: 1.4rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.8rem; }}
  .card {{ background: white; border: 1px solid #e2e0da; border-radius: 8px; padding: 0.8rem 1rem; }}
  .num {{ font-size: 1.6rem; font-weight: 650; }}
  .label {{ color: #666; font-size: 0.85rem; }}
  table {{ border-collapse: collapse; width: 100%; background: white; }}
  th, td {{ text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid #eee; font-size: 0.92rem; }}
  code {{ background: #eee; padding: 0.1rem 0.3rem; }}
</style>
</head>
<body>
<h1>piadblock <span class="label">{escape(str(details.get("version", "")))}</span></h1>
<p>Listening on port <code>{escape(str(details.get("port", "")))}</code>.
Upstream: <code>{upstream or "none"}</code>.
Blocklist: <code>{format_count(details.get("blocklist_domains", 0))}</code> domains (allowlist {format_count(details.get("allow_count", 0))}).
Cache entries: <code>{details.get("cache_entries", 0)}</code>.</p>
<div class="grid">
  <div class="card"><div class="num">{queries}</div><div class="label">Queries since start</div></div>
  <div class="card"><div class="num">{blocked}</div><div class="label">Blocked since start ({percent}%)</div></div>
  <div class="card"><div class="num">{snapshot.get("cached", 0)}</div><div class="label">Cache hits</div></div>
  <div class="card"><div class="num">{snapshot.get("forwarded", 0)}</div><div class="label">Forwarded</div></div>
  <div class="card"><div class="num">{snapshot.get("errors", 0)}</div><div class="label">Errors</div></div>
  <div class="card"><div class="num">{snapshot.get("uptime_seconds", 0)}s</div><div class="label">Uptime</div></div>
</div>
<p>Today ({escape(str(today.get("date", "")))} UTC): <strong>{int(today.get("queries", 0) or 0)}</strong> queries,
<strong>{int(today.get("blocked", 0) or 0)}</strong> blocked.
This is the UTC day written to <code>stats/{escape(str(today.get("date", "")))}.json</code>.</p>
<p class="label">Upstream failures since start: {escape(_reason_line(snapshot))}.</p>
<h2>Recently blocked</h2>
{recent_html}
<p class="label">JSON: <a href="/api/stats">/api/stats</a></p>
</body>
</html>
"""
    return page.encode("utf-8")


def crt_payload(snapshot: dict, lookups: LookupFeed | None, details: dict | None = None) -> dict:
    """Today's totals plus the newest lookups. The CRT polls this.

    ``blocklist_domains`` is the effective list size from the server details.
    Lookup timestamps stay numeric here; the display formats them in local time.
    """
    today = snapshot.get("today") if isinstance(snapshot.get("today"), dict) else {}
    try:
        queries = int(today.get("queries", 0) or 0)
        blocked = int(today.get("blocked", 0) or 0)
    except (TypeError, ValueError):
        queries, blocked = 0, 0
    payload = {
        "date": str(today.get("date", "")),
        "queries": queries,
        "blocked": blocked,
        "lookups": lookups.snapshot() if lookups is not None else [],
    }
    if isinstance(details, dict) and "blocklist_domains" in details:
        try:
            payload["blocklist_domains"] = int(details.get("blocklist_domains") or 0)
            payload["allow_count"] = int(details.get("allow_count") or 0)
        except (TypeError, ValueError):
            pass
    return payload


def make_handler(
    stats: Stats,
    details_fn: Callable[[], dict],
    lookups: LookupFeed | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
            path = urlparse(self.path).path
            details = details_fn()
            snapshot = stats.snapshot()
            if path in ("/", "/index.html"):
                body = render_status_page(snapshot, details)
                content_type = "text/html; charset=utf-8"
            elif path == "/api/stats":
                payload = dict(snapshot)
                payload.update(details)
                body = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
                content_type = "application/json"
            elif path == "/api/crt":
                body = (json.dumps(crt_payload(snapshot, lookups, details)) + "\n").encode("utf-8")
                content_type = "application/json"
            elif path == "/health":
                body = b"ok\n"
                content_type = "text/plain; charset=utf-8"
            else:
                body = b"not found\n"
                self._send(404, "text/plain; charset=utf-8", body)
                return
            self._send(200, content_type, body)

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


class StatusPage:
    """Background HTTP server. Failure to bind is reported and ignored by the DNS server."""

    def __init__(
        self,
        host: str,
        port: int,
        stats: Stats,
        details_fn: Callable[[], dict],
        lookups: LookupFeed | None = None,
    ):
        self.host = host
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        handler = make_handler(stats, details_fn, lookups)
        self._handler = handler

    def start(self) -> None:
        self.httpd = ThreadingHTTPServer((self.host, self.port), self._handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="piadblock-status", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
