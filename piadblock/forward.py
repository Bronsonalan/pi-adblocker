"""Send one DNS query to an upstream resolver and return the response bytes."""

from __future__ import annotations

import socket
import struct

from piadblock.dns import A, DnsError, DnsMessage, build_query, parse_message

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


def query_upstream(host: str, port: int, query: bytes, timeout: float) -> bytes:
    """UDP first. If the answer is truncated, ask the same server over TCP."""
    data = udp_exchange(host, port, query, timeout)
    try:
        truncated = bool(parse_message(data).tc)
    except DnsError:
        truncated = False
    if not truncated:
        return data
    return tcp_exchange(host, port, query, timeout)


def exchange(query: bytes, upstreams: list[str], port: int, timeout: float) -> bytes:
    """Try each upstream. A SERVFAIL is skipped when another server can answer.

    When every upstream returns SERVFAIL, that packet is returned so the
    client sees the same rcode. Transport failures raise :class:`DnsError`.
    """
    if not upstreams:
        raise DnsError("no upstream resolver configured")
    errors: list[str] = []
    last_servfail: bytes | None = None
    for host in upstreams:
        try:
            data = query_upstream(host, port, query, timeout)
        except (OSError, DnsError, EOFError) as exc:
            errors.append(f"{host}: {exc}")
            continue
        try:
            rcode = parse_message(data).rcode
        except DnsError as exc:
            errors.append(f"{host}: {exc}")
            continue
        if rcode == 2:  # SERVFAIL
            last_servfail = data
            errors.append(f"{host}: SERVFAIL")
            continue
        return data
    if last_servfail is not None and len(errors) == len(upstreams):
        return last_servfail
    raise DnsError("; ".join(errors) or "no upstream")


def lookup(server: str, port: int, name: str, qtype: int = A, timeout: float = 2.0) -> DnsMessage:
    """Convenience client used by the installer self-check and by tests."""
    query = build_query(name, qtype)
    data = udp_exchange(server, port, query, timeout)
    return parse_message(data)
