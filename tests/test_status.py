"""Status page HTML and HTTP routes."""

from __future__ import annotations

import http.client
import json
import os
import time
import unittest
from unittest import mock

import helpers  # noqa: F401

from piadblock.crt import LookupFeed
from piadblock.stats import Stats
from piadblock.status import (
    StatusPage,
    crt_payload,
    format_recent_time,
    render_status_page,
    render_status_page_c,
)


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
        self.assertIn("SYSTEM_OVERVIEW", html)
        self.assertIn("TX_LOGS", html)
        self.assertIn('href="/c"', html)
        self.assertIn('fetch("/api/stats"', html)
        self.assertIn('fetch("/api/crt"', html)
        self.assertNotIn("ads.example.com", html)
        self.assertNotIn("1.1.1.1", html)
        self.assertNotIn("MOCK DATA", html)
        self.assertNotIn("12,847", html)

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
        self.assertEqual(body["upstream"], ["1.1.1.1"])
        page_bytes = _get(page.port, "/")
        self.assertIn(b"piadblock", page_bytes)
        self.assertIn(b"THEME_C", page_bytes)
        self.assertIn(b'href="/c"', page_bytes)
        self.assertIn(b"SYSTEM_OVERVIEW", page_bytes)
        self.assertNotIn(b'data-crt="1"', page_bytes)
        theme_c = _get(page.port, "/c")
        self.assertIn(b"TOP_BLOCKED", theme_c)
        self.assertIn(b"THEME_D", theme_c)
        self.assertIn(b'href="/"', theme_c)
        self.assertIn(b'fetch("/api/crt"', theme_c)
        self.assertNotIn(b'data-crt="1"', theme_c)
        missing = _status(page.port, "/nope")
        self.assertEqual(missing, 404)
        self.assertEqual(_status(page.port, "/d"), 404)
        self.assertEqual(_status(page.port, "/manifest.json"), 404)

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
        self.assertNotIn("blocklist_domains", body)
        self.assertIsInstance(body["lookups"][0]["ts"], float)

    def test_crt_routes_and_query_keep_the_json_apis(self):
        stats = Stats()
        stats.bump("queries", 4)
        stats.bump("blocked", 1)
        feed = LookupFeed()
        feed.note("OK", "10.0.0.2", "example.com", "A")
        feed.note("BLOCKED", "192.168.1.9", "doubleclick.net", "A")
        page = StatusPage("127.0.0.1", 0, stats, lambda: {"version": "0.1.0"}, feed)
        page.start()
        self.addCleanup(page.stop)

        crt = _get(page.port, "/crt")
        root = _get(page.port, "/")
        query_root = _get(page.port, "/?crt=1")
        query_c = _get(page.port, "/c?crt=1")
        self.assertNotEqual(crt, root)
        self.assertIn(b"SYSTEM_OVERVIEW", root)
        self.assertNotIn(b'data-crt="1"', root)
        self.assertEqual(query_root, crt)
        self.assertEqual(query_c, crt)
        self.assertEqual(_get(page.port, "/index.html?crt=1"), crt)
        self.assertTrue(crt.startswith(b"<!DOCTYPE html>"))
        self.assertIn(b'data-crt="1"', crt)
        self.assertIn(b'id="kpi-q"', crt)
        self.assertIn(b'id="kpi-b"', crt)
        self.assertIn(b'id="kpi-p"', crt)
        self.assertIn(b'id="status-word"', crt)
        self.assertIn(b'id="fs-btn"', crt)
        self.assertIn(b"FULLSCREEN", crt)
        self.assertIn(b'href="/crt/c"', crt)
        self.assertIn(b"THEME D", crt)
        self.assertIn(b"THEME C", crt)
        self.assertNotIn(b'id="tops"', crt)
        self.assertIn(b"requestFullscreen", crt)
        self.assertIn(b"CRT_ROWS = 6", crt)
        self.assertIn(b"5.5vh", crt)
        self.assertIn(b"font-weight: 800", crt)
        self.assertIn(b"direction: rtl", crt)
        self.assertIn(b'dir="rtl"', crt)
        self.assertIn(b"overflow: hidden", crt)
        self.assertIn(b'setInterval(load, 3000)', crt)
        self.assertIn(b'fetch("/api/stats"', crt)
        self.assertIn(b'fetch("/api/crt"', crt)
        self.assertNotIn(b"text-shadow", crt)
        self.assertNotIn(b"box-shadow", crt)
        self.assertNotIn(b"filter:", crt)
        self.assertNotIn(b"blur(", crt)
        self.assertNotIn(b"SYSTEM_OVERVIEW", crt)
        self.assertNotIn(b"manifest", crt)
        self.assertNotIn(b"doubleclick.net", crt)
        self.assertNotIn(b"MOCK DATA", crt)
        self.assertIn(b"TOP_BLOCKED", _get(page.port, "/c?crt=0"))
        self.assertNotIn(b'data-crt="1"', _get(page.port, "/c?crt=0"))

        stats_body = json.loads(_get(page.port, "/api/stats?crt=1").decode("utf-8"))
        self.assertEqual(stats_body["queries"], 4)
        self.assertEqual(stats_body["blocked"], 1)
        self.assertNotIn("lookups", stats_body)
        crt_body = json.loads(_get(page.port, "/api/crt?crt=1").decode("utf-8"))
        self.assertEqual(crt_body["queries"], 4)
        self.assertEqual(crt_body["blocked"], 1)
        self.assertEqual(crt_body["lookups"][0]["name"], "doubleclick.net")
        self.assertEqual(crt_body["lookups"][0]["status"], "BLOCKED")
        self.assertIsInstance(crt_body["lookups"][0]["ts"], float)

        hud = _get(page.port, "/crt/c")
        self.assertNotEqual(hud, crt)
        self.assertTrue(hud.startswith(b"<!DOCTYPE html>"))
        self.assertIn(b'data-crt="1"', hud)
        self.assertIn(b'data-theme="c"', hud)
        self.assertIn(b'id="tops"', hud)
        self.assertIn(b'class="topstrip"', hud)
        self.assertIn(b'href="/crt"', hud)
        self.assertIn(b'href="/crt/c"', hud)
        self.assertIn(b"THEME D", hud)
        self.assertIn(b"THEME C", hud)
        self.assertIn(b'id="fs-btn"', hud)
        self.assertIn(b"FULLSCREEN", hud)
        self.assertIn(b"requestFullscreen", hud)
        self.assertIn(b"5.5vh", hud)
        self.assertIn(b"font-weight: 800", hud)
        self.assertIn(b"overflow: hidden", hud)
        self.assertIn(b'setInterval(load, 3000)', hud)
        self.assertIn(b'fetch("/api/stats"', hud)
        self.assertIn(b'fetch("/api/crt"', hud)
        self.assertNotIn(b"text-shadow", hud)
        self.assertNotIn(b"box-shadow", hud)
        self.assertNotIn(b"filter:", hud)
        self.assertNotIn(b"blur(", hud)
        self.assertNotIn(b"SYSTEM_OVERVIEW", hud)
        self.assertNotIn(b"doubleclick.net", hud)
        plain = _get(page.port, "/c")
        self.assertIn(b"TOP_BLOCKED", plain)
        self.assertNotIn(b'data-crt="1"', plain)
        self.assertNotIn(b'href="/crt/c"', plain)
        self.assertNotIn(b'href="/crt/c"', root)


class LocalTimeTests(unittest.TestCase):
    def setUp(self):
        self._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(self._restore_tz)

    def _restore_tz(self):
        if self._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._tz
        time.tzset()

    def test_page_clock_is_local_12_hour_and_the_api_stays_raw(self):
        epoch = 14 * 3600 + 31 * 60 + 5
        self.assertEqual(format_recent_time(epoch), "2:31:05 PM")
        self.assertEqual(format_recent_time(0), "12:00:00 AM")
        self.assertEqual(format_recent_time(12 * 3600), "12:00:00 PM")
        self.assertEqual(format_recent_time("not-a-time"), "")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        snapshot = {
            "queries": 1,
            "blocked": 1,
            "blocked_percent": 100.0,
            "cached": 0,
            "forwarded": 0,
            "errors": 0,
            "uptime_seconds": 1,
            "recent_blocked": [
                {"ts": 1792099091, "name": "ads.example.com", "qtype": "A", "client": "192.168.1.5"}
            ],
            "today": {"date": "2026-10-15", "queries": 1, "blocked": 1},
        }
        details = {
            "version": "0.1.0",
            "port": 53,
            "upstream": ["1.1.1.1"],
            "blocklist_domains": 91526,
            "allow_count": 0,
            "deny_count": 91526,
            "cache_entries": 0,
        }
        html = render_status_page(snapshot, details).decode("utf-8")
        theme_c = render_status_page_c(snapshot, details).decode("utf-8")
        self.assertNotIn("1792099091", html)
        self.assertNotIn("1792099091", theme_c)
        self.assertIn("blocklist_domains", html)
        self.assertIn("top_blocked", theme_c)
        self.assertNotIn("MOCK DATA", html)
        self.assertNotIn("172.20.1.217", html)
        self.assertNotIn("+8.4%", html)
        self.assertIn("setInterval(load, 3000)", html)
        self.assertNotIn("/*__SCRIPT__*/", html)

        stats = Stats()
        with mock.patch("piadblock.stats.time.time", return_value=1792099091.0):
            stats.note_blocked("ads.example.com", 1, "192.168.1.5")
        page = StatusPage("127.0.0.1", 0, stats, lambda: details)
        page.start()
        self.addCleanup(page.stop)
        body = json.loads(_get(page.port, "/api/stats").decode("utf-8"))
        self.assertEqual(body["recent_blocked"][0]["ts"], 1792099091)
        self.assertIsInstance(body["recent_blocked"][0]["ts"], float)
        self.assertEqual(body["blocklist_domains"], 91526)
        page_bytes = _get(page.port, "/")
        self.assertNotIn(b"1792099091", page_bytes)
        self.assertIn(b"blocklist_domains", page_bytes)
        self.assertIn(b"THEME_C", page_bytes)

    def test_los_angeles_clock_follows_the_system_zone(self):
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        stamp = time.mktime((2026, 10, 15, 14, 31, 5, 0, 0, -1))
        self.assertEqual(format_recent_time(stamp), "2:31:05 PM")

    def test_crt_payload_carries_the_list_size_and_a_numeric_timestamp(self):
        feed = LookupFeed()
        with mock.patch("piadblock.crt.time.time", return_value=1792099091.0):
            feed.note("BLOCKED", "10.0.0.1", "ads.example", "A")
        payload = crt_payload(
            {"today": {"date": "2026-10-15", "queries": 4, "blocked": 1}},
            feed,
            {"blocklist_domains": 91526, "allow_count": 0},
        )
        self.assertEqual(payload["blocklist_domains"], 91526)
        self.assertEqual(payload["allow_count"], 0)
        self.assertEqual(payload["lookups"][0]["ts"], 1792099091)
        bare = crt_payload({"today": {"queries": 1, "blocked": 0}}, None, {"version": "0.1.0"})
        self.assertNotIn("blocklist_domains", bare)


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
