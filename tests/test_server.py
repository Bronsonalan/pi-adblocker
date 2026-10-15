"""DNS server: block, forward, cache, and TCP."""

from __future__ import annotations

import io
import logging
import socket
import struct
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

from piadblock.blocklist import Matcher
from piadblock.config import Config
from piadblock.dns import A, AAAA, TXT, build_query, build_response, parse_message, rdata_ip
from piadblock.forward import recvall, udp_exchange
from piadblock.querylog import format_lookup
from piadblock.server import DnsServer
from piadblock.stats import utc_day


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


class DetailsTests(unittest.TestCase):
    def test_details_uses_the_denylist_when_the_download_is_empty(self):
        config = Config(
            listen="127.0.0.1",
            port=0,
            ipv6=False,
            status_enabled=False,
            auto_update=False,
            auto_reload=False,
            log_file="",
            stats_file="",
            blocklists=[],
        )
        matcher = Matcher(set(), {"safe.example"}, {"ads.example", "track.example"})
        server = DnsServer(config, matcher)
        self.addCleanup(server.stop)
        details = server.details()
        self.assertEqual(matcher.blocked_count, 0)
        self.assertEqual(details["blocklist_domains"], 2)
        self.assertEqual(details["allow_count"], 1)
        self.assertEqual(details["deny_count"], 2)


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
        feed = self.server.lookups.snapshot()
        self.assertEqual(feed[0]["status"], "OK")
        self.assertEqual(feed[0]["name"], "example.com")
        self.assertEqual(feed[-1]["status"], "BLOCKED")
        self.assertEqual(feed[-1]["name"], "ads.example.com")

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
        snap = self.server.stats.snapshot()
        self.assertGreaterEqual(snap["blocked"], 3)
        day = snap["days"][utc_day()]
        self.assertGreaterEqual(day["blocked"], 3)
        self.assertGreaterEqual(day["domains"]["ads.example.com"], 2)
        self.assertEqual(day["domains"]["a.ads.example.com"], 1)

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

    def test_empty_or_broken_reload_keeps_the_previous_list(self):
        self.denylist.write_text("", encoding="utf-8")
        self.assertFalse(self.server._reload_lists())
        kept = ask(self.server.port, "doubleclick.net", A, qid=91)
        self.assertEqual(rdata_ip(kept.answers[0]), "0.0.0.0")

        self.denylist.write_text("bad domain\ncom\nfoo..bar\n", encoding="utf-8")
        self.assertFalse(self.server._reload_lists())
        kept = ask(self.server.port, "googlesyndication.com", A, qid=92)
        self.assertEqual(rdata_ip(kept.answers[0]), "0.0.0.0")

        with mock.patch(
            "piadblock.blocklist._read_stable",
            return_value=("", "could not read denylist (Permission denied)"),
        ):
            self.assertFalse(self.server._reload_lists())
        kept = ask(self.server.port, "doubleclick.net", A, qid=93)
        self.assertEqual(rdata_ip(kept.answers[0]), "0.0.0.0")

    def test_malformed_lines_are_skipped_and_good_names_still_block(self):
        self.denylist.write_text(
            "doubleclick.net\nbad domain\ncom\ndfirma.pl\n# comment\n",
            encoding="utf-8",
        )
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        logger = logging.getLogger("piadblock")
        handler = Capture()
        previous = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            self.assertTrue(self.server._reload_lists())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)
        self.assertTrue(any("skipped 3 malformed blocklist lines" in line for line in records))
        blocked = ask(self.server.port, "ads.doubleclick.net", A, qid=94)
        self.assertEqual(rdata_ip(blocked.answers[0]), "0.0.0.0")
        suffix = ask(self.server.port, "dfirma.pl", A, qid=95)
        self.assertEqual(rdata_ip(suffix.answers[0]), "93.184.216.34")

    def test_essential_allow_and_local_names_are_forwarded(self):
        self.denylist.write_text(
            "\n".join(
                [
                    "doubleclick.net",
                    "ads.google.com",
                    "gvt2.com",
                    "mask.icloud.com",
                    "mask-h2.icloud.com",
                    "printer.local",
                    "mac.lan",
                    "1.2.168.192.in-addr.arpa",
                    "8.8.8.8.in-addr.arpa",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertTrue(self.server._reload_lists())
        forwarded = {
            101: "www.google.com",
            102: "ads.google.com",
            103: "play.gvt2.com",
            104: "printer.local",
            105: "mac.lan",
            106: "1.2.168.192.in-addr.arpa",
        }
        for qid, name in forwarded.items():
            message = ask(self.server.port, name, A, qid=qid)
            self.assertEqual(rdata_ip(message.answers[0]), "93.184.216.34", name)
        for qid, name in (
            (107, "doubleclick.net"),
            (108, "mask.icloud.com"),
            (109, "mask-h2.icloud.com"),
            (110, "8.8.8.8.in-addr.arpa"),
        ):
            message = ask(self.server.port, name, A, qid=qid)
            self.assertEqual(rdata_ip(message.answers[0]), "0.0.0.0", name)

    def test_queries_stay_fast_while_a_large_list_reloads(self):
        names = [f"ad{index}.example.net" for index in range(100_000)]
        names.extend(
            [
                "doubleclick.net",
                "ads.google.com",
                "mask.icloud.com",
                "dfirma.pl",
                "printer.local",
            ]
        )
        self.denylist.write_text("\n".join(names) + "\n", encoding="utf-8")
        gate = threading.Event()
        entered = threading.Event()
        original = Matcher.prepare

        def gated(merged_path, allow_path, deny_path):
            entered.set()
            self.assertTrue(gate.wait(5))
            return original(merged_path, allow_path, deny_path)

        try:
            with mock.patch("piadblock.server.Matcher.prepare", side_effect=gated):
                self.server._start_reload()
                self.assertTrue(entered.wait(5))
                timings = []
                for qid, name in ((121, "example.com"), (122, "doubleclick.net"), (123, "ad0.example.net")):
                    started = time.perf_counter()
                    message = ask(self.server.port, name, A, qid=qid)
                    timings.append(time.perf_counter() - started)
                    if name == "doubleclick.net":
                        self.assertEqual(rdata_ip(message.answers[0]), "0.0.0.0")
                    else:
                        self.assertEqual(rdata_ip(message.answers[0]), "93.184.216.34")
                self.assertLess(max(timings), 0.75)
                reload_thread = self.server._reload_thread
                self.assertIsNotNone(reload_thread)
                gate.set()
                reload_thread.join(10)
                self.assertFalse(reload_thread.is_alive())
        finally:
            gate.set()

        blocked = ask(self.server.port, "ad0.example.net", A, qid=124)
        self.assertEqual(rdata_ip(blocked.answers[0]), "0.0.0.0")
        essential = ask(self.server.port, "ads.google.com", A, qid=125)
        self.assertEqual(rdata_ip(essential.answers[0]), "93.184.216.34")
        relay = ask(self.server.port, "mask.icloud.com", A, qid=126)
        self.assertEqual(rdata_ip(relay.answers[0]), "0.0.0.0")
        suffix = ask(self.server.port, "www.dfirma.pl", A, qid=127)
        self.assertEqual(rdata_ip(suffix.answers[0]), "93.184.216.34")
        local = ask(self.server.port, "printer.local", A, qid=128)
        self.assertEqual(rdata_ip(local.answers[0]), "93.184.216.34")


if __name__ == "__main__":
    unittest.main()
