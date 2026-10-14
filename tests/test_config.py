"""JSON config defaults and validation."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import helpers  # noqa: F401

from piadblock.config import Config, ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


class ConfigTests(unittest.TestCase):
    def test_example_file_matches_defaults(self):
        loaded = load_config(str(ROOT / "config.example.json"))
        self.assertEqual(loaded.to_public_dict(), Config().to_public_dict())
        self.assertEqual(loaded.unknown_keys, [])

    def test_partial_override(self):
        path = self._write({"port": 5353, "upstream": "8.8.8.8", "tcp": False})
        loaded = load_config(str(path))
        self.assertEqual(loaded.port, 5353)
        self.assertEqual(loaded.upstream, ["8.8.8.8"])
        self.assertFalse(loaded.tcp)
        self.assertEqual(loaded.block_ipv4, "0.0.0.0")
        self.assertEqual(loaded.cache_size, 2048)
        self.assertEqual(loaded.blocklists, [])
        self.assertFalse(loaded.auto_update)

    def test_integral_float_port(self):
        path = self._write({"port": 5353.0})
        self.assertEqual(load_config(str(path)).port, 5353)

    def test_unknown_key_is_ignored(self):
        path = self._write({"port": 5353, "nope": 1})
        loaded = load_config(str(path))
        self.assertEqual(loaded.port, 5353)
        self.assertEqual(loaded.unknown_keys, ["nope"])

    def test_invalid_json(self):
        path = ROOT / "tests" / "_bad.json"
        # Keep the bad file out of the repo root listing by using a temp dir.
        self.addCleanup(lambda: None)
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            file_path = Path(tmp) / "config.json"
            file_path.write_text("{", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(str(file_path))

    def test_rejects_bad_values(self):
        cases = [
            {"port": "fifty"},
            {"port": True},
            {"upstream": []},
            {"upstream": ["not-an-ip"]},
            {"blocklists": ["ftp://example.com/hosts"]},
            {"block_ipv4": "::"},
            {"block_ipv6": "0.0.0.0"},
            {"cache_min_ttl": 100, "cache_max_ttl": 10},
            {"log_level": "loud"},
        ]
        for case in cases:
            with self.subTest(case=case):
                path = self._write(case)
                with self.assertRaises(ConfigError):
                    load_config(str(path))

    def test_empty_blocklists_are_allowed(self):
        path = self._write({"blocklists": []})
        self.assertEqual(load_config(str(path)).blocklists, [])

    def _write(self, payload: dict) -> Path:
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
