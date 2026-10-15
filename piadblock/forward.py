"""Send one DNS query to an upstream resolver and return the response bytes."""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

from piadblock.dns import A, REFUSED, SERVFAIL, DnsError, DnsMessage, build_query, parse_message

# Reasons recorded on each attempt. ``truncation`` is the UDP TC bit followed
# by a TCP retry, which is not itself a client-visible failure.
UPSTREAM_REASONS = ("timeout", "servfail", "refused", "socket", "truncation")

_MAX_TCP = 8192


def recvall(sock: socket.socket, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            if not buf:
                raise EOFError("connection closed")
            raise DnsError("short TCP read")
        buf += chunk
    return bytes(buf)


def _family(host: str) -> int:
    try:
        socket.inet_pton(socket.AF_INET, host)
        return socket.AF_INET
    except OSError:
        socket.inet_pton(socket.AF_INET6, host)
        return socket.AF_INET6


def udp_exchange(host: str, port: int, query: bytes, timeout: float) -> bytes:
    sock = socket.socket(_family(host), socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.send(query)
        data = sock.recv(65535)
    finally:
        sock.close()
    if len(data) < 12:
        raise DnsError(f"short UDP response from {host}")
    if data[:2] != query[:2]:
        raise DnsError(f"response id mismatch from {host}")
    return data


def tcp_exchange(host: str, port: int, query: bytes, timeout: float) -> bytes:
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(struct.pack("!H", len(query)) + query)
        length = struct.unpack("!H", recvall(sock, 2))[0]
        if length < 12 or length > 65535:
            raise DnsError(f"bad TCP length from {host}")
        data = recvall(sock, length)
    finally:
        sock.close()
    if data[:2] != query[:2]:
        raise DnsError(f"response id mismatch from {host}")
    return data


def _transport_reason(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    return "socket"


def _attempt_detail(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


@dataclass(frozen=True)
class UpstreamAttempt:
    """One try against one upstream. ``reason`` is a member of :data:`UPSTREAM_REASONS`."""

    host: str
    reason: str
    detail: str


@dataclass(frozen=True)
class UpstreamExchange:
    """The packet to give the client, plus every attempt that produced it.

    ``packet`` is ``None`` when every upstream failed at the transport layer.
    ``ok`` is true when ``packet`` is an answer the client can use, including
    NXDOMAIN. SERVFAIL and REFUSED are not ok: the caller already tried the
    next upstream, and this packet is only what is left to return.
    """

    packet: bytes | None
    attempts: tuple[UpstreamAttempt, ...]

    @property
    def ok(self) -> bool:
        if self.packet is None:
            return False
        try:
            rcode = parse_message(self.packet).rcode
        except DnsError:
            return False
        return rcode not in (SERVFAIL, REFUSED)

    def summary(self) -> str:
        if not self.attempts:
            return "no upstream"
        return "; ".join(f"{item.host} {item.reason}: {item.detail}" for item in self.attempts)


def exchange(query: bytes, upstreams: list[str], port: int, timeout: float) -> UpstreamExchange:
    """Try each upstream until one returns a usable answer.

    A timeout, socket error, SERVFAIL, or REFUSED moves on to the next
    server immediately. One slow upstream therefore costs a single timeout,
    and the client still gets the next server's answer. Truncated UDP is
    retried over TCP on that same server before the next upstream is tried.
    When every server fails, the last SERVFAIL or REFUSED packet is returned
    so the client sees that rcode. When every failure was a transport error,
    ``packet`` is ``None``.
    """
    if not upstreams:
        raise DnsError("no upstream resolver configured")
    attempts: list[UpstreamAttempt] = []
    last_negative: bytes | None = None
    for host in upstreams:
        try:
            data = udp_exchange(host, port, query, timeout)
        except (OSError, DnsError, EOFError) as exc:
            attempts.append(UpstreamAttempt(host, _transport_reason(exc), _attempt_detail(exc)))
            continue
        try:
            truncated = bool(parse_message(data).tc)
        except DnsError as exc:
            attempts.append(UpstreamAttempt(host, "socket", _attempt_detail(exc)))
            continue
        if truncated:
            attempts.append(UpstreamAttempt(host, "truncation", "TC bit set; retrying over TCP"))
            try:
                data = tcp_exchange(host, port, query, timeout)
            except (OSError, DnsError, EOFError) as exc:
                attempts.append(UpstreamAttempt(host, _transport_reason(exc), _attempt_detail(exc)))
                continue
        try:
            rcode = parse_message(data).rcode
        except DnsError as exc:
            attempts.append(UpstreamAttempt(host, "socket", _attempt_detail(exc)))
            continue
        if rcode == SERVFAIL:
            attempts.append(UpstreamAttempt(host, "servfail", "SERVFAIL"))
            last_negative = data
            continue
        if rcode == REFUSED:
            attempts.append(UpstreamAttempt(host, "refused", "REFUSED"))
            last_negative = data
            continue
        return UpstreamExchange(data, tuple(attempts))
    return UpstreamExchange(last_negative, tuple(attempts))


def lookup(server: str, port: int, name: str, qtype: int = A, timeout: float = 2.0) -> DnsMessage:
    """Convenience client used by the installer self-check and by tests."""
    query = build_query(name, qtype)
    data = udp_exchange(server, port, query, timeout)
    return parse_message(data)
