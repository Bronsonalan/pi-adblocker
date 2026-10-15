"""Daily blocked-domain counts are complete and survive a restart."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from piadblock.gitrepo import GitAccess, Repo
from piadblock.report import daily_stats_document, document_for_day
from piadblock.stats import Stats, utc_day
from piadblock.__main__ import sync_daily_stats


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "piadblock"
    env["GIT_AUTHOR_EMAIL"] = "piadblock@localhost"
    env["GIT_COMMITTER_NAME"] = "piadblock"
    env["GIT_COMMITTER_EMAIL"] = "piadblock@localhost"
    return env


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", f"safe.directory={cwd}", *args],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )


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
            self.assertEqual(snapshot["today"]["queries"], bucket["queries"])
            self.assertEqual(snapshot["today"]["blocked"], bucket["blocked"])
            self.assertEqual(snapshot["today"]["blocked_domains"], document["blocked_domains"])
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


class PublishedFileTests(unittest.TestCase):
    def test_pushed_file_matches_the_live_day_not_a_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            bare = tmp / "remote.git"
            pi = tmp / "pi"
            subprocess.run(
                ["git", "init", "--bare", "-b", "main", str(bare)],
                check=True,
                capture_output=True,
                text=True,
                env=_git_env(),
            )
            pi.mkdir()
            _git(pi, "init", "-b", "main")
            _git(pi, "config", "user.name", "piadblock")
            _git(pi, "config", "user.email", "piadblock@localhost")
            (pi / "blocklist.txt").write_text("doubleclick.net\n", encoding="utf-8")
            day = utc_day()
            stale = {
                "date": day,
                "queries": 597,
                "blocked": 0,
                "blocked_domains": {},
                "top_blocked": [],
            }
            (pi / "stats").mkdir()
            (pi / "stats" / f"{day}.json").write_text(json.dumps(stale, indent=2) + "\n", encoding="utf-8")
            _git(pi, "add", "-A")
            _git(pi, "commit", "-m", "seed")
            _git(pi, "remote", "add", "origin", str(bare))
            _git(pi, "push", "origin", "HEAD:main")

            stats_path = tmp / "stats.json"
            previous = {
                "queries": 0,
                "blocked": 0,
                "days": {
                    "2020-01-01": {"queries": 50, "blocked": 4, "domains": {"old.example": 4}},
                    day: {"queries": 597, "blocked": 0, "domains": {}},
                },
            }
            stats_path.write_text(json.dumps(previous), encoding="utf-8")
            stats = Stats(str(stats_path))
            for _ in range(3):
                stats.bump("queries")
            stats.bump("blocked")
            stats.note_blocked("doubleclick.net", 1, "10.0.0.8")
            stats.note_upstream("timeout")
            stats.note_upstream("servfail")
            stats.save()
            live = stats.snapshot()
            self.assertEqual(live["queries"], 3)
            self.assertEqual(live["blocked"], 1)
            self.assertEqual(live["today"]["queries"], 600)
            self.assertEqual(live["today"]["blocked"], 1)
            self.assertEqual(live["today"]["blocked_domains"], {"doubleclick.net": 1})
            self.assertEqual(live["today"]["upstream_errors"]["timeout"], 1)
            self.assertEqual(live["today"]["upstream_errors"]["servfail"], 1)
            self.assertNotEqual(live["today"]["queries"], live["queries"])

            repo = Repo(str(pi), GitAccess("piadblock", "", "missing", str(bare)))
            repo.retry_delays = (0, 0, 0)
            written, warning = sync_daily_stats(json.loads(stats_path.read_text(encoding="utf-8")), repo)
            self.assertEqual(warning, "", warning)
            self.assertIn(f"stats/{day}.json", written)

            seen = tmp / "seen"
            _git(tmp, "clone", str(bare), str(seen))
            pushed = json.loads((seen / "stats" / f"{day}.json").read_text(encoding="utf-8"))
            self.assertEqual(pushed, live["today"])
            self.assertEqual(pushed, document_for_day(live, day))
            self.assertEqual(pushed["queries"], 600)
            self.assertEqual(pushed["blocked"], 1)
            self.assertNotEqual(pushed["queries"], 597)
            self.assertNotEqual(pushed["blocked"], 0)
            older = json.loads((seen / "stats" / "2020-01-01.json").read_text(encoding="utf-8"))
            self.assertEqual(older["queries"], 50)
            self.assertEqual(older["blocked"], 4)


if __name__ == "__main__":
    unittest.main()
