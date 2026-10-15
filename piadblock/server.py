"""DNS server: block, cache, or forward.

UDP port 53 is the path clients actually use. TCP is there for answers that
do not fit in a UDP packet (the TC bit tells the client to retry).
"""

from __future__ import annotations

import errno
import logging
import select
import signal
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from piadblock import __version__
from piadblock.blocklist import Matcher
from piadblock.cache import DnsCache
from piadblock.config import Config
from piadblock.dns import (
    CLASS_ANY,
    CLASS_IN,
    NOTIMP,
    SERVFAIL,
    DnsError,
    blocked_answer,
    build_response,
    cacheable_ttl,
    fit_udp,
    parse_message,
    type_name,
)
from piadblock.forward import exchange, recvall
from piadblock.portcheck import describe, listeners_on
from piadblock.querylog import emit_lookup
from piadblock.stats import Stats
from piadblock.status import StatusPage
from piadblock.updater import update_blocklists

logger = logging.getLogger("piadblock")


class DnsServer:
    def __init__(self, config: Config, matcher: Matcher | None = None):
        self.config = config
        self._load_error = ""
        if matcher is not None:
            self._matcher = matcher
        else:
            report = Matcher.prepare(config.merged_path, config.allowlist, config.denylist)
            self._matcher = report.matcher
            self._load_error = report.error
            if report.error:
                logger.error("blocklist load failed (%s); retrying", report.error)
            elif report.skipped:
                logger.warning("skipped %s malformed blocklist lines", report.skipped)
        self.cache = DnsCache(
            config.cache_size,
            config.cache_min_ttl,
            config.cache_max_ttl,
        )
        self.stats = Stats(config.stats_file)
        self.port = config.port
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._bind_error: BaseException | None = None
        self._reload_flag = False
        self._sockets: list[socket.socket] = []
        self._tcp_socks: set[socket.socket] = set()
        self._slots = threading.BoundedSemaphore(config.workers)
        self._executor = ThreadPoolExecutor(
            max_workers=config.workers,
            thread_name_prefix="piadblock",
        )
        self._status: StatusPage | None = None
        self._thread: threading.Thread | None = None
        self._update_thread: threading.Thread | None = None
        self._reload_lock = threading.Lock()
        self._reload_thread: threading.Thread | None = None
        self._reload_again = False
        self._last_reload_error = ""
        self._last_reload_error_at = 0.0
        self._mtime_token: tuple = ()
        self._last_mtime_check = 0.0
        self._last_summary = time.monotonic()
        self._last_save = time.monotonic()
        self._summary_queries = -1

    def start_background(self) -> None:
        """Bind and serve on a daemon thread. Used by tests."""
        self._thread = threading.Thread(target=self._loop, name="piadblock-dns", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("DNS server did not start")
        if self._bind_error:
            raise self._bind_error

    def serve_forever(self) -> None:
        """Bind and serve on this thread until SIGTERM, SIGINT, or :meth:`stop`."""
        self._install_signals()
        try:
            self._loop()
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self.stats.save()
        if self._bind_error:
            raise SystemExit(str(self._bind_error))

    def stop(self) -> None:
        self._stop.set()
        self._close_listen_sockets()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self._stop_status()
        self._executor.shutdown(wait=False, cancel_futures=True)
        reload_thread = self._reload_thread
        if reload_thread is not None and reload_thread is not threading.current_thread():
            reload_thread.join(timeout=3)
        self.stats.save()

    def details(self) -> dict:
        matcher = self._matcher
        return {
            "version": __version__,
            "listen": self.config.listen,
            "port": self.port,
            "upstream": list(self.config.upstream),
            "blocklist_domains": matcher.blocked_count,
            "allow_count": matcher.allow_count,
            "deny_count": matcher.deny_count,
            "cache_entries": self.cache.size,
        }

    def resolve_packet(self, data: bytes, client_ip: str) -> bytes | None:
        try:
            query = parse_message(data)
        except DnsError:
            self.stats.bump("errors")
            logger.debug("malformed query from %s", client_ip)
            return None
        if query.qr or not query.questions:
            self.stats.bump("errors")
            return None

        question = query.questions[0]
        name = question.name
        qtype = question.qtype
        qtype_label = type_name(qtype)
        self.stats.bump("queries")
        if query.opcode != 0:
            emit_lookup("FAIL", client_ip, name, qtype_label)
            return build_response(data, [], rcode=NOTIMP)

        matcher = self._matcher
        if question.qclass in (CLASS_IN, CLASS_ANY) and matcher.is_blocked(name):
            self.stats.bump("blocked")
            self.stats.note_blocked(name, qtype, client_ip)
            emit_lookup("BLOCKED", client_ip, name, qtype_label)
            return blocked_answer(
                data,
                qtype,
                ttl=self.config.block_ttl,
                ipv4=self.config.block_ipv4,
                ipv6=self.config.block_ipv6,
            )

        key = (name, qtype, question.qclass)
        cached = self.cache.get(key, query.id)
        if cached is not None:
            self.stats.bump("cached")
            emit_lookup("OK", client_ip, name, qtype_label)
            return cached

        try:
            result = exchange(
                data,
                list(self.config.upstream),
                self.config.upstream_port,
                self.config.upstream_timeout,
            )
        except (OSError, DnsError) as exc:
            self.stats.bump("errors")
            self.stats.note_upstream("socket")
            logger.warning("upstream socket %s for %s %s: %s", ",".join(self.config.upstream), name, qtype_label, exc)
            emit_lookup("FAIL", client_ip, name, qtype_label)
            return build_response(data, [], rcode=SERVFAIL)

        for attempt in result.attempts:
            self.stats.note_upstream(attempt.reason)
            logger.warning(
                "upstream %s %s for %s %s: %s",
                attempt.reason,
                attempt.host,
                name,
                qtype_label,
                attempt.detail,
            )
        if not result.ok:
            self.stats.bump("errors")
            logger.warning("upstream failed for %s %s: %s", name, qtype_label, result.summary())
            emit_lookup("FAIL", client_ip, name, qtype_label)
            if result.packet:
                return result.packet
            return build_response(data, [], rcode=SERVFAIL)
        response = result.packet

        self.stats.bump("forwarded")
        emit_lookup("OK", client_ip, name, qtype_label)
        ttl = cacheable_ttl(
            response,
            min_ttl=self.config.cache_min_ttl,
            max_ttl=self.config.cache_max_ttl,
            negative_ttl=self.config.cache_negative_ttl,
        )
        if ttl is not None:
            self.cache.store(key, response, ttl)
            adjusted = self.cache.get(key, query.id)
            if adjusted is not None:
                response = adjusted
        return response

    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        signal.signal(signal.SIGTERM, self._on_stop_signal)
        signal.signal(signal.SIGINT, self._on_stop_signal)
        signal.signal(signal.SIGHUP, self._on_hup_signal)

    def _on_stop_signal(self, _signum, _frame) -> None:
        self._stop.set()

    def _on_hup_signal(self, _signum, _frame) -> None:
        self._reload_flag = True

    def _loop(self) -> None:
        try:
            self._open_sockets()
        except OSError as exc:
            self._bind_error = exc
            logger.error("%s", exc)
            self._ready.set()
            return
        self._log_listen()
        self._start_status()
        self._start_updater()
        if self._load_error:
            # Leave the token empty so the poll keeps retrying the first load.
            self._mtime_token = ()
            self._reload_flag = True
        else:
            self._mtime_token = self._mtimes()
        self._ready.set()
        try:
            while not self._stop.is_set():
                self._maybe_reload()
                self._maybe_summary()
                self._maybe_save()
                if not self._sockets:
                    break
                try:
                    readable, _, _ = select.select(list(self._sockets), [], [], 0.5)
                except InterruptedError:
                    # SIGHUP arrives here. Keep the sockets open and reload on the next pass.
                    continue
                except (OSError, ValueError):
                    break
                for sock in readable:
                    if sock in self._tcp_socks:
                        self._accept(sock)
                    else:
                        self._read_udp(sock)
        finally:
            self._ready.set()
            self._close_listen_sockets()
            self._stop_status()

    def _open_sockets(self) -> None:
        port = self.config.port
        try:
            first = self._udp_socket(socket.AF_INET, self.config.listen, port)
        except PermissionError as exc:
            raise OSError(self._permission_message()) from exc
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise OSError(self._in_use_message()) from exc
            raise OSError(f"could not bind {self.config.listen}:{port}: {exc}") from exc
        self._sockets.append(first)
        self.port = first.getsockname()[1]
        if self.config.tcp:
            self._sockets.append(self._tcp_socket(socket.AF_INET, self.config.listen, self.port))
        v6_host = _ipv6_host(self.config.listen, self.config.ipv6)
        if v6_host:
            try:
                self._sockets.append(self._udp_socket(socket.AF_INET6, v6_host, self.port))
                if self.config.tcp:
                    self._sockets.append(self._tcp_socket(socket.AF_INET6, v6_host, self.port))
            except OSError as exc:
                logger.warning("IPv6 listen disabled: %s", exc)

    def _udp_socket(self, family: int, host: str, port: int) -> socket.socket:
        sock = socket.socket(family, socket.SOCK_DGRAM)
        self._prepare(sock, family)
        sock.bind((host, port))
        sock.setblocking(False)
        return sock

    def _tcp_socket(self, family: int, host: str, port: int) -> socket.socket:
        sock = socket.socket(family, socket.SOCK_STREAM)
        self._prepare(sock, family)
        sock.bind((host, port))
        sock.listen(64)
        sock.setblocking(False)
        self._tcp_socks.add(sock)
        return sock

    @staticmethod
    def _prepare(sock: socket.socket, family: int) -> None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)

    def _permission_message(self) -> str:
        return (
            f"could not bind {self.config.listen}:{self.config.port}: permission denied. "
            "Port 53 requires root. For a local test, set \"port\" above 1024."
        )

    def _in_use_message(self) -> str:
        lines = [f"could not bind port {self.config.port}: address already in use."]
        try:
            found = listeners_on(self.config.port or 53)
        except OSError:
            found = []
        if found:
            lines.append("Something is already listening:")
            lines.extend(f"  {describe(item)}" for item in found)
        lines.append('See the README section "Port 53 is already in use".')
        return "\n".join(lines)

    def _log_listen(self) -> None:
        matcher = self._matcher
        transport = "udp+tcp" if self.config.tcp else "udp"
        logger.info(
            "piadblock %s listening on %s:%s (%s), upstream %s",
            __version__,
            self.config.listen,
            self.port,
            transport,
            ", ".join(self.config.upstream),
        )
        logger.info(
            "lists: %s blocklist, %s allow, %s deny",
            matcher.blocked_count,
            matcher.allow_count,
            matcher.deny_count,
        )

    def _start_status(self) -> None:
        if not self.config.status_enabled:
            return
        try:
            page = StatusPage(
                self.config.status_listen,
                self.config.status_port,
                self.stats,
                self.details,
            )
            page.start()
        except OSError as exc:
            logger.warning("status page disabled: %s", exc)
            return
        self._status = page
        logger.info("status page http://%s:%s/", self.config.status_listen, page.port)

    def _start_updater(self) -> None:
        if not self.config.auto_update or not self.config.blocklists:
            return
        self._update_thread = threading.Thread(target=self._update_loop, name="piadblock-update", daemon=True)
        self._update_thread.start()

    def _update_loop(self) -> None:
        if self._stop.wait(5):
            return
        while not self._stop.is_set():
            try:
                result = update_blocklists(self.config, force=False)
                if result.ok and not result.skipped:
                    self._reload_flag = True
            except Exception:
                logger.exception("background blocklist update failed")
            if self._stop.wait(60):
                return

    def _maybe_reload(self) -> None:
        due = self._reload_flag
        self._reload_flag = False
        now = time.monotonic()
        if self.config.auto_reload and now - self._last_mtime_check >= 5:
            self._last_mtime_check = now
            token = self._mtimes()
            if token != self._mtime_token:
                due = True
        if due:
            self._start_reload()

    def _start_reload(self) -> None:
        """Parse on a side thread. The select loop keeps answering meanwhile."""
        with self._reload_lock:
            if self._reload_thread is not None and self._reload_thread.is_alive():
                self._reload_again = True
                return
            self._reload_again = False
            self._reload_thread = threading.Thread(
                target=self._reload_worker,
                name="piadblock-reload",
                daemon=True,
            )
            self._reload_thread.start()

    def _reload_worker(self) -> None:
        while not self._stop.is_set():
            try:
                self._reload_lists()
            except Exception:
                logger.exception("blocklist reload failed; keeping the lists already loaded")
            with self._reload_lock:
                if not self._reload_again:
                    self._reload_thread = None
                    return
                self._reload_again = False

    def _reload_lists(self) -> bool:
        """Build a new matcher and swap it in only when it is usable.

        Direct callers (tests) run this on their own thread. The server loop
        calls it from :meth:`_reload_worker`, so a long parse does not stall
        ``select``. The assignment of ``self._matcher`` is the publish point.
        """
        before = self._mtimes()
        report = Matcher.prepare(self.config.merged_path, self.config.allowlist, self.config.denylist)
        if report.error:
            self._note_reload_error(
                f"blocklist reload failed ({report.error}); keeping the lists already loaded"
            )
            return False
        incoming = report.matcher.blocked_count + report.matcher.deny_count
        current = self._matcher.blocked_count + self._matcher.deny_count
        if incoming == 0 and current > 0:
            self._note_reload_error(
                "blocklist reload blocked nothing; keeping the lists already loaded"
            )
            return False
        self._matcher = report.matcher
        if self._mtimes() == before:
            self._mtime_token = before
        if report.skipped:
            logger.warning("skipped %s malformed blocklist lines", report.skipped)
        logger.info(
            "lists reloaded: %s blocklist, %s allow, %s deny",
            report.matcher.blocked_count,
            report.matcher.allow_count,
            report.matcher.deny_count,
        )
        self._load_error = ""
        self._last_reload_error = ""
        return True

    def _note_reload_error(self, message: str) -> None:
        now = time.monotonic()
        if message != self._last_reload_error or now - self._last_reload_error_at >= 60:
            logger.error(message)
            self._last_reload_error = message
            self._last_reload_error_at = now

    def _mtimes(self) -> tuple:
        marks = []
        for path in (self.config.merged_path, self.config.allowlist, self.config.denylist):
            try:
                stat = Path(path).stat()
            except OSError:
                marks.append((path, None))
            else:
                marks.append((path, stat.st_mtime_ns))
        return tuple(marks)

    def _maybe_summary(self) -> None:
        now = time.monotonic()
        if now - self._last_summary < 60:
            return
        self._last_summary = now
        snap = self.stats.snapshot()
        if snap["queries"] == self._summary_queries:
            return
        self._summary_queries = snap["queries"]
        reasons = snap.get("upstream_errors") or {}
        logger.info(
            "stats queries=%s blocked=%s cached=%s forwarded=%s errors=%s "
            "timeout=%s servfail=%s refused=%s socket=%s truncation=%s",
            snap["queries"],
            snap["blocked"],
            snap["cached"],
            snap["forwarded"],
            snap["errors"],
            reasons.get("timeout", 0),
            reasons.get("servfail", 0),
            reasons.get("refused", 0),
            reasons.get("socket", 0),
            reasons.get("truncation", 0),
        )

    def _maybe_save(self) -> None:
        now = time.monotonic()
        if now - self._last_save < 1:
            return
        self._last_save = now
        self.stats.save()

    def _read_udp(self, sock: socket.socket) -> None:
        try:
            data, addr = sock.recvfrom(4096)
        except OSError:
            return
        if not self._slots.acquire(blocking=False):
            self._overload_udp(sock, data, addr)
            return
        try:
            self._executor.submit(self._udp_task, sock, data, addr)
        except RuntimeError:
            self._slots.release()

    def _udp_task(self, sock: socket.socket, data: bytes, addr) -> None:
        try:
            try:
                response = self.resolve_packet(data, addr[0])
            except Exception:
                logger.exception("udp query failed")
                self.stats.bump("errors")
                return
            if not response:
                return
            response = fit_udp(data, response)
            try:
                sock.sendto(response, addr)
            except OSError:
                self.stats.bump("errors")
        finally:
            self._slots.release()

    def _overload_udp(self, sock: socket.socket, data: bytes, addr) -> None:
        self.stats.bump("errors")
        try:
            response = build_response(data, [], rcode=SERVFAIL)
        except DnsError:
            return
        try:
            sock.sendto(response, addr)
        except OSError:
            return

    def _accept(self, sock: socket.socket) -> None:
        try:
            conn, addr = sock.accept()
        except OSError:
            return
        if not self._slots.acquire(blocking=False):
            conn.close()
            self.stats.bump("errors")
            return
        try:
            self._executor.submit(self._tcp_task, conn, addr)
        except RuntimeError:
            self._slots.release()
            conn.close()

    def _tcp_task(self, conn: socket.socket, addr) -> None:
        client = addr[0]
        try:
            conn.settimeout(self.config.upstream_timeout + 2)
            for _ in range(8):
                try:
                    header = recvall(conn, 2)
                except (EOFError, TimeoutError, socket.timeout, OSError, DnsError):
                    break
                length = struct.unpack("!H", header)[0]
                if length < 12 or length > 8192:
                    break
                try:
                    payload = recvall(conn, length)
                except (EOFError, TimeoutError, socket.timeout, OSError, DnsError):
                    break
                response = self.resolve_packet(payload, client)
                if not response:
                    break
                try:
                    conn.sendall(struct.pack("!H", len(response)) + response)
                except OSError:
                    break
        finally:
            try:
                conn.close()
            except OSError:
                pass
            self._slots.release()

    def _close_listen_sockets(self) -> None:
        for sock in list(self._sockets):
            try:
                sock.close()
            except OSError:
                pass
        self._sockets.clear()
        self._tcp_socks.clear()

    def _stop_status(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None


def _ipv6_host(listen: str, enabled: bool) -> str | None:
    if not enabled:
        return None
    if listen in ("0.0.0.0", "::", ""):
        return "::"
    if listen == "127.0.0.1":
        return "::1"
    return None
