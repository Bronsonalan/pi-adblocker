"""Upstream failover: timeout, SERVFAIL, REFUSED, and truncated UDP."""

from __future__ import annotations

import contextlib
import io
import logging
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

import helpers  # noqa: F401

from piadblock.config import Config
from piadblock.dns import A, REFUSED, SERVFAIL, build_query, build_response, parse_message, rdata_ip
from piadblock.forward import exchange, recvall, udp_exchange
from piadblock.server import DnsServer


def _answer(query: bytes, *, rcode: int = 0, tc: bool = False) -> bytes:
    answers = [] if rcode or tc else [(A, 60, socket.inet_aton("93.184.216.34"))]
    return build_response(query, answers, rcode=rcode, tc=tc)


class _Endpoint(threading.Thread):
    """One address. ``mode`` is ok, servfail, refused, silent, or tc."""

    def __init__(self, host: str, port: int, mode: str):
        super().__init__(daemon=True)
        self.mode = mode
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind((host, port))
        self.port = self.udp.getsockname()[1]
        self.udp.settimeout(0.2)
        self.tcp: socket.socket | None = None
        if mode == "tc":
            self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.tcp.bind((host, self.port))
            self.tcp.listen(4)
            self.tcp.settimeout(0.2)
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            if self.mode != "silent":
                self._udp_once()
            if self.tcp is not None:
                self._tcp_once()
            if self.mode == "silent":
                self._halt.wait(0.05)

    def _udp_once(self) -> None:
        try:
            data, addr = self.udp.recvfrom(4096)
        except socket.timeout:
            return
        if self.mode == "silent":
            return
        if self.mode == "servfail":
            packet = _answer(data, rcode=SERVFAIL)
        elif self.mode == "refused":
            packet = _answer(data, rcode=REFUSED)
        elif self.mode == "tc":
            packet = _answer(data, tc=True)
        else:
            packet = _answer(data)
        try:
            self.udp.sendto(packet, addr)
        except OSError:
            return

    def _tcp_once(self) -> None:
        assert self.tcp is not None
        try:
            conn, _addr = self.tcp.accept()
        except socket.timeout:
            return
        with conn:
            conn.settimeout(1)
            try:
                length = struct.unpack("!H", recvall(conn, 2))[0]
                query = recvall(conn, length)
                packet = _answer(query)
                conn.sendall(struct.pack("!H", len(packet)) + packet)
            except (OSError, EOFError):
                return

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=2)
        self.udp.close()
        if self.tcp is not None:
            self.tcp.close()


class ExchangeTests(unittest.TestCase):
    def test_timeout_uses_the_next_upstream(self):
        silent = _Endpoint("127.0.0.1", 0, "silent")
        good = _Endpoint("127.0.0.2", silent.port, "ok")
        silent.start()
        good.start()
        self.addCleanup(silent.stop)
        self.addCleanup(good.stop)
        query = build_query("example.com", A, qid=7)
        result = exchange(query, ["127.0.0.1", "127.0.0.2"], silent.port, 0.2)
        self.assertTrue(result.ok)
        self.assertEqual([item.reason for item in result.attempts], ["timeout"])
        self.assertEqual(rdata_ip(parse_message(result.packet).answers[0]), "93.184.216.34")
        self.assertEqual(result.packet[:2], query[:2])

    def test_servfail_uses_the_next_upstream(self):
        bad = _Endpoint("127.0.0.1", 0, "servfail")
        good = _Endpoint("127.0.0.2", bad.port, "ok")
        bad.start()
        good.start()
        self.addCleanup(bad.stop)
        self.addCleanup(good.stop)
        result = exchange(build_query("example.com", A, qid=8), ["127.0.0.1", "127.0.0.2"], bad.port, 0.5)
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts[0].reason, "servfail")
        self.assertEqual(result.attempts[0].detail, "SERVFAIL")
        self.assertEqual(rdata_ip(parse_message(result.packet).answers[0]), "93.184.216.34")

    def test_every_upstream_servfail_is_returned_and_not_ok(self):
        bad = _Endpoint("127.0.0.1", 0, "servfail")
        bad.start()
        self.addCleanup(bad.stop)
        result = exchange(build_query("example.com", A, qid=9), ["127.0.0.1"], bad.port, 0.5)
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts[0].reason, "servfail")
        self.assertEqual(parse_message(result.packet).rcode, SERVFAIL)

    def test_refused_uses_the_next_upstream(self):
        bad = _Endpoint("127.0.0.1", 0, "refused")
        good = _Endpoint("127.0.0.2", bad.port, "ok")
        bad.start()
        good.start()
        self.addCleanup(bad.stop)
        self.addCleanup(good.stop)
        result = exchange(build_query("example.com", A, qid=10), ["127.0.0.1", "127.0.0.2"], bad.port, 0.5)
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts[0].reason, "refused")

    def test_truncated_udp_is_retried_over_tcp(self):
        endpoint = _Endpoint("127.0.0.1", 0, "tc")
        endpoint.start()
        self.addCleanup(endpoint.stop)
        result = exchange(build_query("example.com", A, qid=11), ["127.0.0.1"], endpoint.port, 0.5)
        self.assertTrue(result.ok)
        self.assertEqual([item.reason for item in result.attempts], ["truncation"])
        self.assertIn("TCP", result.attempts[0].detail)
        self.assertEqual(rdata_ip(parse_message(result.packet).answers[0]), "93.184.216.34")
        self.assertEqual(parse_message(result.packet).tc, 0)


class ServerFallbackTests(unittest.TestCase):
    def test_timeout_is_logged_and_counted_without_failing_the_client(self):
        silent = _Endpoint("127.0.0.1", 0, "silent")
        good = _Endpoint("127.0.0.2", silent.port, "ok")
        silent.start()
        good.start()
        self.addCleanup(silent.stop)
        self.addCleanup(good.stop)
        with tempfile.TemporaryDirectory() as raw:
            config = Config(
                listen="127.0.0.1",
                port=0,
                tcp=False,
                ipv6=False,
                upstream=["127.0.0.1", "127.0.0.2"],
                upstream_port=silent.port,
                upstream_timeout=0.2,
                cache_size=0,
                workers=2,
                status_enabled=False,
                auto_update=False,
                auto_reload=False,
                log_file="",
                stats_file=str(Path(raw) / "stats.json"),
                blocklists=[],
                blocklist_dir=raw,
                allowlist=str(Path(raw) / "allow.txt"),
                denylist=str(Path(raw) / "deny.txt"),
            )
            Path(config.allowlist).write_text("", encoding="utf-8")
            Path(config.denylist).write_text("", encoding="utf-8")
            server = DnsServer(config)
            server.start_background()
            self.addCleanup(server.stop)
            with self.assertLogs("piadblock", level="WARNING") as caught:
                query = build_query("example.com", A, qid=21)
                from piadblock.forward import udp_exchange

                data = udp_exchange("127.0.0.1", server.port, query, timeout=2)
            message = parse_message(data)
            self.assertEqual(rdata_ip(message.answers[0]), "93.184.216.34")
            snap = server.stats.snapshot()
            self.assertEqual(snap["errors"], 0)
            self.assertEqual(snap["upstream_errors"]["timeout"], 1)
            self.assertEqual(snap["today"]["upstream_errors"]["timeout"], 1)
            self.assertEqual(snap["forwarded"], 1)
            text = "\n".join(caught.output)
            self.assertIn("upstream timeout 127.0.0.1 for example.com A:", text)
            self.assertNotIn("upstream failed for example.com", text)

    def test_servfail_from_every_upstream_is_an_error(self):
        bad = _Endpoint("127.0.0.1", 0, "servfail")
        bad.start()
        self.addCleanup(bad.stop)
        with tempfile.TemporaryDirectory() as raw:
            config = Config(
                listen="127.0.0.1",
                port=0,
                tcp=False,
                ipv6=False,
                upstream=["127.0.0.1"],
                upstream_port=bad.port,
                upstream_timeout=0.5,
                cache_size=0,
                workers=2,
                status_enabled=False,
                auto_update=False,
                auto_reload=False,
                log_file="",
                stats_file="",
                blocklists=[],
                blocklist_dir=raw,
                allowlist=str(Path(raw) / "allow.txt"),
                denylist=str(Path(raw) / "deny.txt"),
            )
            Path(config.allowlist).write_text("", encoding="utf-8")
            Path(config.denylist).write_text("", encoding="utf-8")
            server = DnsServer(config)
            server.start_background()
            self.addCleanup(server.stop)
            logging.getLogger("piadblock").setLevel(logging.WARNING)
            with self.assertLogs("piadblock", level="WARNING") as caught:
                from piadblock.forward import udp_exchange

                data = udp_exchange(
                    "127.0.0.1",
                    server.port,
                    build_query("example.com", A, qid=22),
                    timeout=2,
                )
            self.assertEqual(parse_message(data).rcode, SERVFAIL)
            snap = server.stats.snapshot()
            self.assertEqual(snap["errors"], 1)
            self.assertEqual(snap["upstream_errors"]["servfail"], 1)
            self.assertEqual(snap["forwarded"], 0)
            self.assertIn("upstream servfail 127.0.0.1 for example.com A: SERVFAIL", "\n".join(caught.output))
            self.assertIn("upstream failed for example.com A:", "\n".join(caught.output))


class ForwardLatencyTests(unittest.TestCase):
    def test_forwarded_p50_against_an_instant_stub_is_under_10ms(self):
        stub = _Endpoint("127.0.0.1", 0, "ok")
        stub.start()
        self.addCleanup(stub.stop)
        with tempfile.TemporaryDirectory() as raw:
            config = Config(
                listen="127.0.0.1",
                port=0,
                tcp=False,
                ipv6=False,
                upstream=["127.0.0.1"],
                upstream_port=stub.port,
                upstream_timeout=0.4,
                cache_size=0,
                workers=4,
                status_enabled=False,
                auto_update=False,
                auto_reload=False,
                log_file="",
                stats_file="",
                blocklists=[],
                blocklist_dir=raw,
                allowlist=str(Path(raw) / "allow.txt"),
                denylist=str(Path(raw) / "deny.txt"),
            )
            Path(config.allowlist).write_text("\n", encoding="utf-8")
            Path(config.denylist).write_text("doubleclick.net\n", encoding="utf-8")
            server = DnsServer(config)
            server.start_background()
            self.addCleanup(server.stop)
            samples = []
            with contextlib.redirect_stdout(io.StringIO()):
                for index in range(40):
                    query = build_query(f"n{index}.example", A, qid=index + 1)
                    started = time.perf_counter()
                    data = udp_exchange("127.0.0.1", server.port, query, timeout=1)
                    samples.append((time.perf_counter() - started) * 1000.0)
                    self.assertEqual(rdata_ip(parse_message(data).answers[0]), "93.184.216.34")
                blocked = udp_exchange(
                    "127.0.0.1",
                    server.port,
                    build_query("doubleclick.net", A, qid=99),
                    timeout=1,
                )
            samples.sort()
            p50 = samples[len(samples) // 2]
            self.assertLess(p50, 10.0, f"p50 {p50:.2f} ms")
            self.assertEqual(server.stats.snapshot()["errors"], 0)
            self.assertEqual(server.stats.snapshot()["forwarded"], 40)
            self.assertEqual(rdata_ip(parse_message(blocked).answers[0]), "0.0.0.0")

    def test_first_forward_after_listen_falls_back_inside_three_seconds(self):
        silent = _Endpoint("127.0.0.1", 0, "silent")
        good = _Endpoint("127.0.0.2", silent.port, "ok")
        silent.start()
        good.start()
        self.addCleanup(silent.stop)
        self.addCleanup(good.stop)
        with tempfile.TemporaryDirectory() as raw:
            config = Config(
                listen="127.0.0.1",
                port=0,
                tcp=False,
                ipv6=False,
                upstream=["127.0.0.1", "127.0.0.2"],
                upstream_port=silent.port,
                upstream_timeout=0.4,
                cache_size=0,
                workers=2,
                status_enabled=False,
                auto_update=False,
                auto_reload=False,
                log_file="",
                stats_file="",
                blocklists=[],
                blocklist_dir=raw,
                allowlist=str(Path(raw) / "allow.txt"),
                denylist=str(Path(raw) / "deny.txt"),
            )
            Path(config.allowlist).write_text("\n", encoding="utf-8")
            Path(config.denylist).write_text("\n", encoding="utf-8")
            server = DnsServer(config)
            server.start_background()
            self.addCleanup(server.stop)
            started = time.perf_counter()
            with contextlib.redirect_stdout(io.StringIO()):
                data = udp_exchange(
                    "127.0.0.1",
                    server.port,
                    build_query("google.com", A, qid=7),
                    timeout=3,
                )
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 3.0)
            self.assertEqual(rdata_ip(parse_message(data).answers[0]), "93.184.216.34")
            self.assertEqual(server.stats.snapshot()["errors"], 0)
            self.assertEqual(server.stats.snapshot()["forwarded"], 1)


if __name__ == "__main__":
    unittest.main()
