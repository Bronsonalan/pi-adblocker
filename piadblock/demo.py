"""Local status-page demo for UI work without DNS.

Serves the same HTML and JSON routes as the Pi (``/``, ``/c``, ``/crt``,
``/crt/c``, ``/api/stats``, ``/api/crt``) with fake lookups that keep
changing. Does not bind port 53, start the DNS server, touch
``/var/lib/piadblock``, or write files.
"""

from __future__ import annotations

import argparse
import random
import signal
import sys
import threading
import time
from pathlib import Path

from piadblock import __version__
from piadblock.crt import LookupFeed
from piadblock.dns import A, AAAA, HTTPS
from piadblock.stats import Stats, utc_day
from piadblock.status import StatusPage

HOST = "127.0.0.1"
DEFAULT_PORT = 8080

_BLOCKED = (
    "doubleclick.net",
    "googleadservices.com",
    "adservice.google.com",
    "pagead2.googlesyndication.com",
    "ads.facebook.com",
    "an.facebook.com",
    "mask.icloud.com",
    "metrics.icloud.com",
    "api.mixpanel.com",
    "api-js.mixpanel.com",
    "segment.io",
    "api.segment.io",
    "firebase-settings.crashlytics.com",
    "app-measurement.com",
    "bam.nr-data.net",
    "ingestusipv4.quantummetric.com",
    "tr6.snapchat.com",
    "sstats.adobe.com",
    "notify.bugsnag.com",
    "ads.youtube.com",
    "static.adsafeprotected.com",
    "securepubads.g.doubleclick.net",
)

_ALLOWED = (
    "www.apple.com",
    "xp.apple.com",
    "gsp-ssl.ls.apple.com",
    "www.google.com",
    "clients4.google.com",
    "github.com",
    "api.github.com",
    "www.netflix.com",
    "nflxvideo.net",
    "www.amazon.com",
    "i.ytimg.com",
    "s.youtube.com",
    "www.reddit.com",
    "www.nytimes.com",
    "cdn.jsdelivr.net",
    "fonts.googleapis.com",
)

_CLIENTS = (
    "192.168.1.10",
    "192.168.1.24",
    "192.168.1.55",
    "192.168.1.88",
    "10.0.0.12",
    "10.0.0.40",
    "172.20.1.5",
)

_QTYPES = ("A", "AAAA", "HTTPS", "A", "A", "AAAA")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def blocklist_size(root: Path | None = None) -> int:
    """Count non-empty, non-comment lines in the checkout's blocklist.txt."""
    path = (root or _repo_root()) / "blocklist.txt"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 185_432
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count or 185_432


def _seed_stats(stats: Stats, lookups: LookupFeed, rng: random.Random) -> None:
    """Fill today, yesterday, and a short lookup ring so the first paint is lively."""
    today = utc_day()
    yesterday = utc_day(time.time() - 86400)
    prior_domains = {name: rng.randint(4, 90) for name in _BLOCKED[:12]}
    prior_blocked = sum(prior_domains.values())
    prior_queries = prior_blocked + rng.randint(1800, 4200)
    with stats._lock:
        stats._days[yesterday] = {
            "queries": prior_queries,
            "blocked": prior_blocked,
            "domains": dict(prior_domains),
            "upstream_errors": {
                "timeout": rng.randint(0, 3),
                "servfail": rng.randint(0, 2),
                "refused": 0,
                "socket": rng.randint(0, 1),
                "truncation": 0,
            },
        }
        today_domains = {name: rng.randint(2, 40) for name in _BLOCKED[:10]}
        today_blocked = sum(today_domains.values())
        today_queries = today_blocked + rng.randint(900, 2200)
        stats._days[today] = {
            "queries": today_queries,
            "blocked": today_blocked,
            "domains": dict(today_domains),
            "upstream_errors": {
                "timeout": rng.randint(0, 2),
                "servfail": 0,
                "refused": 0,
                "socket": 0,
                "truncation": 0,
            },
        }
        stats._counters["queries"] = today_queries + rng.randint(200, 800)
        stats._counters["blocked"] = today_blocked + rng.randint(40, 200)
        stats._counters["cached"] = rng.randint(80, 400)
        stats._counters["forwarded"] = max(
            0, stats._counters["queries"] - stats._counters["blocked"] - stats._counters["cached"]
        )
        stats._counters["errors"] = rng.randint(0, 6)
        stats._reasons["timeout"] = rng.randint(0, 2)
        stats.started_at = time.time() - rng.randint(3600, 86400 * 3)
        for name, count in today_domains.items():
            stats._recent.appendleft(
                {
                    "ts": time.time() - rng.random() * 120,
                    "name": name,
                    "qtype": rng.choice(_QTYPES),
                    "client": rng.choice(_CLIENTS),
                }
            )
            if len(stats._recent) >= 12:
                break

    for _ in range(18):
        _emit_lookup(stats, lookups, rng, advance_counters=False)


def _emit_lookup(
    stats: Stats,
    lookups: LookupFeed,
    rng: random.Random,
    *,
    advance_counters: bool = True,
) -> None:
    roll = rng.random()
    if roll < 0.28:
        status = "BLOCKED"
        name = rng.choice(_BLOCKED)
    elif roll < 0.97:
        status = "OK"
        name = rng.choice(_ALLOWED)
    else:
        status = "FAIL"
        name = rng.choice(_ALLOWED + _BLOCKED)
    client = rng.choice(_CLIENTS)
    qtype = rng.choice(_QTYPES)
    lookups.note(status, client, name, qtype)
    if not advance_counters:
        return
    stats.bump("queries")
    if status == "BLOCKED":
        stats.bump("blocked")
        code = {"A": A, "AAAA": AAAA, "HTTPS": HTTPS}.get(qtype, A)
        stats.note_blocked(name, code, client)
    elif status == "FAIL":
        stats.bump("errors")
        if rng.random() < 0.5:
            stats.note_upstream("timeout")
        else:
            stats.note_upstream("socket")
    elif rng.random() < 0.35:
        stats.bump("cached")
    else:
        stats.bump("forwarded")


def _feeder(stats: Stats, lookups: LookupFeed, stop: threading.Event, rng: random.Random) -> None:
    while not stop.wait(rng.uniform(0.7, 1.3)):
        for _ in range(rng.randint(1, 3)):
            _emit_lookup(stats, lookups, rng)


def details_fn(domains: int) -> dict:
    return {
        "version": __version__,
        "listen": HOST,
        "port": 0,
        "upstream": ["1.1.1.1", "1.0.0.1", "9.9.9.9"],
        "blocklist_domains": domains,
        "allow_count": 12,
        "deny_count": 0,
        "cache_entries": 128,
    }


def start_demo(host: str = HOST, port: int = DEFAULT_PORT) -> StatusPage:
    """Bind the status page with a live fake feed. Raises OSError if every port fails."""
    stats = Stats("")
    lookups = LookupFeed()
    rng = random.Random()
    _seed_stats(stats, lookups, rng)
    domains = blocklist_size()
    stop = threading.Event()
    thread = threading.Thread(
        target=_feeder,
        args=(stats, lookups, stop, rng),
        name="piadblock-demo-feed",
        daemon=True,
    )
    thread.start()

    last_error: OSError | None = None
    candidates = [port] + [p for p in range(port + 1, port + 20) if p != port]
    for candidate in candidates:
        page = StatusPage(
            host,
            candidate,
            stats,
            lambda d=domains: details_fn(d),
            lookups,
        )
        try:
            page.start()
        except OSError as exc:
            last_error = exc
            continue
        page._demo_stop = stop  # type: ignore[attr-defined]
        return page
    assert last_error is not None
    raise last_error


def run_demo(host: str = HOST, port: int = DEFAULT_PORT) -> int:
    """Start the demo server and block until SIGINT/SIGTERM."""
    try:
        page = start_demo(host, port)
    except OSError as exc:
        print(f"error: could not bind {host}:{port}: {exc}", file=sys.stderr)
        return 1
    stop = getattr(page, "_demo_stop", threading.Event())

    def _halt(_signum=None, _frame=None) -> None:
        stop.set()
        page.stop()

    signal.signal(signal.SIGINT, _halt)
    signal.signal(signal.SIGTERM, _halt)
    url = f"http://localhost:{page.port}/"
    print(f"piadblock demo listening on {url}", flush=True)
    print(f"Open {url}", flush=True)
    print("Same pages as the Pi: /  /c  /crt  /crt/c  /api/stats  /api/crt", flush=True)
    print("Ctrl-C to stop. Re-run demo.sh to pick up new pushes.", flush=True)
    try:
        while not stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        _halt()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local status-page demo (no DNS)")
    parser.add_argument(
        "--host",
        default=HOST,
        help=f"bind address (default: {HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"preferred port (default: {DEFAULT_PORT}; tries the next few if busy)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_demo(args.host, args.port)


if __name__ == "__main__":
    sys.exit(main())
