"""DNS packet parsing and response building."""

from __future__ import annotations

import struct
import unittest

import helpers  # noqa: F401  (puts the repo on sys.path)

from piadblock.dns import (
    A,
    AAAA,
    OPT,
    SERVFAIL,
    TXT,
    DnsError,
    advertised_udp_size,
    blocked_answer,
    build_query,
    build_response,
    cacheable_ttl,
    decode_name,
    encode_name,
    fit_udp,
    parse_message,
    rdata_ip,
    rr_ttl_offsets,
)

# Hand-built query for example.com A, id 0x1234, recursion desired.
# This does not go through build_query, so a bug shared by the encoder and
# the decoder cannot hide.
RAW_QUERY = bytes.fromhex(
    "1234"
    "0100"
    "0001"
    "0000"
    "0000"
    "0000"
    "076578616d706c6503636f6d00"
    "0001"
    "0001"
)


class DnsParseTests(unittest.TestCase):
    def test_raw_query_parses(self):
        message = parse_message(RAW_QUERY)
        self.assertEqual(message.id, 0x1234)
        self.assertEqual(message.qr, 0)
        self.assertEqual(message.rd, 1)
        self.assertEqual(len(message.questions), 1)
        question = message.questions[0]
        self.assertEqual(question.name, "example.com")
        self.assertEqual(question.qtype, A)
        self.assertEqual(question.qclass, 1)
        self.assertEqual(message.answers, [])

    def test_build_query_roundtrip(self):
        packet = build_query("www.Example.COM", AAAA, qid=7, edns_size=1232)
        message = parse_message(packet)
        self.assertEqual(message.id, 7)
        self.assertEqual(message.questions[0].name, "www.example.com")
        self.assertEqual(message.questions[0].qtype, AAAA)
        self.assertEqual(advertised_udp_size(packet), 1232)
        self.assertEqual(message.additionals[0].rtype, OPT)

    def test_name_roundtrip(self):
        encoded = encode_name("a.b.example.com")
        name, offset = decode_name(encoded, 0)
        self.assertEqual(name, "a.b.example.com")
        self.assertEqual(offset, len(encoded))

    def test_root_name(self):
        name, offset = decode_name(b"\x00", 0)
        self.assertEqual(name, "")
        self.assertEqual(offset, 1)

    def test_short_packet_is_rejected(self):
        with self.assertRaises(DnsError):
            parse_message(b"\x00\x01\x02")

    def test_compression_loop_is_rejected(self):
        # A one-byte header pad so the pointer at offset 12 points at itself.
        packet = b"\x00" * 12 + b"\xc0\x0c"
        with self.assertRaises(DnsError):
            decode_name(packet, 12)

    def test_truncated_label_is_rejected(self):
        with self.assertRaises(DnsError):
            decode_name(b"\x05abc", 0)

    def test_non_ascii_label_is_rejected(self):
        with self.assertRaises(DnsError):
            decode_name(b"\x01\xff\x00", 0)


class DnsResponseTests(unittest.TestCase):
    def test_blocked_a_is_null_ipv4(self):
        response = blocked_answer(RAW_QUERY, A, ttl=60, ipv4="0.0.0.0", ipv6="::")
        # The answer name is a compression pointer back to the question at byte 12.
        self.assertIn(b"\xc0\x0c", response)
        message = parse_message(response)
        self.assertEqual(message.id, 0x1234)
        self.assertEqual(message.qr, 1)
        self.assertEqual(message.rd, 1)
        self.assertEqual(message.ra, 1)
        self.assertEqual(message.rcode, 0)
        self.assertEqual(message.questions[0].name, "example.com")
        self.assertEqual(len(message.answers), 1)
        answer = message.answers[0]
        self.assertEqual(answer.name, "example.com")
        self.assertEqual(answer.rtype, A)
        self.assertEqual(answer.ttl, 60)
        self.assertEqual(rdata_ip(answer), "0.0.0.0")

    def test_blocked_aaaa_is_null_ipv6(self):
        query = build_query("ads.example.com", AAAA, qid=9)
        response = blocked_answer(query, AAAA, ttl=30, ipv4="0.0.0.0", ipv6="::")
        message = parse_message(response)
        self.assertEqual(message.id, 9)
        self.assertEqual(rdata_ip(message.answers[0]), "::")
        self.assertEqual(len(message.answers[0].rdata), 16)

    def test_blocked_txt_is_nodata(self):
        query = build_query("ads.example.com", TXT, qid=3)
        response = blocked_answer(query, TXT, ttl=30, ipv4="0.0.0.0", ipv6="::")
        message = parse_message(response)
        self.assertEqual(message.rcode, 0)
        self.assertEqual(message.answers, [])
        self.assertEqual(message.questions[0].qtype, TXT)

    def test_blocked_any_returns_both_sinks(self):
        query = build_query("ads.example.com", 255, qid=4)
        response = blocked_answer(query, 255, ttl=15, ipv4="0.0.0.0", ipv6="::")
        message = parse_message(response)
        kinds = {answer.rtype: rdata_ip(answer) for answer in message.answers}
        self.assertEqual(kinds[A], "0.0.0.0")
        self.assertEqual(kinds[AAAA], "::")

    def test_ttl_offsets_skip_opt(self):
        response = bytearray(build_response(RAW_QUERY, [(A, 100, b"\x01\x02\x03\x04"), (A, 40, b"\x05\x06\x07\x08")]))
        # Append an OPT record and count it in ARCOUNT. Its "TTL" is flags, not a TTL.
        struct.pack_into("!H", response, 10, 1)
        response += b"\x00" + struct.pack("!HHIH", OPT, 1232, 0x00008000, 0)
        offsets = rr_ttl_offsets(bytes(response))
        self.assertEqual(len(offsets), 2)
        message = parse_message(bytes(response))
        self.assertEqual([answer.ttl for answer in message.answers], [100, 40])
        self.assertEqual(message.additionals[0].rtype, OPT)
        self.assertNotEqual(message.additionals[0].ttl, 0)

    def test_fit_udp_sets_tc_without_edns(self):
        query = build_query("example.com", TXT, qid=5)
        big = build_response(query, [(TXT, 60, b"x" * 200) for _ in range(6)])
        self.assertGreater(len(big), 512)
        fitted = fit_udp(query, big)
        self.assertLessEqual(len(fitted), 512)
        message = parse_message(fitted)
        self.assertEqual(message.tc, 1)
        self.assertEqual(message.answers, [])
        self.assertEqual(message.id, 5)

    def test_fit_udp_keeps_large_response_when_client_allows_it(self):
        query = build_query("example.com", TXT, qid=6, edns_size=4096)
        big = build_response(query, [(TXT, 60, b"x" * 200) for _ in range(6)])
        self.assertGreater(len(big), 512)
        self.assertIs(fit_udp(query, big), big)

    def test_cacheable_ttl(self):
        query = build_query("example.com", A)
        packet = build_response(query, [(A, 100, b"\x01\x02\x03\x04"), (A, 40, b"\x08\x08\x08\x08")])
        self.assertEqual(cacheable_ttl(packet, min_ttl=0, max_ttl=86400, negative_ttl=300), 40)
        self.assertEqual(cacheable_ttl(packet, min_ttl=60, max_ttl=86400, negative_ttl=300), 60)
        self.assertIsNone(cacheable_ttl(packet, min_ttl=0, max_ttl=0, negative_ttl=300))
        failure = build_response(query, [], rcode=SERVFAIL)
        self.assertIsNone(cacheable_ttl(failure, min_ttl=0, max_ttl=86400, negative_ttl=300))
        empty = build_response(query, [])
        self.assertEqual(cacheable_ttl(empty, min_ttl=0, max_ttl=86400, negative_ttl=300), 300)


if __name__ == "__main__":
    unittest.main()
