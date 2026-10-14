"""Daily blocked-domain counts are complete and survive a restart."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from piadblock.report import daily_stats_document
from piadblock.stats import Stats, utc_day


class DailyCountTests(unittest.TestCase):
    def test_full_day_counts_survive_restart_and_are_not_capped_at_40(self):
        with tempfile.TemporaryDirectory() as raw:
            path = str(Path(raw) / "stats.json")
            stats = Stats(path)
            for index in range(45):
                stats.bump("queries")
                stats.bump("blocked")
                stats.note_blocked(f"ad{index}.example", 1, "127.0.0.1")
            stats.save()

            again = Stats(path)
            again.bump("queries")
            again.bump("blocked")
            again.note_blocked("ad0.example", 1, "127.0.0.1")
            snapshot = again.snapshot()
            day = utc_day()
            bucket = snapshot["days"][day]
            self.assertEqual(bucket["queries"], 46)
            self.assertEqual(bucket["blocked"], 46)
            self.assertEqual(len(bucket["domains"]), 45)
            self.assertEqual(bucket["domains"]["ad0.example"], 2)
            self.assertLessEqual(len(snapshot["recent_blocked"]), 40)

            document = daily_stats_document(day, bucket["queries"], bucket["blocked"], bucket["domains"])
            self.assertEqual(len(document["blocked_domains"]), 45)
            self.assertEqual(len(document["top_blocked"]), 45)
            self.assertEqual(document["top_blocked"][0], {"domain": "ad0.example", "count": 2})
            self.assertNotIn("recent_blocked", document)
            self.assertEqual(
                document["top_blocked"],
                [
                    {"domain": name, "count": count}
                    for name, count in sorted(
                        document["blocked_domains"].items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                ],
            )

    def test_top_blocked_is_the_full_map_sorted_by_count_then_name(self):
        document = daily_stats_document(
            "2026-10-14",
            5,
            5,
            {"b.example": 1, "a.example": 3, "zero.example": 0, "c.example": 1},
        )
        self.assertEqual(
            document,
            {
                "date": "2026-10-14",
                "queries": 5,
                "blocked": 5,
                "blocked_domains": {"a.example": 3, "b.example": 1, "c.example": 1},
                "top_blocked": [
                    {"domain": "a.example", "count": 3},
                    {"domain": "b.example", "count": 1},
                    {"domain": "c.example", "count": 1},
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
