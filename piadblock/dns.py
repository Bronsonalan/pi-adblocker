"""DNS packet codec.

This speaks enough of RFC 1035 to answer A/AAAA questions and to forward
everything else. Name compression is understood on the way in. Answers we
build point at the question name with the usual ``0xC00C`` pointer (the
question always starts at byte 12).
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass, field

DNS_HEADER = struct.Struct("!HHHHHH")
RR_HEAD = struct.Struct("!HHIH")

A = 1
NS = 2
CNAME = 5
SOA = 6
PTR = 12
MX = 15
TXT = 16
AAAA = 28
SRV = 33
OPT = 41
HTTPS = 65
ANY = 255

CLASS_IN = 1
CLASS_ANY = 255

NOERROR = 0
FORMERR = 1
SERVFAIL = 2
NXDOMAIN = 3
NOTIMP = 4
REFUSED = 5

MAX_NAME_LENGTH = 253
MAX_COMPRESSION_HOPS = 16
MAX_RECORDS = 128
UDP_PAYLOAD_MAX = 4096

TYPE_NAMES = {
    A: "A",
    NS: "NS",
    CNAME: "CNAME",
    SOA: "SOA",
    PTR: "PTR",
    MX: "MX",
    TXT: "TXT",
    AAAA: "AAAA",
    SRV: "SRV",
    OPT: "OPT",
    HTTPS: "HTTPS",
    ANY: "ANY",
}


class DnsError(Exception):
    """A packet is truncated, compressed into a loop, or otherwise unusable."""


@dataclass
class Question:
    name: str
    qtype: int
    qclass: int


@dataclass
class ResourceRecord:
    name: str
    rtype: int
    rclass: int
    ttl: int
    rdata: bytes


@dataclass
class DnsMessage:
    id: int
    flags: int
    questions: list[Question] = field(default_factory=list)
    answers: list[ResourceRecord] = field(default_factory=list)
    authorities: list[ResourceRecord] = field(default_factory=list)
    additionals: list[ResourceRecord] = field(default_factory=list)

    @property
    def qr(self) -> int:
        return (self.flags >> 15) & 1

    @property
    def opcode(self) -> int:
        return (self.flags >> 11) & 0xF

    @property
    def tc(self) -> int:
        return (self.flags >> 9) & 1

    @property
    def rd(self) -> int:
        return (self.flags >> 8) & 1

    @property
    def ra(self) -> int:
        return (self.flags >> 7) & 1

    @property
    def rcode(self) -> int:
        return self.flags & 0xF


def type_name(qtype: int) -> str:
    return TYPE_NAMES.get(qtype, str(qtype))


def decode_name(packet: bytes, offset: int) -> tuple[str, int]:
    """Return ``(name, offset_after_name)``.

    ``offset_after_name`` is the first byte after this name in the message.
    A compression pointer moves the read cursor but not that returned offset.
    """
    if offset < 0 or offset >= len(packet):
        raise DnsError("name starts outside the packet")

    labels: list[str] = []
    hops = 0
    pos = offset
    end = offset
    jumped = False

    while True:
        if pos >= len(packet):
            raise DnsError("truncated name")
        length = packet[pos]
        if length == 0:
            pos += 1
            if not jumped:
                end = pos
            break
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(packet):
                raise DnsError("truncated compression pointer")
            pointer = ((length & 0x3F) << 8) | packet[pos + 1]
            if pointer >= len(packet):
                raise DnsError("compression pointer out of range")
            hops += 1
            if hops > MAX_COMPRESSION_HOPS:
                raise DnsError("compression loop")
            if not jumped:
                end = pos + 2
            jumped = True
            pos = pointer
            continue
        if length & 0xC0:
            raise DnsError("unsupported label type")
        if length > 63:
            raise DnsError("label too long")
        pos += 1
        if pos + length > len(packet):
            raise DnsError("truncated label")
        try:
            label = packet[pos : pos + length].decode("ascii")
        except UnicodeDecodeError as exc:
            raise DnsError("non-ascii label") from exc
        labels.append(label.lower())
        pos += length
        if sum(len(item) + 1 for item in labels) > MAX_NAME_LENGTH:
            raise DnsError("name too long")

    return ".".join(labels), end


def encode_name(name: str) -> bytes:
    """Encode a presentation-format name. The root is ``\"\"`` or ``\".\"``."""
    if name in ("", "."):
        return b"\x00"
    labels = name.rstrip(".").split(".")
    if not labels or any(label == "" for label in labels):
        raise DnsError("empty label")
    out = bytearray()
    for label in labels:
        raw = label.encode("ascii")
        if not raw or len(raw) > 63:
            raise DnsError("bad label")
        out.append(len(raw))
        out += raw
    out.append(0)
    if len(out) > 255:
        raise DnsError("name too long")
    return bytes(out)


def _parse_question(packet: bytes, offset: int) -> tuple[Question, int]:
    name, offset = decode_name(packet, offset)
    if offset + 4 > len(packet):
        raise DnsError("truncated question")
    qtype, qclass = struct.unpack("!HH", packet[offset : offset + 4])
    return Question(name, qtype, qclass), offset + 4


def _parse_rr(packet: bytes, offset: int) -> tuple[ResourceRecord, int]:
    name, offset = decode_name(packet, offset)
    if offset + 10 > len(packet):
        raise DnsError("truncated record")
    rtype, rclass, ttl, rdlength = RR_HEAD.unpack(packet[offset : offset + 10])
    offset += 10
    if rdlength > len(packet) - offset:
        raise DnsError("truncated rdata")
    rdata = packet[offset : offset + rdlength]
    return ResourceRecord(name, rtype, rclass, ttl, rdata), offset + rdlength


def parse_message(packet: bytes) -> DnsMessage:
    if len(packet) < 12:
        raise DnsError("packet shorter than a DNS header")
    ident, flags, qdcount, ancount, nscount, arcount = DNS_HEADER.unpack(packet[:12])
    total = qdcount + ancount + nscount + arcount
    if total > MAX_RECORDS:
        raise DnsError("too many records")
    offset = 12
    questions = []
    for _ in range(qdcount):
        question, offset = _parse_question(packet, offset)
        questions.append(question)
    answers = []
    authorities = []
    additionals = []
    for bucket, count in (
        (answers, ancount),
        (authorities, nscount),
        (additionals, arcount),
    ):
        for _ in range(count):
            record, offset = _parse_rr(packet, offset)
            bucket.append(record)
    return DnsMessage(ident, flags, questions, answers, authorities, additionals)


def question_end(packet: bytes) -> int:
    """Byte offset just past the first question."""
    if len(packet) < 12:
        raise DnsError("packet shorter than a DNS header")
    qdcount = struct.unpack("!H", packet[4:6])[0]
    if qdcount < 1:
        raise DnsError("no question")
    _, end = _parse_question(packet, 12)
    return end


def make_flags(*, qr: int = 0, opcode: int = 0, aa: int = 0, tc: int = 0, rd: int = 0, ra: int = 0, rcode: int = 0) -> int:
    return (
        (qr & 1) << 15
        | (opcode & 0xF) << 11
        | (aa & 1) << 10
        | (tc & 1) << 9
        | (rd & 1) << 8
        | (ra & 1) << 7
        | (rcode & 0xF)
    )


def build_query(name: str, qtype: int, qid: int = 0x1234, rd: bool = True, edns_size: int | None = None) -> bytes:
    """Build a standard recursive query, optionally with one EDNS OPT record."""
    header = DNS_HEADER.pack(
        qid & 0xFFFF,
        make_flags(rd=1 if rd else 0),
        1,
        0,
        0,
        1 if edns_size else 0,
    )
    question = encode_name(name) + struct.pack("!HH", qtype, CLASS_IN)
    if not edns_size:
        return header + question
    # OPT: root name, type OPT, class = UDP payload size, ttl = extended flags, no data.
    opt = b"\x00" + RR_HEAD.pack(OPT, edns_size, 0, 0)
    return header + question + opt


def build_response(
    query: bytes,
    answers: list[tuple[int, int, bytes]],
    *,
    rcode: int = NOERROR,
    ra: bool = True,
    aa: bool = False,
    tc: bool = False,
) -> bytes:
    """Answer ``query`` with records whose name is a pointer to the question.

    Each answer is ``(rtype, ttl, rdata)``. The question bytes are copied
    unchanged, so the ``0xC00C`` pointer lands on the question name.
    """
    message = parse_message(query)
    if not message.questions:
        raise DnsError("cannot answer a packet with no question")
    end = question_end(query)
    flags = make_flags(
        qr=1,
        aa=1 if aa else 0,
        tc=1 if tc else 0,
        rd=message.rd,
        ra=1 if ra else 0,
        rcode=rcode,
    )
    header = DNS_HEADER.pack(message.id, flags, 1, len(answers), 0, 0)
    body = bytearray(header)
    body += query[12:end]
    for rtype, ttl, rdata in answers:
        body += b"\xc0\x0c"
        body += RR_HEAD.pack(rtype, CLASS_IN, ttl & 0xFFFFFFFF, len(rdata))
        body += rdata
    return bytes(body)


def blocked_answer(query: bytes, qtype: int, *, ttl: int, ipv4: str, ipv6: str) -> bytes:
    """Sink a blocked name: A -> IPv4 sink, AAAA -> IPv6 sink, anything else -> NODATA."""
    if qtype == A:
        answers = [(A, ttl, socket.inet_aton(ipv4))]
    elif qtype == AAAA:
        answers = [(AAAA, ttl, socket.inet_pton(socket.AF_INET6, ipv6))]
    elif qtype == ANY:
        answers = [
            (A, ttl, socket.inet_aton(ipv4)),
            (AAAA, ttl, socket.inet_pton(socket.AF_INET6, ipv6)),
        ]
    else:
        answers = []
    return build_response(query, answers, rcode=NOERROR, ra=True)


def with_id(packet: bytes, qid: int) -> bytes:
    if len(packet) < 2:
        return packet
    return struct.pack("!H", qid & 0xFFFF) + packet[2:]


def rr_ttl_offsets(packet: bytes) -> list[int]:
    """Byte offsets of every resource-record TTL, skipping EDNS OPT.

    OPT stores extended flags in the TTL field, so rewriting it would corrupt
    the message. Callers use the offsets to count a cached TTL down.
    """
    if len(packet) < 12:
        return []
    _ident, _flags, qdcount, ancount, nscount, arcount = DNS_HEADER.unpack(packet[:12])
    offset = 12
    for _ in range(qdcount):
        _, offset = decode_name(packet, offset)
        offset += 4
    offsets = []
    for _ in range(ancount + nscount + arcount):
        _, offset = decode_name(packet, offset)
        if offset + 10 > len(packet):
            break
        rtype, _rclass, _ttl, rdlength = RR_HEAD.unpack(packet[offset : offset + 10])
        if rtype != OPT:
            offsets.append(offset + 4)
        offset += 10 + rdlength
    return offsets


def advertised_udp_size(packet: bytes) -> int:
    """Client UDP buffer from EDNS, clamped to what we are willing to send."""
    try:
        message = parse_message(packet)
    except DnsError:
        return 512
    size = 512
    for record in message.additionals:
        if record.rtype == OPT and record.rclass >= 512:
            size = record.rclass
            break
    return max(512, min(size, UDP_PAYLOAD_MAX))


def fit_udp(query: bytes, response: bytes) -> bytes:
    """Truncate to the question and set TC when the client cannot hold ``response``.

    The client is expected to retry the same question over TCP, which has no
    512-byte ceiling. The rcode of the original response is preserved.
    """
    if len(response) <= advertised_udp_size(query):
        return response
    rcode = NOERROR
    try:
        rcode = parse_message(response).rcode
    except DnsError:
        pass
    try:
        return build_response(query, [], rcode=rcode, ra=True, tc=True)
    except DnsError:
        return response


def rdata_ip(record: ResourceRecord) -> str | None:
    try:
        if record.rtype == A and len(record.rdata) == 4:
            return socket.inet_ntoa(record.rdata)
        if record.rtype == AAAA and len(record.rdata) == 16:
            return socket.inet_ntop(socket.AF_INET6, record.rdata)
    except OSError:
        return None
    return None


def cacheable_ttl(packet: bytes, *, min_ttl: int, max_ttl: int, negative_ttl: int) -> int | None:
    """Seconds to keep ``packet``, or ``None`` when it must not be cached.

    SERVFAIL and truncated messages are not cached. NODATA and NXDOMAIN use
    the SOA TTL when one is present, otherwise ``negative_ttl``. The result
    is clamped to ``[min_ttl, max_ttl]`` (a ``max_ttl`` of 0 disables the cache).
    """
    if max_ttl <= 0:
        return None
    try:
        message = parse_message(packet)
    except DnsError:
        return None
    if not message.qr or message.tc:
        return None
    if message.rcode in (FORMERR, SERVFAIL, NOTIMP, REFUSED):
        return None
    ttls = [record.ttl for record in message.answers if record.rtype != OPT]
    if not ttls:
        ttls = [record.ttl for record in message.authorities if record.rtype != OPT]
    ttl = min(ttls) if ttls else negative_ttl
    if ttl <= 0:
        return None
    if min_ttl:
        ttl = max(ttl, min_ttl)
    ttl = min(ttl, max_ttl)
    if ttl <= 0:
        return None
    return ttl
