"""Status page HTML and HTTP routes."""

from __future__ import annotations

import http.client
import json
import unittest

import helpers  # noqa: F401

from piadblock.crt import LookupFeed
from piadblock.stats import Stats
from piadblock.status import StatusPage, render_status_page


class StatusTests(unittest.TestCase):
    def test_render_includes_counters(self):
        snapshot = {
            "queries": 10,
            "blocked": 4,
            "blocked_percent": 40.0,
            "cached": 3,
            "forwarded": 3,
            "errors": 0,
            "uptime_seconds": 12,
            "recent_blocked": [
                {"ts": 1, "name": "ads.example.com", "qtype": "A", "client": "192.168.1.5"}
            ],
        }
        details = {
            "version": "0.1.0",
            "port": 53,
            "upstream": ["1.1.1.1"],
            "blocklist_domains": 100,
            "allow_count": 1,
            "deny_count": 2,
            "cache_entries": 3,
        }
        html = render_status_page(snapshot, details).decode("utf-8")
        self.assertIn("10", html)
        self.assertIn("ads.example.com", html)
        self.assertIn("1.1.1.1", html)

    def test_http_routes(self):
        stats = Stats()
        stats.bump("queries", 4)
        stats.bump("blocked", 1)
        page = StatusPage(
            "127.0.0.1",
            0,
            stats,
            lambda: {
                "version": "0.1.0",
                "upstream": ["1.1.1.1"],
                "port": 53,
                "blocklist_domains": 3,
                "allow_count": 0,
                "deny_count": 0,
                "cache_entries": 0,
            },
        )
        page.start()
        self.addCleanup(page.stop)
        self.assertEqual(_get(page.port, "/health"), b"ok\n")
        body = json.loads(_get(page.port, "/api/stats").decode("utf-8"))
        self.assertEqual(body["queries"], 4)
        self.assertEqual(body["blocked"], 1)
        self.assertEqual(body["blocklist_domains"], 3)
        page_bytes = _get(page.port, "/")
        self.assertIn(b"piadblock", page_bytes)
        missing = _status(page.port, "/nope")
        self.assertEqual(missing, 404)

    def test_crt_feed_is_today_plus_newest_lookup(self):
        stats = Stats()
        stats.bump("queries", 4)
        stats.bump("blocked", 1)
        feed = LookupFeed()
        feed.note("OK", "10.0.0.2", "example.com", "A")
        feed.note("BLOCKED", "192.168.1.9", "doubleclick.net", "A")
        page = StatusPage("127.0.0.1", 0, stats, lambda: {"version": "0.1.0"}, feed)
        page.start()
        self.addCleanup(page.stop)
        body = json.loads(_get(page.port, "/api/crt").decode("utf-8"))
        self.assertEqual(body["queries"], 4)
        self.assertEqual(body["blocked"], 1)
        self.assertEqual(body["lookups"][0]["name"], "doubleclick.net")
        self.assertEqual(body["lookups"][0]["status"], "BLOCKED")
        self.assertEqual(body["lookups"][0]["client"], "192.168.1.9")


def _get(port: int, path: str) -> bytes:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    conn.request("GET", path)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return body


def _status(port: int, path: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    conn.request("GET", path)
    response = conn.getresponse()
    response.read()
    status = response.status
    conn.close()
    return status


if __name__ == "__main__":
    unittest.main()
