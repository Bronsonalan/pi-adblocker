"""Hosts files, Adblock rules, and allow/deny decisions."""

from __future__ import annotations

import unittest
from pathlib import Path

import helpers  # noqa: F401

from piadblock.blocklist import Matcher, parse_domain_list

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


class DefaultDenylistTests(unittest.TestCase):
    def test_shipped_denylist_is_exactly_ten_domains(self):
        path = Path(__file__).resolve().parents[1] / "blocklist.txt"
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        expected = [
            "doubleclick.net",
            "googlesyndication.com",
            "googleadservices.com",
            "adnxs.com",
            "adsrvr.org",
            "criteo.com",
            "taboola.com",
            "outbrain.com",
            "scorecardresearch.com",
            "amazon-adsystem.com",
        ]
        self.assertEqual(lines, expected)
        domains = parse_domain_list(path.read_text(encoding="utf-8"), allow_single_label=True)
        self.assertEqual(domains, set(expected))
        matcher = Matcher(set(), set(), domains)
        self.assertTrue(matcher.is_blocked("doubleclick.net"))
        self.assertTrue(matcher.is_blocked("ads.doubleclick.net"))
        self.assertTrue(matcher.is_blocked("pagead2.googlesyndication.com"))
        self.assertTrue(matcher.is_blocked("a.b.criteo.com"))
        self.assertTrue(matcher.is_blocked("foo.amazon-adsystem.com"))
        self.assertFalse(matcher.is_blocked("example.com"))
        self.assertFalse(matcher.is_blocked("google.com"))


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


if __name__ == "__main__":
    unittest.main()
