"""Fullscreen lookup feed for a 640x480 console.

The DNS process only appends to :class:`LookupFeed`. This module draws the
screen. A slow tty therefore cannot stall a query. The layout is 40 columns
by 15 rows, which is a 16x32 font on a 640x480 CRT. A larger console uses
its own size and the same formatting.
"""

from __future__ import annotations

import json
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

# 640 / 16 and 480 / 32. Terminus 32x16 fills a 640x480 CRT with these cells.
CRT_WIDTH = 40
CRT_HEIGHT = 15

BLOCKED_STYLE = "1;37;41"
OK_STYLE = "32"
FAIL_STYLE = "1;33"
HEADER_STYLE = "1;32"

# Largest first. setfont on the Pi console picks the first file that exists.
FONT_NAMES = (
    "Lat15-Terminus32x16.psf.gz",
    "Uni3-Terminus32x16.psf.gz",
    "Lat15-Terminus32x16.psf",
    "Lat15-Terminus28x14.psf.gz",
    "Lat15-Terminus24x12.psf.gz",
    "Lat15-Terminus20x10.psf.gz",
    "Lat15-Terminus16x8.psf.gz",
)
FONT_DIRS = ("/usr/share/consolefonts", "/usr/share/kbd/consolefonts")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class LookupFeed:
    """Newest-first ring of lookups. ``note`` does no I/O."""

    def __init__(self, limit: int = 64):
        self._lock = threading.Lock()
        self._items: deque[dict] = deque(maxlen=limit)

    def note(self, status: str, client: str, name: str, qtype: str) -> None:
        item = {
            "ts": time.time(),
            "status": status,
            "client": client,
            "name": name,
            "qtype": qtype,
        }
        with self._lock:
            self._items.appendleft(item)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._items)


def format_local_time(ts: float) -> str:
    """Local wall clock as ``h:MM:SS AM/PM``.

    ``time.localtime`` uses the Pi's configured timezone. This does not
    import ``zoneinfo``, so a missing tzdata package cannot change the result.
    """
    local = time.localtime(ts)
    hour = local.tm_hour
    suffix = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{local.tm_min:02d}:{local.tm_sec:02d} {suffix}"


def visible_text(line: str) -> str:
    """Drop ANSI color sequences. The result is what the CRT actually shows."""
    return _ANSI.sub("", line)


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def choose_font(directories: list[str] | None = None) -> str:
    """Return a large console font path, or ``""`` when none is installed."""
    roots = directories if directories is not None else list(FONT_DIRS)
    for name in FONT_NAMES:
        for root in roots:
            path = Path(root) / name
            if path.is_file():
                return str(path)
    return ""


def prepare_console(tty_path: str, font: str = "") -> str:
    """Load a large font on ``tty_path``. Missing setfont or font is skipped."""
    chosen = font or choose_font()
    if not chosen or not str(tty_path).startswith("/dev/"):
        return ""
    if shutil.which("setfont") is None:
        return ""
    subprocess.run(
        ["setfont", "-C", tty_path, chosen],
        check=False,
        capture_output=True,
        text=True,
    )
    return chosen


def style_for(status: str) -> str:
    if status == "BLOCKED":
        return BLOCKED_STYLE
    if status == "FAIL":
        return FAIL_STYLE
    return OK_STYLE


def compose_lookup(status: str, client: str, name: str, qtype: str, width: int) -> str:
    """One row: status, client, domain, type. Long fields shrink from the right."""
    label = status if status in {"BLOCKED", "FAIL", "OK"} else (status or "OK")
    prefix = f"{label[:7]:<7} "
    client = client or "-"
    name = name or "-"
    qtype = qtype or ""
    rest = width - len(prefix)
    if rest <= 0:
        return truncate(prefix.rstrip(), width)
    full = f"{client} {name}" + (f" {qtype}" if qtype else "")
    if len(full) <= rest:
        return prefix + full
    without_type = f"{client} {name}"
    if len(without_type) <= rest:
        return prefix + without_type
    # Keep the start of the address and the start of the domain.
    client_max = min(len(client), max(1, rest - 5))
    client_shown = truncate(client, client_max)
    name_width = rest - len(client_shown) - 1
    name_shown = truncate(name, max(0, name_width))
    if name_shown:
        text = f"{prefix}{client_shown} {name_shown}"
    else:
        text = f"{prefix}{client_shown}"
    return truncate(text.rstrip(), width)


def _fit_sides(left: str, right: str, width: int) -> str:
    gap = width - len(left) - len(right)
    if gap >= 1:
        return left + (" " * gap) + right
    return truncate(f"{left} {right}".strip(), width)


def _title_line(clock: str, width: int, blocklist_domains: int | None) -> str:
    left = "PI AD BLOCK"
    if blocklist_domains is None:
        return _fit_sides(left, clock, width)
    count = f"{int(blocklist_domains):,}"
    gap = width - len(left) - len(count) - len(clock)
    if gap < 2:
        return _fit_sides(left, clock, width)
    left_gap = gap // 2
    right_gap = gap - left_gap
    return left + (" " * left_gap) + count + (" " * right_gap) + clock


def paint(text: str, width: int, style: str) -> str:
    shown = truncate(text, width)
    padded = shown + (" " * (width - len(shown)))
    if not style:
        return padded
    return f"\x1b[{style}m{padded}\x1b[0m"


def render_frame(
    lookups: list,
    queries: int,
    blocked: int,
    *,
    width: int = CRT_WIDTH,
    height: int = CRT_HEIGHT,
    clock: str = "",
    waiting: bool = False,
    blocklist_domains: int | None = None,
) -> str:
    """Return one full screen. Newest lookup is the first row under the header.

    Every row is exactly ``width`` visible columns, and there are ``height``
    rows. Blocked rows use a red bar and the word BLOCKED.
    """
    width = max(1, width)
    height = max(1, height)
    when = clock or format_local_time(time.time())
    title = _title_line(when, width, None if waiting else blocklist_domains)
    totals = truncate(f"TODAY {int(queries)} Q  {int(blocked)} BLOCKED", width)
    rows = [
        paint(title, width, HEADER_STYLE),
        paint(totals, width, HEADER_STYLE),
        paint("=" * width, width, OK_STYLE),
    ]
    if waiting and len(rows) < height:
        rows.append(paint("WAITING FOR PIADBLOCK", width, FAIL_STYLE))
    for item in lookups:
        if len(rows) >= height:
            break
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        text = compose_lookup(
            status,
            str(item.get("client") or ""),
            str(item.get("name") or ""),
            str(item.get("qtype") or ""),
            width,
        )
        rows.append(paint(text, width, style_for(status)))
    while len(rows) < height:
        rows.append(paint("", width, ""))
    body = "\r\n".join(rows[:height])
    return "\x1b[H" + body + "\x1b[J"


def fetch_crt(url: str, timeout: float = 0.4) -> dict | None:
    """GET the status JSON. A down server returns None so the screen can wait."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def tty_size(stream) -> tuple[int, int]:
    """Return ``(columns, rows)`` or the 640x480 cell size when unknown."""
    try:
        import fcntl
        import struct
        import termios

        packed = fcntl.ioctl(stream.fileno(), termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols, _xp, _yp = struct.unpack("HHHH", packed)
    except (OSError, AttributeError, ImportError, ValueError):
        return CRT_WIDTH, CRT_HEIGHT
    if rows <= 0 or cols <= 0:
        return CRT_WIDTH, CRT_HEIGHT
    return cols, rows


def _configure_output(stream) -> None:
    """Write CR LF ourselves so a newline cannot depend on the tty's ONLCR bit."""
    try:
        import termios

        attrs = termios.tcgetattr(stream.fileno())
        attrs[1] = attrs[1] & ~termios.ONLCR
        termios.tcsetattr(stream.fileno(), termios.TCSADRAIN, attrs)
    except (OSError, AttributeError, ImportError, ValueError):
        return


def serve_display(stream, fetch, stop: threading.Event, *, width: int, height: int, period: float = 0.2) -> None:
    """Redraw ``stream`` until ``stop`` is set. Fetch failures show a waiting row."""
    stream.write("\x1b[?25l\x1b[2J")
    stream.flush()
    previous = None
    try:
        while not stop.is_set():
            payload = fetch()
            if not isinstance(payload, dict):
                frame = render_frame([], 0, 0, width=width, height=height, waiting=True)
            else:
                lookups = payload.get("lookups") if isinstance(payload.get("lookups"), list) else []
                try:
                    queries = int(payload.get("queries") or 0)
                    blocked = int(payload.get("blocked") or 0)
                except (TypeError, ValueError):
                    queries, blocked = 0, 0
                domains = payload.get("blocklist_domains")
                try:
                    blocklist_domains = int(domains) if domains is not None else None
                except (TypeError, ValueError):
                    blocklist_domains = None
                frame = render_frame(
                    lookups,
                    queries,
                    blocked,
                    width=width,
                    height=height,
                    blocklist_domains=blocklist_domains,
                )
            if frame != previous:
                stream.write(frame)
                stream.flush()
                previous = frame
            if stop.wait(period):
                break
    finally:
        stream.write("\x1b[?25h\x1b[2J\x1b[H")
        stream.flush()


def run_display(tty_path: str, url: str) -> int:
    """Open ``tty_path``, load a large font when one exists, and draw until signal."""
    stop = threading.Event()

    def _stop(_signum, _frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        stream = open(tty_path, "w", encoding="utf-8", errors="replace", buffering=1)
    except OSError as exc:
        print(f"error: could not open {tty_path} ({exc})", file=sys.stderr)
        return 1
    try:
        prepare_console(tty_path)
        _configure_output(stream)
        width, height = tty_size(stream)
        serve_display(stream, lambda: fetch_crt(url), stop, width=width, height=height)
    finally:
        stream.close()
    return 0
