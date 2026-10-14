"""Parse hosts files, Adblock domain rules, and local allow/deny lists.

Matching is by suffix and most-specific wins. Listing ``ads.example.com``
blocks that name and ``a.ads.example.com``, but not ``example.com``. An
allowlist entry at the same name beats a block. A more specific denylist
entry beats a broader allow (``ads.good.com`` denied, ``good.com`` allowed).
"""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from pathlib import Path

# Hosts files ship loopback names next to the ads. Never import those from a
# blocklist. A user denylist can still name them on purpose.
NEVER_BLOCK = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "local",
        "localdomain",
        "broadcasthost",
        "ip6-localhost",
        "ip6-loopback",
        "ip6-localnet",
        "ip6-mcastprefix",
        "ip6-allnodes",
        "ip6-allrouters",
        "ip6-allhosts",
    }
)

_LABEL = r"[a-z0-9_](?:[a-z0-9_-]{0,62})"
_DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+{_LABEL}$")
_SINGLE_RE = re.compile(rf"^{_LABEL}$")
_ADBLOCK_RE = re.compile(r"^\|\|([^\^/\s]+)\^(?:\$\S+)?$")


def _is_ip(token: str) -> bool:
    host = token.split("%", 1)[0]
    if not host:
        return False
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return True
        except OSError:
            continue
    # Zone indexes and slightly odd IPv6 tokens still mean "this is a hosts line".
    return ":" in token


def normalize_domain(name: str, *, allow_single_label: bool = False) -> str | None:
    """Return a canonical domain, or ``None`` when ``name`` should be ignored."""
    text = name.strip().lower().rstrip(".")
    if text.startswith("*."):
        text = text[2:]
    if not text or any(sep in text for sep in (" ", "/", "\\", "*", "?", "#")):
        return None
    try:
        text = text.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if _is_ip(text):
        return None
    if _DOMAIN_RE.match(text):
        if len(text) > 253:
            return None
        return text
    if allow_single_label and _SINGLE_RE.match(text):
        return text
    return None


def parse_domain_list(
    text: str,
    *,
    allow_single_label: bool = False,
    skip_never_block: bool = False,
) -> set[str]:
    """Extract domains from a hosts file, an Adblock list, or a plain list.

    Accepted line shapes:

    * ``0.0.0.0 ads.example.com`` (extra names on the same line are kept)
    * ``ads.example.com``
    * ``||ads.example.com^`` and ``||ads.example.com^$third-party``
    * ``*.ads.example.com`` (stored as ``ads.example.com``)

    ``@@||`` exceptions, comments, and path rules are skipped. DNS cannot
    express a URL path, so those rules are not approximated.
    """
    domains: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "!", "[")):
            continue
        if line.startswith("@@"):
            continue

        names: list[str] = []
        adblock = _ADBLOCK_RE.match(line)
        if adblock:
            names = [adblock.group(1)]
        else:
            # Strip a trailing inline comment on hosts lines ("0.0.0.0 host # why").
            if "#" in line:
                head, _comment = line.split("#", 1)
                if head.strip():
                    line = head.strip()
            parts = line.split()
            if not parts:
                continue
            if _is_ip(parts[0]):
                names = parts[1:]
            elif len(parts) == 1:
                names = parts
            else:
                continue

        for name in names:
            domain = normalize_domain(name, allow_single_label=allow_single_label)
            if domain is None:
                continue
            if skip_never_block and domain in NEVER_BLOCK:
                continue
            domains.add(domain)
    return domains


def load_domain_file(path: str, *, allow_single_label: bool = False, skip_never_block: bool = False) -> set[str]:
    if not path:
        return set()
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return parse_domain_list(
        text,
        allow_single_label=allow_single_label,
        skip_never_block=skip_never_block,
    )


@dataclass(frozen=True)
class Decision:
    blocked: bool
    reason: str


class Matcher:
    """Immutable view of the three lists. Reload by replacing the object."""

    def __init__(self, blocked: set[str], allowed: set[str], denied: set[str]):
        self.blocked = blocked
        self.allowed = allowed
        self.denied = denied

    @classmethod
    def load(cls, merged_path: str, allow_path: str, deny_path: str) -> Matcher:
        return cls(
            load_domain_file(merged_path, skip_never_block=True),
            load_domain_file(allow_path, allow_single_label=True),
            load_domain_file(deny_path, allow_single_label=True),
        )

    @property
    def blocked_count(self) -> int:
        return len(self.blocked)

    @property
    def allow_count(self) -> int:
        return len(self.allowed)

    @property
    def deny_count(self) -> int:
        return len(self.denied)

    def decide(self, name: str) -> Decision:
        domain = normalize_domain(name, allow_single_label=True)
        if not domain:
            return Decision(False, "not a domain")
        labels = domain.split(".")
        for index in range(len(labels)):
            suffix = ".".join(labels[index:])
            if suffix in self.allowed:
                return Decision(False, f"allowlist ({suffix})")
            if suffix in self.denied:
                return Decision(True, f"denylist ({suffix})")
            if suffix in self.blocked:
                return Decision(True, f"blocklist ({suffix})")
        return Decision(False, "not listed")

    def is_blocked(self, name: str) -> bool:
        return self.decide(name).blocked
