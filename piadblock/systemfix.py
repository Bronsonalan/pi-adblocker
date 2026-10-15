"""Decide how the installer should free port 53 and keep the Pi's own DNS working.

These functions only look at text. The installer applies the result, and
uninstall reverses it from the state file.
"""

from __future__ import annotations

import ipaddress

from piadblock.paths import DHCPCD_BEGIN, DHCPCD_END
from piadblock.portcheck import Listener, is_dnsmasq, is_piadblock, is_resolved


def nameservers(resolv_text: str) -> list[str]:
    found = []
    for line in resolv_text.splitlines():
        line = line.split("#", 1)[0].strip()
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            found.append(parts[1])
    return found


def is_non_public(ip: str) -> bool:
    """True for loopback, private, link-local, and anything we should not leave the Pi using."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return True
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_unspecified
        or addr.is_multicast
        or addr.is_reserved
    )


def resolver_should_be_replaced(resolv_text: str) -> bool:
    """Replace resolv.conf when it points at ourselves, the router, or nowhere.

    A Pi that already uses a public resolver (1.1.1.1, 8.8.8.8, ...) is left
    alone. A Pi that uses 127.0.0.53 or the router's 192.168.x.x address would
    loop once the router sends the rest of the network to this Pi, so that
    file is replaced with the configured upstream.
    """
    servers = nameservers(resolv_text)
    if not servers:
        return True
    return any(is_non_public(server) for server in servers)


def piadblock_owns_resolv(resolv_text: str) -> bool:
    """True when this installer wrote the file and may refresh it."""
    return "# Written by piadblock." in resolv_text


def render_resolv_conf(upstreams: list[str]) -> str:
    lines = [
        "# Written by piadblock.",
        "# This Pi resolves directly through the upstream so blocklist updates",
        "# still work when piadblock is the DNS server for the rest of the LAN.",
        "# The previous file is recorded in /var/lib/piadblock/install-state.json.",
        "# timeout:1 attempts:1. glibc's default is 5 seconds times 2 attempts,",
        "# which is about 10 seconds when a nameserver does not answer.",
    ]
    for upstream in upstreams:
        lines.append(f"nameserver {upstream}")
    lines.append("options timeout:1 attempts:1 rotate edns0")
    return "\n".join(lines) + "\n"


def render_resolved_dropin(upstreams: list[str]) -> str:
    dns = " ".join(upstreams)
    return (
        "# Written by piadblock. Removed on uninstall.\n"
        "[Resolve]\n"
        "DNSStubListener=no\n"
        f"DNS={dns}\n"
        "FallbackDNS=\n"
    )


def render_dhcpcd_block(upstreams: list[str]) -> str:
    servers = " ".join(upstreams)
    return f"{DHCPCD_BEGIN}\nstatic domain_name_servers={servers}\n{DHCPCD_END}\n"


def render_nm_conf() -> str:
    return (
        "# Written by piadblock so NetworkManager does not overwrite /etc/resolv.conf.\n"
        "[main]\n"
        "dns=none\n"
    )


def upsert_marked_block(text: str, block: str, begin: str = DHCPCD_BEGIN, end: str = DHCPCD_END) -> str:
    """Insert ``block`` once, replacing a previous marked copy."""
    stripped = remove_marked_block(text, begin, end).rstrip()
    if stripped:
        stripped += "\n"
    if not block.endswith("\n"):
        block += "\n"
    return stripped + "\n" + block if stripped else block


def remove_marked_block(text: str, begin: str = DHCPCD_BEGIN, end: str = DHCPCD_END) -> str:
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    for line in lines:
        token = line.strip()
        if token == begin:
            skipping = True
            continue
        if skipping and token == end:
            skipping = False
            continue
        if not skipping:
            kept.append(line)
    return "".join(kept)


def classify(listeners: list[Listener]) -> dict[str, list[Listener]]:
    """Split port-53 listeners into ours / resolved / dnsmasq / other."""
    groups: dict[str, list[Listener]] = {
        "ours": [],
        "resolved": [],
        "dnsmasq": [],
        "other": [],
    }
    for listener in listeners:
        if is_piadblock(listener):
            groups["ours"].append(listener)
        elif is_resolved(listener):
            groups["resolved"].append(listener)
        elif is_dnsmasq(listener):
            groups["dnsmasq"].append(listener)
        else:
            groups["other"].append(listener)
    return groups
