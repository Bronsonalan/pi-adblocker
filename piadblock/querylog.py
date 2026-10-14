"""One-line query log written to stdout so journald records every lookup."""

from __future__ import annotations

import sys


def format_lookup(status: str, client: str, name: str, qtype: str) -> str:
    """``BLOCKED 192.168.1.10 doubleclick.net A`` or ``OK 192.168.1.10 example.com A``."""
    return f"{status} {client} {name} {qtype}"


def emit_lookup(status: str, client: str, name: str, qtype: str) -> None:
    """Print one lookup. ``flush`` so systemd's journal sees it immediately."""
    print(format_lookup(status, client, name, qtype), file=sys.stdout, flush=True)
