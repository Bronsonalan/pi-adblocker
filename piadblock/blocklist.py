"""Parse hosts files, Adblock domain rules, and local allow/deny lists.

Matching is by suffix and most-specific wins. Listing ``ads.example.com``
blocks that name and ``a.ads.example.com``, but not ``example.com``. An
allowlist entry at the same name beats a block. A more specific denylist
entry beats a broader allow (``ads.good.com`` denied, ``good.com`` allowed).

A built-in essential-name list wins over ``blocklist.txt`` so a broad entry
cannot take down Google, Apple, GitHub, Cloudflare, or Microsoft. The
Private Relay hosts ``mask.icloud.com`` and ``mask-h2.icloud.com`` stay
blocked. TLDs and public suffixes are rejected. ``.local``, ``.lan``, and
private-address reverse lookups are never blocked.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from pathlib import Path

from piadblock.public_suffixes import is_public_suffix

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
_ASCII_DOMAIN = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-_*")

# Parents of services a home network needs. This wins over blocklist.txt,
# including a more specific name under the parent, except CARVE_OUT.
ESSENTIAL_ALLOW = frozenset(
    {
        "google.com",
        "dns.google",
        "gstatic.com",
        "googleapis.com",
        "googleusercontent.com",
        "ggpht.com",
        "gvt1.com",
        "gvt2.com",
        "youtube.com",
        "ytimg.com",
        "gmail.com",
        "1e100.net",
        "apple.com",
        "icloud.com",
        "cdn-apple.com",
        "mzstatic.com",
        "apple-cloudkit.com",
        "github.com",
        "githubusercontent.com",
        "cloudflare.com",
        "cloudflare-dns.com",
        "microsoft.com",
        "windows.com",
        "windowsupdate.com",
        "microsoftonline.com",
        "office.com",
        "office365.com",
        "live.com",
    }
)
# iCloud Private Relay stays blocked even though icloud.com is essential.
CARVE_OUT = frozenset({"mask.icloud.com", "mask-h2.icloud.com"})
_LOCAL_SUFFIXES = frozenset({"local", "lan", "home.arpa", "localhost", "localdomain"})
_PRIVATE_V4 = (
    ipaddress.IPv4Network("0.0.0.0/8"),
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("169.254.0.0/16"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
_PRIVATE_V6 = (
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("fe80::/10"),
    ipaddress.IPv6Network("::1/128"),
)


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


def _label_ok(label: str) -> bool:
    size = len(label)
    if size == 0 or size > 63:
        return False
    first = label[0]
    if not ("a" <= first <= "z" or "0" <= first <= "9" or first == "_"):
        return False
    for char in label[1:]:
        if not ("a" <= char <= "z" or "0" <= char <= "9" or char in "-_"):
            return False
    return True


def _ascii_domain(text: str, *, allow_single_label: bool) -> str | None:
    """Validate an already-lowercased ASCII name without the regex engine."""
    if text.startswith("*."):
        text = text[2:]
    if not text or len(text) > 253:
        return None
    labels = text.split(".")
    if any(not _label_ok(label) for label in labels):
        return None
    if len(labels) == 1 and not allow_single_label:
        return None
    if all(label.isdigit() for label in labels) and _is_ip(text):
        return None
    return text


def _normalize_name(name: str, *, allow_single_label: bool) -> str | None:
    text = name.strip().lower().rstrip(".")
    if not text:
        return None
    if all(char in _ASCII_DOMAIN for char in text):
        return _ascii_domain(text, allow_single_label=allow_single_label)
    return normalize_domain(text, allow_single_label=allow_single_label)


def parse_domain_list(
    text: str,
    *,
    allow_single_label: bool = False,
    skip_never_block: bool = False,
    reject_public_suffix: bool = False,
) -> set[str]:
    """Extract domains from a hosts file, an Adblock list, or a plain list."""
    domains, _skipped = parse_domain_list_report(
        text,
        allow_single_label=allow_single_label,
        skip_never_block=skip_never_block,
        reject_public_suffix=reject_public_suffix,
    )
    return domains


def parse_domain_list_report(
    text: str,
    *,
    allow_single_label: bool = False,
    skip_never_block: bool = False,
    reject_public_suffix: bool = False,
) -> tuple[set[str], int]:
    """Return ``(domains, skipped)``.

    ``skipped`` counts malformed lines and rejected public suffixes. Comments
    and ``@@`` exceptions are ignored and not counted. A bad line never aborts
    the rest of the file.

    Accepted line shapes:

    * ``0.0.0.0 ads.example.com`` (extra names on the same line are kept)
    * ``ads.example.com``
    * ``||ads.example.com^`` and ``||ads.example.com^$third-party``
    * ``*.ads.example.com`` (stored as ``ads.example.com``)
    """
    domains: set[str] = set()
    skipped = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "!", "[")):
            continue
        if line.startswith("@@"):
            continue
        names = _names_on_line(line)
        if not names:
            skipped += 1
            continue
        recognized = 0
        for name in names:
            domain = _normalize_name(name, allow_single_label=allow_single_label)
            if domain is None:
                continue
            recognized += 1
            if reject_public_suffix and is_public_suffix(domain):
                skipped += 1
                continue
            if skip_never_block and domain in NEVER_BLOCK:
                continue
            domains.add(domain)
        if recognized == 0:
            skipped += 1
    return domains, skipped


def _names_on_line(line: str) -> list[str]:
    if " " not in line and "\t" not in line and "|" not in line and "#" not in line and "/" not in line:
        return [line]
    adblock = _ADBLOCK_RE.match(line)
    if adblock:
        return [adblock.group(1)]
    if "#" in line:
        head, _comment = line.split("#", 1)
        if head.strip():
            line = head.strip()
    parts = line.split()
    if not parts:
        return []
    if _is_ip(parts[0]):
        return parts[1:]
    if len(parts) == 1:
        return parts
    return []


def _read_stable(path: Path) -> tuple[str, str]:
    """Read ``path``. Return ``(text, error)``. A changing file is an error."""
    try:
        first = path.stat()
    except OSError as exc:
        return "", f"could not read {path} ({exc})"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "", f"could not read {path} ({exc})"
    try:
        second = path.stat()
    except OSError as exc:
        return "", f"could not read {path} ({exc})"
    if first.st_size != second.st_size or first.st_mtime_ns != second.st_mtime_ns:
        return "", f"{path} changed while it was being read"
    return text, ""


def load_domain_file(
    path: str,
    *,
    allow_single_label: bool = False,
    skip_never_block: bool = False,
    reject_public_suffix: bool = False,
) -> tuple[set[str], int, str]:
    """Return ``(domains, skipped, error)``. ``error`` is empty on success."""
    if not path:
        return set(), 0, ""
    file_path = Path(path)
    if not file_path.is_file():
        return set(), 0, f"missing {path}"
    text, error = _read_stable(file_path)
    if error:
        return set(), 0, error
    domains, skipped = parse_domain_list_report(
        text,
        allow_single_label=allow_single_label,
        skip_never_block=skip_never_block,
        reject_public_suffix=reject_public_suffix,
    )
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        return set(), skipped, f"could not read {path} ({exc})"
    if size > 0 and not domains and skipped:
        return domains, skipped, f"{path} parsed to nothing ({skipped} lines skipped)"
    if size == 0:
        return domains, skipped, f"{path} is empty"
    return domains, skipped, ""


def _load_optional(path: str, **kwargs) -> tuple[set[str], int, str]:
    """An allow list or a downloaded list may be missing or empty."""
    if not path or not Path(path).is_file():
        return set(), 0, ""
    domains, skipped, error = load_domain_file(path, **kwargs)
    if error.endswith(" is empty"):
        return set(), skipped, ""
    return domains, skipped, error


def _matched_suffix(domain: str, names: frozenset[str]) -> str | None:
    labels = domain.split(".")
    for index in range(len(labels)):
        suffix = ".".join(labels[index:])
        if suffix in names:
            return suffix
    return None


def _is_local_name(domain: str) -> bool:
    return _matched_suffix(domain, _LOCAL_SUFFIXES) is not None


def _is_private_reverse(domain: str) -> bool:
    if domain.endswith(".in-addr.arpa"):
        return _private_v4_reverse(domain[: -len(".in-addr.arpa")])
    if domain.endswith(".ip6.arpa"):
        return _private_v6_reverse(domain[: -len(".ip6.arpa")])
    return False


def _private_v4_reverse(body: str) -> bool:
    labels = body.split(".")
    if not labels or len(labels) > 4:
        return False
    octets: list[int] = []
    for label in labels:
        if not label.isdigit():
            return False
        number = int(label)
        if number > 255:
            return False
        octets.append(number)
    octets.reverse()
    prefix = len(octets) * 8
    while len(octets) < 4:
        octets.append(0)
    network = ipaddress.IPv4Network((bytes(octets), prefix))
    return any(network.subnet_of(private) for private in _PRIVATE_V4)


def _private_v6_reverse(body: str) -> bool:
    labels = body.split(".")
    if not labels or len(labels) > 32:
        return False
    if any(len(label) != 1 or label not in "0123456789abcdef" for label in labels):
        return False
    # A partial zone of zeros covers public space as well as ::.
    if all(label == "0" for label in labels):
        return False
    nibbles = list(reversed(labels))
    padded = "".join(nibbles + ["0"] * (32 - len(nibbles)))
    text = ":".join(padded[index : index + 4] for index in range(0, 32, 4))
    network = ipaddress.IPv6Network(f"{text}/{len(labels) * 4}")
    return any(network.subnet_of(private) for private in _PRIVATE_V6)


@dataclass(frozen=True)
class Decision:
    blocked: bool
    reason: str


@dataclass(frozen=True)
class LoadReport:
    """Result of reading the three list files. ``error`` is empty on success."""

    matcher: Matcher
    skipped: int
    error: str


class Matcher:
    """Immutable view of the three lists. Reload by replacing the object."""

    def __init__(self, blocked: set[str], allowed: set[str], denied: set[str]):
        self.blocked = blocked
        self.allowed = allowed
        self.denied = denied

    @classmethod
    def prepare(cls, merged_path: str, allow_path: str, deny_path: str) -> LoadReport:
        """Read the lists. A bad denylist yields an empty matcher and an error.

        The caller keeps the previous matcher when ``error`` is set, or when
        the new matcher would block nothing and a previous list was in use.
        """
        blocked, skip_blocked, block_error = _load_optional(
            merged_path,
            skip_never_block=True,
            reject_public_suffix=True,
        )
        allowed, skip_allowed, allow_error = _load_optional(
            allow_path,
            allow_single_label=True,
        )
        if not deny_path:
            denied, skip_denied, deny_error = set(), 0, "denylist path is empty"
        else:
            denied, skip_denied, deny_error = load_domain_file(
                deny_path,
                allow_single_label=True,
                reject_public_suffix=True,
            )
        skipped = skip_blocked + skip_allowed + skip_denied
        error = deny_error or block_error or allow_error
        if error:
            return LoadReport(cls(set(), set(), set()), skipped, error)
        return LoadReport(cls(blocked, allowed, denied), skipped, "")

    @classmethod
    def load(cls, merged_path: str, allow_path: str, deny_path: str) -> Matcher:
        return cls.prepare(merged_path, allow_path, deny_path).matcher

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
        if _is_local_name(domain) or _is_private_reverse(domain):
            return Decision(False, "local name")
        # Private Relay stays on the block list. Essential allow does not cover it.
        if _matched_suffix(domain, CARVE_OUT):
            return self._listed(domain)
        essential = _matched_suffix(domain, ESSENTIAL_ALLOW)
        if essential:
            return Decision(False, f"essential ({essential})")
        return self._listed(domain)

    def _listed(self, domain: str) -> Decision:
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
