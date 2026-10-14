"""Small LRU cache of DNS response packets, with TTLs that count down."""

from __future__ import annotations

import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from piadblock.dns import DnsError, rr_ttl_offsets, with_id


@dataclass
class _Entry:
    packet: bytes
    expires: float
    ttl_offsets: tuple[int, ...]


class DnsCache:
    """Cache full response packets.

    ``get`` rewrites the transaction id and every non-OPT TTL so a later
    client sees the time left, not the TTL from the moment we stored it.
    The clock is injectable so tests can expire entries without sleeping.
    """

    def __init__(
        self,
        max_size: int,
        min_ttl: int,
        max_ttl: int,
        clock=time.monotonic,
    ):
        self.max_size = max_size
        self.min_ttl = min_ttl
        self.max_ttl = max_ttl
        self._clock = clock
        self._data: OrderedDict[tuple, _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    @property
    def size(self) -> int:
        return len(self)

    def get(self, key: tuple, qid: int) -> bytes | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            now = self._clock()
            if now >= entry.expires:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            packet = bytearray(entry.packet)
            remaining = max(1, int(entry.expires - now))
            offsets = entry.ttl_offsets
        packet[:2] = with_id(bytes(packet), qid)[:2]
        for offset in offsets:
            if 0 <= offset <= len(packet) - 4:
                struct.pack_into("!I", packet, offset, remaining)
        return bytes(packet)

    def store(self, key: tuple, packet: bytes, ttl: int) -> None:
        if self.max_size <= 0 or ttl <= 0 or len(packet) < 12:
            return
        # QR is the high bit of the third header byte.
        if not (packet[2] & 0x80):
            return
        effective = ttl
        if self.min_ttl:
            effective = max(effective, self.min_ttl)
        if self.max_ttl:
            effective = min(effective, self.max_ttl)
        if effective <= 0:
            return
        try:
            offsets = tuple(rr_ttl_offsets(packet))
        except DnsError:
            offsets = ()
        now = self._clock()
        entry = _Entry(bytes(packet), now + effective, offsets)
        with self._lock:
            self._data[key] = entry
            self._data.move_to_end(key)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
