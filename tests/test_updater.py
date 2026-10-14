"""Blocklist download and merge."""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import helpers  # noqa: F401

from piadblock.config import Config
from piadblock.updater import update_blocklists

HOSTS = """# comment
127.0.0.1 localhost
0.0.0.0 ads.example.com
0.0.0.0 shared.example.com
"""
ADBLOCK = """||shared.example.com^
||other.example.net^$third-party
@@||good.example.com^
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = {"/hosts": HOSTS, "/adblock": ADBLOCK}.get(self.path)
        if body is None:
            self.send_response(500)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        return


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)

    def _config(self, directory: Path, urls: list[str]) -> Config:
        return Config(
            blocklists=urls,
            blocklist_dir=str(directory),
            allowlist=str(directory / "allow.txt"),
            denylist=str(directory / "deny.txt"),
            log_file="",
            stats_file="",
            auto_update=False,
            download_timeout=2,
        )

    def test_empty_url_list_does_not_download(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config = self._config(directory, [])
            result = update_blocklists(config, force=True)
            self.assertTrue(result.ok)
            self.assertTrue(result.skipped)
            self.assertEqual(result.sources, [])
            self.assertFalse((directory / "merged.txt").exists())

    def test_merge_dedupes_hosts_and_adblock(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            base = f"http://127.0.0.1:{self.port}"
            config = self._config(directory, [f"{base}/hosts", f"{base}/adblock"])
            result = update_blocklists(config, force=True)
            self.assertTrue(result.ok)
            self.assertEqual(result.domain_count, 3)
            text = (directory / "merged.txt").read_text(encoding="utf-8")
            self.assertIn("ads.example.com", text)
            self.assertIn("shared.example.com", text)
            self.assertIn("other.example.net", text)
            self.assertNotIn("good.example.com", text)
            self.assertNotIn("localhost", text)
            self.assertEqual(text.count("shared.example.com"), 1)
            meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["domain_count"], 3)
            self.assertTrue(meta["ok"])

    def test_failed_download_keeps_existing_list(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            merged = directory / "merged.txt"
            merged.write_text("# old\nkeep.example.com\n", encoding="utf-8")
            config = self._config(directory, ["http://127.0.0.1:1/missing"])
            result = update_blocklists(config, force=True)
            self.assertFalse(result.ok)
            self.assertTrue(result.kept_existing)
            self.assertIn("keep.example.com", merged.read_text(encoding="utf-8"))

    def test_if_due_skips_a_fresh_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            merged = directory / "merged.txt"
            merged.write_text("keep.example.com\n", encoding="utf-8")
            (directory / "meta.json").write_text(json.dumps({"domain_count": 1}), encoding="utf-8")
            base = f"http://127.0.0.1:{self.port}"
            config = self._config(directory, [f"{base}/hosts"])
            result = update_blocklists(config, force=False)
            self.assertTrue(result.skipped)
            self.assertEqual(result.domain_count, 1)
            self.assertNotIn("ads.example.com", merged.read_text(encoding="utf-8"))

    def test_stale_file_is_replaced(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            merged = directory / "merged.txt"
            merged.write_text("old.example.com\n", encoding="utf-8")
            old = time.time() - 48 * 3600
            import os

            os.utime(merged, (old, old))
            base = f"http://127.0.0.1:{self.port}"
            config = self._config(directory, [f"{base}/hosts"])
            config.update_interval_hours = 24
            result = update_blocklists(config, force=False)
            self.assertTrue(result.ok)
            self.assertFalse(result.skipped)
            self.assertIn("ads.example.com", merged.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
