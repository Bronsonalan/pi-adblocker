"""DNS server: block, forward, cache, and TCP."""

from __future__ import annotations

import io
import socket
import struct
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import helpers  # noqa: F401

from piadblock.blocklist import Matcher
from piadblock.config import Config
from piadblock.dns import A, AAAA, TXT, build_query, build_response, parse_message, rdata_ip
from piadblock.forward import recvall, udp_exchange
from piadblock.querylog import format_lookup
from piadblock.server import DnsServer


class Upstream(threading.Thread):
    """Stand-in resolver that counts queries and answers example.com."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.2)
        self.count = 0
        self._halt = threading.Event()
        self._lock = threading.Lock()

    def run(self):
        while not self._halt.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            with self._lock:
                self.count += 1
            try:
                qtype = parse_message(data).questions[0].qtype
            except Exception:
                continue
            if qtype == AAAA:
                rdata = socket.inet_pton(socket.AF_INET6, "2001:db8::10")
                response = build_response(data, [(AAAA, 120, rdata)])
            else:
                response = build_response(data, [(A, 120, socket.inet_aton("93.184.216.34"))])
            try:
                self.sock.sendto(response, addr)
            except OSError:
                return

    def stop(self):
        self._halt.set()
        self.join(timeout=2)
        self.sock.close()

    @property
    def queries(self) -> int:
        with self._lock:
            return self.count


def ask(port: int, name: str, qtype: int = A, qid: int = 1):
    query = build_query(name, qtype, qid=qid)
    data = udp_exchange("127.0.0.1", port, query, timeout=2)
    return parse_message(data)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self._stdout = io.StringIO()
        self._stdout_ctx = redirect_stdout(self._stdout)
        self._stdout_ctx.__enter__()
        self.upstream = Upstream()
        self.upstream.start()
        self.matcher = Matcher(
            blocked={"ads.example.com", "allowed.example.com"},
            allowed={"allowed.example.com"},
            denied=set(),
        )
        self.config = Config(
            listen="127.0.0.1",
            port=0,
            tcp=True,
            ipv6=False,
            upstream=["127.0.0.1"],
            upstream_port=self.upstream.port,
            upstream_timeout=0.5,
            cache_size=128,
            workers=4,
            status_enabled=False,
            auto_update=False,
            auto_reload=False,
            log_file="",
            stats_file="",
            blocklists=[],
        )
        self.server = DnsServer(self.config, self.matcher)
        self.server.start_background()

    def tearDown(self):
        self.server.stop()
        self.upstream.stop()
        self._stdout_ctx.__exit__(None, None, None)

    def test_every_lookup_is_printed_to_stdout(self):
        self.assertEqual(
            format_lookup("BLOCKED", "192.168.1.10", "doubleclick.net", "A"),
            "BLOCKED 192.168.1.10 doubleclick.net A",
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            blocked = ask(self.server.port, "ads.example.com", A, qid=71)
            forwarded = ask(self.server.port, "example.com", A, qid=72)
            again = ask(self.server.port, "example.com", A, qid=73)
        self.assertEqual(rdata_ip(blocked.answers[0]), "0.0.0.0")
        self.assertEqual(rdata_ip(forwarded.answers[0]), "93.184.216.34")
        self.assertEqual(again.id, 73)
        text = buffer.getvalue().splitlines()
        self.assertIn("BLOCKED 127.0.0.1 ads.example.com A", text)
        self.assertIn("OK 127.0.0.1 example.com A", text)
        self.assertEqual(text.count("OK 127.0.0.1 example.com A"), 2)
        self.assertNotIn("BLOCKED 127.0.0.1 example.com A", text)

    def test_blocked_names_do_not_reach_upstream(self):
        message = ask(self.server.port, "ads.example.com", A, qid=11)
        self.assertEqual(rdata_ip(message.answers[0]), "0.0.0.0")
        self.assertEqual(message.id, 11)

        aaaa = ask(self.server.port, "a.ads.example.com", AAAA, qid=12)
        self.assertEqual(rdata_ip(aaaa.answers[0]), "::")

        txt = ask(self.server.port, "ads.example.com", TXT, qid=13)
        self.assertEqual(txt.rcode, 0)
        self.assertEqual(txt.answers, [])
        self.assertEqual(self.upstream.queries, 0)
        self.assertGreaterEqual(self.server.stats.snapshot()["blocked"], 3)

    def test_forward_then_cache(self):
        first = ask(self.server.port, "example.com", A, qid=21)
        self.assertEqual(rdata_ip(first.answers[0]), "93.184.216.34")
        self.assertEqual(first.id, 21)
        self.assertEqual(self.upstream.queries, 1)

        second = ask(self.server.port, "example.com", A, qid=22)
        self.assertEqual(rdata_ip(second.answers[0]), "93.184.216.34")
        self.assertEqual(second.id, 22)
        self.assertEqual(self.upstream.queries, 1)
        self.assertEqual(self.server.stats.snapshot()["cached"], 1)

    def test_allowlist_overrides_blocklist(self):
        message = ask(self.server.port, "allowed.example.com", A, qid=31)
        self.assertEqual(rdata_ip(message.answers[0]), "93.184.216.34")
        self.assertEqual(self.upstream.queries, 1)

    def test_tcp_block(self):
        query = build_query("ads.example.com", A, qid=41)
        sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=2)
        try:
            sock.sendall(struct.pack("!H", len(query)) + query)
            length = struct.unpack("!H", recvall(sock, 2))[0]
            message = parse_message(recvall(sock, length))
        finally:
            sock.close()
        self.assertEqual(message.id, 41)
        self.assertEqual(rdata_ip(message.answers[0]), "0.0.0.0")
        self.assertEqual(self.upstream.queries, 0)

    def test_malformed_packet_does_not_kill_the_server(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(b"\x00\x01", ("127.0.0.1", self.server.port))
        finally:
            sock.close()
        message = ask(self.server.port, "example.com", A, qid=51)
        self.assertEqual(rdata_ip(message.answers[0]), "93.184.216.34")

    def test_upstream_failure_is_servfail_and_blocks_still_answer(self):
        self.server.stop()
        self.config.upstream_port = 1
        self.config.upstream_timeout = 0.3
        self.server = DnsServer(self.config, self.matcher)
        self.server.start_background()
        failed = ask(self.server.port, "example.com", A, qid=61)
        self.assertEqual(failed.rcode, 2)
        self.assertEqual(failed.answers, [])
        blocked = ask(self.server.port, "ads.example.com", A, qid=62)
        self.assertEqual(rdata_ip(blocked.answers[0]), "0.0.0.0")


class ReloadTests(unittest.TestCase):
    """Deleting a denylist line unblocks. The allowlist stays a separate override."""

    def setUp(self):
        self._stdout = io.StringIO()
        self._stdout_ctx = redirect_stdout(self._stdout)
        self._stdout_ctx.__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        self.denylist = directory / "deny.txt"
        self.allowlist = directory / "allow.txt"
        self.denylist.write_text("doubleclick.net\ngooglesyndication.com\n", encoding="utf-8")
        self.allowlist.write_text("", encoding="utf-8")
        (directory / "merged.txt").write_text("", encoding="utf-8")
        self.upstream = Upstream()
        self.upstream.start()
        self.config = Config(
            listen="127.0.0.1",
            port=0,
            tcp=False,
            ipv6=False,
            upstream=["127.0.0.1"],
            upstream_port=self.upstream.port,
            upstream_timeout=0.5,
            cache_size=128,
            workers=4,
            status_enabled=False,
            auto_update=False,
            auto_reload=False,
            log_file="",
            stats_file="",
            blocklists=[],
            blocklist_dir=str(directory),
            allowlist=str(self.allowlist),
            denylist=str(self.denylist),
        )
        self.server = DnsServer(self.config)
        self.server.start_background()

    def tearDown(self):
        self.server.stop()
        self.upstream.stop()
        self._stdout_ctx.__exit__(None, None, None)

    def test_deleting_a_line_unblocks_and_allowlist_still_overrides(self):
        blocked = ask(self.server.port, "doubleclick.net", A, qid=81)
        self.assertEqual(rdata_ip(blocked.answers[0]), "0.0.0.0")

        self.denylist.write_text("googlesyndication.com\n", encoding="utf-8")
        self.server._reload_lists()
        forwarded = ask(self.server.port, "doubleclick.net", A, qid=82)
        self.assertEqual(rdata_ip(forwarded.answers[0]), "93.184.216.34")
        sibling = ask(self.server.port, "googlesyndication.com", A, qid=83)
        self.assertEqual(rdata_ip(sibling.answers[0]), "0.0.0.0")

        # A positive answer may now be cached. Putting the name back still blocks,
        # because the block check runs before the cache.
        self.denylist.write_text("doubleclick.net\ngooglesyndication.com\n", encoding="utf-8")
        self.server._reload_lists()
        blocked_again = ask(self.server.port, "doubleclick.net", A, qid=84)
        self.assertEqual(rdata_ip(blocked_again.answers[0]), "0.0.0.0")

        self.denylist.unlink()
        self.server._reload_lists()
        kept = ask(self.server.port, "googlesyndication.com", A, qid=85)
        self.assertEqual(rdata_ip(kept.answers[0]), "0.0.0.0")

        self.denylist.write_text("doubleclick.net\ngooglesyndication.com\n", encoding="utf-8")
        self.allowlist.write_text("doubleclick.net\n", encoding="utf-8")
        self.server._reload_lists()
        allowed = ask(self.server.port, "doubleclick.net", A, qid=86)
        self.assertEqual(rdata_ip(allowed.answers[0]), "93.184.216.34")
        self.assertIn("doubleclick.net", self.denylist.read_text(encoding="utf-8"))
        still = ask(self.server.port, "googlesyndication.com", A, qid=87)
        self.assertEqual(rdata_ip(still.answers[0]), "0.0.0.0")
        self.assertGreaterEqual(self.upstream.queries, 1)


if __name__ == "__main__":
    unittest.main()
