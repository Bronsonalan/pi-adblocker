"""Hosts files, Adblock rules, and allow/deny decisions."""

from __future__ import annotations

import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

import helpers  # noqa: F401

from piadblock.blocklist import (
    Matcher,
    load_domain_file,
    parse_domain_list,
    parse_domain_list_report,
)

SAMPLE = """
# Title: StevenBlack/hosts
127.0.0.1 localhost
127.0.0.1 localhost.localdomain
255.255.255.255 broadcasthost
::1 localhost
fe80::1%lo0 localhost
0.0.0.0 0.0.0.0

# a real block, plus a second name and an inline comment
0.0.0.0 ads.example.com extra.example.com # sponsored
0.0.0.0 DoubleClick.net

||tracker.example.net^
||tracker.example.net^$third-party
||cdn.example.net/banner.js^
@@||good.example.net^
*.wild.example.org

not a domain
münchen.test
"""


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.domains = parse_domain_list(SAMPLE, skip_never_block=True)

    def test_hosts_and_adblock_rules(self):
        self.assertIn("ads.example.com", self.domains)
        self.assertIn("extra.example.com", self.domains)
        self.assertIn("doubleclick.net", self.domains)
        self.assertIn("tracker.example.net", self.domains)
        self.assertIn("wild.example.org", self.domains)
        self.assertIn("xn--mnchen-3ya.test", self.domains)

    def test_skips_localhost_exceptions_and_paths(self):
        self.assertNotIn("localhost", self.domains)
        self.assertNotIn("localhost.localdomain", self.domains)
        self.assertNotIn("broadcasthost", self.domains)
        self.assertNotIn("good.example.net", self.domains)
        self.assertNotIn("cdn.example.net", self.domains)
        self.assertNotIn("0.0.0.0", self.domains)

    def test_single_label_allowed_for_user_lists(self):
        domains = parse_domain_list("localhost\n# comment\nads.example.com\n", allow_single_label=True)
        self.assertEqual(domains, {"localhost", "ads.example.com"})


class DecideTests(unittest.TestCase):
    def matcher(self, blocked, allowed=(), denied=()):
        return Matcher(set(blocked), set(allowed), set(denied))

    def test_exact_and_subdomain_block(self):
        matcher = self.matcher(["ads.example.com"])
        self.assertTrue(matcher.is_blocked("ads.example.com"))
        self.assertTrue(matcher.is_blocked("A.ADS.example.com."))
        self.assertFalse(matcher.is_blocked("example.com"))
        self.assertFalse(matcher.is_blocked("notads.example.com"))

    def test_allow_beats_same_name_block(self):
        matcher = self.matcher(["example.com"], allowed=["example.com"])
        decision = matcher.decide("example.com")
        self.assertFalse(decision.blocked)
        self.assertIn("allowlist", decision.reason)
        self.assertFalse(matcher.is_blocked("www.example.com"))

    def test_specific_deny_beats_broader_allow(self):
        matcher = self.matcher([], allowed=["good.com"], denied=["ads.good.com"])
        self.assertTrue(matcher.is_blocked("ads.good.com"))
        self.assertTrue(matcher.is_blocked("x.ads.good.com"))
        self.assertFalse(matcher.is_blocked("good.com"))
        self.assertFalse(matcher.is_blocked("www.good.com"))

    def test_specific_allow_beats_broader_block(self):
        matcher = self.matcher(["example.com"], allowed=["news.example.com"])
        self.assertFalse(matcher.is_blocked("news.example.com"))
        self.assertFalse(matcher.is_blocked("a.news.example.com"))
        self.assertTrue(matcher.is_blocked("example.com"))
        self.assertTrue(matcher.is_blocked("ads.example.com"))

    def test_not_a_domain(self):
        decision = self.matcher(["example.com"]).decide("not a domain")
        self.assertFalse(decision.blocked)
        self.assertEqual(decision.reason, "not a domain")

    def test_blocklist_count_is_the_union_of_download_and_deny(self):
        matcher = self.matcher(["a.com", "b.com"], denied=["b.com", "c.com"])
        self.assertEqual(matcher.blocked_count, 2)
        self.assertEqual(matcher.deny_count, 2)
        self.assertEqual(matcher.blocklist_count, 3)

    def test_blocklist_count_follows_the_deny_file_when_nothing_is_downloaded(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "merged.txt").write_text("", encoding="utf-8")
            (root / "allow.txt").write_text("safe.example\n", encoding="utf-8")
            (root / "deny.txt").write_text("ads.example\ntrack.example\n", encoding="utf-8")
            report = Matcher.prepare(str(root / "merged.txt"), str(root / "allow.txt"), str(root / "deny.txt"))
        self.assertEqual(report.error, "")
        self.assertEqual(report.matcher.blocked_count, 0)
        self.assertEqual(report.matcher.allow_count, 1)
        self.assertEqual(report.matcher.deny_count, 2)
        self.assertEqual(report.matcher.blocklist_count, 2)


class DefaultDenylistTests(unittest.TestCase):
    def test_shipped_blocklist_domains(self):
        path = Path(__file__).resolve().parents[1] / "blocklist.txt"
        text = path.read_text(encoding="utf-8")
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertGreater(len(lines), 80000)
        raw = parse_domain_list(text, allow_single_label=True)
        self.assertGreater(len(raw), 80000)
        for name in (
            "doubleclick.net",
            "googlesyndication.com",
            "mask.icloud.com",
            "mask-h2.icloud.com",
            "gvt2.com",
            "dfirma.pl",
            "dkonto.pl",
            "reddit.com",
            "redd.it",
            "redditstatic.com",
            "redditmedia.com",
            "reddituploads.com",
            "redditinc.com",
            "reddit.app.link",
        ):
            self.assertIn(name, raw)
        for parent in (
            "google.com",
            "gstatic.com",
            "googleapis.com",
            "apple.com",
            "icloud.com",
            "github.com",
            "cloudflare.com",
            "microsoft.com",
            "windows.com",
            "windowsupdate.com",
            "com",
            "net",
            "org",
            "local",
            "lan",
            "arpa",
        ):
            self.assertNotIn(parent, raw)

        started = time.perf_counter()
        report = Matcher.prepare("", "", str(path))
        elapsed = time.perf_counter() - started
        self.assertEqual(report.error, "")
        self.assertLess(elapsed, 3.0)
        self.assertGreater(report.skipped, 0)
        matcher = report.matcher
        self.assertGreater(matcher.deny_count, 80000)
        self.assertNotIn("dfirma.pl", matcher.denied)
        self.assertNotIn("dkonto.pl", matcher.denied)
        self.assertIn("gvt2.com", matcher.denied)
        self.assertTrue(matcher.is_blocked("doubleclick.net"))
        self.assertTrue(matcher.is_blocked("ads.doubleclick.net"))
        self.assertTrue(matcher.is_blocked("reddit.com"))
        self.assertTrue(matcher.is_blocked("www.reddit.com"))
        self.assertTrue(matcher.is_blocked("redd.it"))
        self.assertTrue(matcher.is_blocked("reddit.app.link"))
        self.assertTrue(matcher.is_blocked("pagead2.googlesyndication.com"))
        self.assertTrue(matcher.is_blocked("mask.icloud.com"))
        self.assertTrue(matcher.is_blocked("mask-h2.icloud.com"))
        self.assertTrue(matcher.is_blocked("a.mask.icloud.com"))
        self.assertFalse(matcher.is_blocked("example.com"))
        self.assertFalse(matcher.is_blocked("google.com"))
        self.assertFalse(matcher.is_blocked("www.google.com"))
        self.assertFalse(matcher.is_blocked("gvt2.com"))
        self.assertFalse(matcher.is_blocked("play.gvt2.com"))
        self.assertFalse(matcher.is_blocked("dfirma.pl"))
        self.assertFalse(matcher.is_blocked("apple.com"))
        self.assertFalse(matcher.is_blocked("icloud.com"))
        self.assertFalse(matcher.is_blocked("www.icloud.com"))
        self.assertFalse(matcher.is_blocked("github.com"))
        self.assertFalse(matcher.is_blocked("cloudflare.com"))
        self.assertFalse(matcher.is_blocked("microsoft.com"))
        self.assertFalse(matcher.is_blocked("ads.google.com"))
        self.assertFalse(matcher.is_blocked("1130867761-doradztwo.storage.googleapis.com"))
        self.assertTrue(matcher.is_blocked("cryweb.github.io"))
        self.assertFalse(matcher.is_blocked("printer.local"))
        self.assertFalse(matcher.is_blocked("1.1.168.192.in-addr.arpa"))


class RealListTests(unittest.TestCase):
    def test_stevenblack_file_when_present(self):
        path = Path("/tmp/stevenblack.hosts")
        if not path.exists():
            self.skipTest("StevenBlack sample was not downloaded")
        domains = parse_domain_list(path.read_text(encoding="utf-8"), skip_never_block=True)
        self.assertIn("doubleclick.net", domains)
        self.assertIn("pagead2.googlesyndication.com", domains)
        self.assertNotIn("example.com", domains)
        self.assertNotIn("localhost", domains)
        self.assertNotIn("localhost.localdomain", domains)
        self.assertGreater(len(domains), 70000)


class SafetyTests(unittest.TestCase):
    def matcher(self, denied=(), allowed=(), blocked=()):
        return Matcher(set(blocked), set(allowed), set(denied))

    def test_essential_parent_wins_over_a_listed_child(self):
        matcher = self.matcher(denied=["google.com", "ads.google.com", "gvt2.com"])
        for name in ("google.com", "www.google.com", "ads.google.com", "gvt2.com", "play.gvt2.com"):
            decision = matcher.decide(name)
            self.assertFalse(decision.blocked, name)
            self.assertIn("essential", decision.reason)

    def test_private_relay_stays_blocked(self):
        matcher = self.matcher(denied=["mask.icloud.com", "mask-h2.icloud.com", "icloud.com"])
        self.assertTrue(matcher.is_blocked("mask.icloud.com"))
        self.assertTrue(matcher.is_blocked("a.mask.icloud.com"))
        self.assertTrue(matcher.is_blocked("mask-h2.icloud.com"))
        self.assertFalse(matcher.is_blocked("www.icloud.com"))
        self.assertFalse(matcher.is_blocked("metrics.icloud.com"))
        allowed = self.matcher(
            denied=["mask.icloud.com"],
            allowed=["mask.icloud.com"],
        )
        decision = allowed.decide("mask.icloud.com")
        self.assertFalse(decision.blocked)
        self.assertIn("allowlist", decision.reason)

    def test_local_names_and_private_reverse_are_not_blocked(self):
        loopback = ".".join(["1", *["0"] * 31]) + ".ip6.arpa"
        matcher = self.matcher(
            denied=[
                "printer.local",
                "lan",
                "mac.lan",
                "foo.home.arpa",
                "1.2.168.192.in-addr.arpa",
                "168.192.in-addr.arpa",
                "10.in-addr.arpa",
                "8.8.8.8.in-addr.arpa",
                loopback,
                "d.f.ip6.arpa",
            ]
        )
        for name in (
            "printer.local",
            "mac.lan",
            "foo.home.arpa",
            "localhost",
            "1.2.168.192.in-addr.arpa",
            "168.192.in-addr.arpa",
            "10.in-addr.arpa",
            "16.172.in-addr.arpa",
            "1.0.0.10.in-addr.arpa",
            loopback,
            "d.f.ip6.arpa",
            "8.e.f.ip6.arpa",
        ):
            decision = matcher.decide(name)
            self.assertFalse(decision.blocked, name)
            self.assertEqual(decision.reason, "local name")
        self.assertTrue(matcher.is_blocked("8.8.8.8.in-addr.arpa"))
        self.assertFalse(matcher.is_blocked("1.2.0.11.in-addr.arpa"))
        self.assertFalse(matcher.is_blocked("192.in-addr.arpa"))

    def test_public_suffix_and_malformed_lines_are_skipped(self):
        text = "doubleclick.net\nbad domain\ncom\ndfirma.pl\n# comment\n@@||skip.example^\n"
        domains, skipped = parse_domain_list_report(
            text,
            allow_single_label=True,
            reject_public_suffix=True,
        )
        self.assertEqual(domains, {"doubleclick.net"})
        self.assertEqual(skipped, 3)
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(directory))
        path = directory / "deny.txt"
        path.write_text(text, encoding="utf-8")
        loaded, file_skipped, error = load_domain_file(
            str(path),
            allow_single_label=True,
            reject_public_suffix=True,
        )
        self.assertEqual(error, "")
        self.assertEqual(loaded, {"doubleclick.net"})
        self.assertEqual(file_skipped, 3)
        empty = directory / "empty.txt"
        empty.write_text("", encoding="utf-8")
        _domains, _skipped, empty_error = load_domain_file(str(empty))
        self.assertIn("empty", empty_error)
        _domains, _skipped, missing_error = load_domain_file(str(directory / "missing.txt"))
        self.assertIn("missing", missing_error)
        garbage = directory / "garbage.txt"
        garbage.write_text("foo..bar\nnot a domain\n", encoding="utf-8")
        _domains, garbage_skipped, garbage_error = load_domain_file(str(garbage))
        self.assertGreater(garbage_skipped, 0)
        self.assertIn("parsed to nothing", garbage_error)


class LargeListTests(unittest.TestCase):
    def test_hundred_thousand_names_load_quickly(self):
        text = "\n".join(f"ad{index}.example.net" for index in range(100_000))
        text += "\nnot a domain\ncom\n"
        tracemalloc.start()
        started = time.perf_counter()
        domains, skipped = parse_domain_list_report(
            text,
            allow_single_label=True,
            reject_public_suffix=True,
        )
        elapsed = time.perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(len(domains), 100_000)
        self.assertIn("ad0.example.net", domains)
        self.assertIn("ad99999.example.net", domains)
        self.assertEqual(skipped, 2)
        self.assertLess(elapsed, 3.0)
        self.assertLess(peak, 80 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
