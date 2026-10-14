"""LRU cache, expiry, and TTL countdown."""

from __future__ import annotations

import unittest

import helpers  # noqa: F401

from piadblock.cache import DnsCache
from piadblock.dns import A, build_query, build_response, parse_message

QUERY = build_query("example.com", A, qid=1)
PACKET = build_response(QUERY, [(A, 100, b"\x01\x02\x03\x04")])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.cache = DnsCache(max_size=2, min_ttl=0, max_ttl=86400, clock=lambda: self.now[0])

    def test_hit_rewrites_id_and_counts_ttl_down(self):
        self.cache.store(("example.com", A, 1), PACKET, 100)
        hit = self.cache.get(("example.com", A, 1), 0xABCD)
        self.assertIsNotNone(hit)
        message = parse_message(hit)
        self.assertEqual(message.id, 0xABCD)
        self.assertEqual(message.answers[0].ttl, 100)

        self.now[0] += 40
        later = parse_message(self.cache.get(("example.com", A, 1), 2))
        self.assertEqual(later.id, 2)
        self.assertEqual(later.answers[0].ttl, 60)

    def test_expiry(self):
        self.cache.store(("example.com", A, 1), PACKET, 10)
        self.now[0] += 10
        self.assertIsNone(self.cache.get(("example.com", A, 1), 1))
        self.assertEqual(len(self.cache), 0)

    def test_zero_ttl_is_not_stored(self):
        self.cache.store(("example.com", A, 1), PACKET, 0)
        self.assertIsNone(self.cache.get(("example.com", A, 1), 1))

    def test_lru_evicts_least_recently_used(self):
        self.cache.store(("a.example", A, 1), PACKET, 50)
        self.cache.store(("b.example", A, 1), PACKET, 50)
        # Touch a.example so b.example is the least recently used.
        self.assertIsNotNone(self.cache.get(("a.example", A, 1), 1))
        self.cache.store(("c.example", A, 1), PACKET, 50)
        self.assertIsNone(self.cache.get(("b.example", A, 1), 1))
        self.assertIsNotNone(self.cache.get(("a.example", A, 1), 1))
        self.assertIsNotNone(self.cache.get(("c.example", A, 1), 1))

    def test_disabled_cache(self):
        cache = DnsCache(max_size=0, min_ttl=0, max_ttl=0, clock=lambda: self.now[0])
        cache.store(("example.com", A, 1), PACKET, 30)
        self.assertEqual(len(cache), 0)


if __name__ == "__main__":
    unittest.main()
