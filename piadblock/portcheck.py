"""Find which process is bound to a UDP or TCP port.

``ss`` is not required. ``/proc/net/udp`` and ``/proc/net/tcp`` are readable
on Raspberry Pi OS, and ``/proc/<pid>/fd`` names the socket inode. The
installer uses this to spot systemd-resolved and dnsmasq on port 53.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Listener:
    proto: str
    ip: str
    port: int
    inode: str
    pid: int | None = None
    process: str | None = None
    cmdline: str = ""


def parse_ipv4(hexip: str) -> str:
    raw = bytes.fromhex(hexip)
    return socket.inet_ntoa(raw[::-1])


def parse_ipv6(hexip: str) -> str:
    raw = bytes.fromhex(hexip)
    groups = [raw[index : index + 4][::-1] for index in range(0, 16, 4)]
    return socket.inet_ntop(socket.AF_INET6, b"".join(groups))


def parse_proc_net(text: str, port: int, *, proto: str) -> list[tuple[str, str]]:
    """Return ``(ip, inode)`` rows in ``/proc/net/{udp,tcp}`` text bound to ``port``.

    TCP rows are kept only when the socket is LISTEN (state ``0A``). UDP rows
    have no listen state; every socket bound to the port is returned.
    """
    found: list[tuple[str, str]] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 10:
            continue
        local = parts[1]
        if ":" not in local:
            continue
        address, port_hex = local.rsplit(":", 1)
        try:
            local_port = int(port_hex, 16)
        except ValueError:
            continue
        if local_port != port:
            continue
        if proto == "tcp":
            if parts[3] != "0A":
                continue
        try:
            ip = parse_ipv4(address) if len(address) == 8 else parse_ipv6(address)
        except (OSError, ValueError):
            ip = address
        found.append((ip, parts[9]))
    return found


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def _comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def processes_for_inodes(inodes: set[str]) -> dict[str, tuple[int, str, str]]:
    """Map a socket inode to ``(pid, comm, cmdline)``."""
    wanted = set(inodes)
    found: dict[str, tuple[int, str, str]] = {}
    if not wanted:
        return found
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        fd_dir = entry / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if not (target.startswith("socket:[") and target.endswith("]")):
                continue
            inode = target[len("socket:[") : -1]
            if inode in wanted and inode not in found:
                found[inode] = (pid, _comm(pid), _cmdline(pid))
                if len(found) == len(wanted):
                    return found
    return found


def listeners_on(port: int) -> list[Listener]:
    """Listeners bound to ``port`` across IPv4/IPv6 UDP and TCP."""
    specs = (
        ("/proc/net/udp", "udp"),
        ("/proc/net/udp6", "udp"),
        ("/proc/net/tcp", "tcp"),
        ("/proc/net/tcp6", "tcp"),
    )
    rows: list[tuple[str, str, str]] = []
    for path, proto in specs:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for ip, inode in parse_proc_net(text, port, proto=proto):
            rows.append((proto, ip, inode))
    owners = processes_for_inodes({inode for _proto, _ip, inode in rows})
    listeners = []
    for proto, ip, inode in rows:
        pid, process, cmdline = owners.get(inode, (None, None, ""))
        listeners.append(
            Listener(
                proto=proto,
                ip=ip,
                port=port,
                inode=inode,
                pid=pid,
                process=process,
                cmdline=cmdline or "",
            )
        )
    return listeners


def _blob(listener: Listener) -> str:
    return f"{listener.process or ''} {listener.cmdline}".lower()


def is_resolved(listener: Listener) -> bool:
    return "systemd-resolve" in _blob(listener)


def is_dnsmasq(listener: Listener) -> bool:
    return "dnsmasq" in _blob(listener)


def is_piadblock(listener: Listener) -> bool:
    text = _blob(listener)
    return "piadblock" in text or "-m piadblock" in text


def describe(listener: Listener) -> str:
    who = listener.process or "unknown"
    if listener.pid:
        who = f"{who} (pid {listener.pid})"
    return f"{listener.proto} {listener.ip}:{listener.port} {who}"
