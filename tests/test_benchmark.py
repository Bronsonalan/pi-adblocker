"""Smoke test for tools/benchmark.py. Uses a tiny list, never port 53."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

_SPEC = importlib.util.spec_from_file_location(
    "piadblock_benchmark",
    helpers.ROOT / "tools" / "benchmark.py",
)
assert _SPEC is not None and _SPEC.loader is not None
bench = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bench)

SCRIPT = helpers.ROOT / "tools" / "benchmark.py"


class BenchmarkMathTests(unittest.TestCase):
    def test_percentile_and_recommendation(self):
        self.assertEqual(bench.percentile([10.0], 95), 10.0)
        self.assertEqual(bench.percentile([0.0, 10.0], 50), 5.0)
        rows = [
            {"domains": 100, "timeouts": 0, "queries": 5, "p95_ms": 10.0, "peak_mb": 20.0},
            {"domains": 200, "timeouts": 0, "queries": 5, "p95_ms": 80.0, "peak_mb": 20.0},
            {"domains": 300, "timeouts": 1, "queries": 5, "p95_ms": 10.0, "peak_mb": 20.0},
            {"domains": 400, "timeouts": 0, "queries": 5, "p95_ms": 10.0, "peak_mb": 900.0},
        ]
        chosen = bench.choose_max(rows, budget_mb=512, p95_limit_ms=50)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["domains"], 100)
        self.assertIsNone(bench.choose_max(rows[1:], budget_mb=512, p95_limit_ms=50))


class BenchmarkSmokeTests(unittest.TestCase):
    def test_port_53_is_refused(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--port", "53", "--sizes", "10"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("53", completed.stderr)

    def test_tiny_run_prints_a_table_and_removes_temp_files(self):
        before = set(Path(tempfile.gettempdir()).glob("piadblock-bench-*"))
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "--sizes", "400", "--port", "0"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        after = set(Path(tempfile.gettempdir()).glob("piadblock-bench-*"))
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertEqual(before, after)
        text = completed.stdout
        self.assertIn("piadblock capacity benchmark", text)
        self.assertIn("host:", text)
        self.assertIn("recommended max list size:", text)
        self.assertIn("port 53 is not used", text)
        self.assertIn("400", text)
        self.assertIn("p50_ms", text)
        self.assertIn("fwd_p50", text)
        self.assertIn("peak_mb", text)
        row = bench.measure(300, 0)
        self.assertEqual(row["domains"], 300)
        self.assertGreaterEqual(row["queries"], 1)
        self.assertEqual(row["timeouts"], 0)
        self.assertNotEqual(row["port"], 53)
        self.assertGreater(row["port"], 1023)
        self.assertLess(row["p95_ms"], 1000)
        self.assertLess(row["forward_p50_ms"], 10.0)
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("piadblock-bench-*")), before)


if __name__ == "__main__":
    unittest.main()
