"""Local status-page demo: same routes as the Pi, fake live data, no DNS."""

from __future__ import annotations

import http.client
import json
import time
import unittest

import helpers  # noqa: F401

from piadblock.demo import blocklist_size, start_demo


class DemoTests(unittest.TestCase):
    def test_blocklist_size_reads_repo_file(self):
        count = blocklist_size()
        self.assertGreater(count, 1000)

    def test_demo_serves_pages_and_changing_apis(self):
        page = start_demo("127.0.0.1", 0)
        self.addCleanup(page.stop)
        self.addCleanup(lambda: getattr(page, "_demo_stop", None) and page._demo_stop.set())

        for path in ("/", "/c", "/crt", "/crt/c"):
            status, body = _get(page.port, path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body.startswith(b"<!DOCTYPE html>"), path)
            self.assertIn(b'fetch("/api/stats"', body)
            self.assertIn(b'fetch("/api/crt"', body)

        status, stats_raw = _get(page.port, "/api/stats")
        self.assertEqual(status, 200)
        stats = json.loads(stats_raw.decode("utf-8"))
        self.assertGreater(stats["queries"], 0)
        self.assertGreater(stats["blocked"], 0)
        self.assertIn("today", stats)
        self.assertIn("top_blocked", stats["today"])
        self.assertTrue(stats["today"]["top_blocked"])
        self.assertIn("days", stats)
        self.assertGreaterEqual(len(stats["days"]), 2)
        self.assertIn("blocklist_domains", stats)
        self.assertGreater(stats["blocklist_domains"], 0)
        self.assertIn("uptime_seconds", stats)
        self.assertIn("version", stats)

        status, crt_raw = _get(page.port, "/api/crt")
        self.assertEqual(status, 200)
        crt = json.loads(crt_raw.decode("utf-8"))
        self.assertGreater(crt["queries"], 0)
        self.assertGreater(crt["blocked"], 0)
        self.assertTrue(crt["lookups"])
        self.assertIn(crt["lookups"][0]["status"], ("OK", "BLOCKED", "FAIL"))
        self.assertIn("name", crt["lookups"][0])
        self.assertIn("ts", crt["lookups"][0])
        self.assertIn("blocklist_domains", crt)

        time.sleep(1.6)
        _, stats2_raw = _get(page.port, "/api/stats")
        stats2 = json.loads(stats2_raw.decode("utf-8"))
        _, crt2_raw = _get(page.port, "/api/crt")
        crt2 = json.loads(crt2_raw.decode("utf-8"))
        self.assertGreaterEqual(stats2["queries"], stats["queries"])
        self.assertNotEqual(
            [row.get("ts") for row in crt["lookups"][:5]],
            [row.get("ts") for row in crt2["lookups"][:5]],
        )


def _get(port: int, path: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
