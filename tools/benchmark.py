#!/usr/bin/env python3
"""Capacity benchmark for the DNS blocker.

Generates a synthetic block list, starts the real server on 127.0.0.1 and a
high port, and measures load time, reload time, memory, and query latency
while a reload is in progress. It does not bind port 53, does not read the
installed block list, and does not change systemd.

From a checkout, or from /opt/piadblock on the Pi:

    python3 tools/benchmark.py

sudo is not required. The default port is 15353.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import platform
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from piadblock.config import Config  # noqa: E402
from piadblock.dns import A, build_query, parse_message, rdata_ip  # noqa: E402
from piadblock.server import DnsServer  # noqa: E402

DEFAULT_SIZES = (100_000, 250_000, 500_000)
DEFAULT_PORT = 15353
P95_LIMIT_MS = 50.0
QUERY_TIMEOUT_S = 1.0
RAM_FRACTION = 0.25
ASSUMED_PI_MB = 2048.0
# One client streaming as fast as it can. Extra clients mostly measure
# benchmark threads fighting the parser for the GIL, not one device's DNS.
CLIENTS = 0
TEMP_PREFIX = "piadblock-bench-"


def read_status_kb() -> tuple[float, float]:
    """Return ``(VmRSS, VmHWM)`` in kilobytes from ``/proc/self/status``."""
    rss = hwm = None
    text = Path("/proc/self/status").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            rss = float(line.split()[1])
        elif line.startswith("VmHWM:"):
            hwm = float(line.split()[1])
    if rss is None or hwm is None:
        raise RuntimeError("/proc/self/status has no VmRSS/VmHWM")
    return rss, hwm


def read_memtotal_mb() -> float | None:
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            return float(line.split()[1]) / 1024.0
    return None


def machine_model() -> str:
    try:
        raw = Path("/proc/device-tree/model").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b"").decode("utf-8", "replace").strip()


def host_info() -> dict:
    model = machine_model()
    is_pi = "Raspberry Pi" in model
    mem_mb = read_memtotal_mb()
    if is_pi and mem_mb:
        budget_mb = mem_mb * RAM_FRACTION
        budget_reason = f"{budget_mb:.0f} MB (25% of MemTotal {mem_mb:.0f} MB on this Pi)"
    else:
        budget_mb = ASSUMED_PI_MB * RAM_FRACTION
        budget_reason = (
            f"{budget_mb:.0f} MB (25% of {ASSUMED_PI_MB:.0f} MB, "
            "assumed for a Pi 4 because this host is not a Pi)"
        )
    if is_pi:
        label = model or "Raspberry Pi"
    else:
        label = f"not a Raspberry Pi ({platform.machine()}, {platform.system()})"
    return {
        "label": label,
        "is_pi": is_pi,
        "mem_mb": mem_mb,
        "budget_mb": budget_mb,
        "budget_reason": budget_reason,
        "platform": platform.platform(),
    }


def percentile(values: list[float], pct: float) -> float:
    """Linear percentile. ``pct`` is 0-100."""
    if not values:
        raise ValueError("no samples")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def domain_at(index: int) -> str:
    """One synthetic name. Shapes match a hosts-style list: 2 or 3 labels."""
    slot = index % 3
    if slot == 0:
        return f"ad{index}.tracker.test"
    if slot == 1:
        return f"pixel-{index}.metrics.invalid"
    return f"cdn{index}.ads.example.test"


def write_domains(path: Path, count: int) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# synthetic list for the piadblock capacity benchmark\n")
        batch: list[str] = []
        for index in range(count):
            batch.append(domain_at(index) + "\n")
            if len(batch) >= 20000:
                handle.writelines(batch)
                batch.clear()
        if batch:
            handle.writelines(batch)
        handle.write("# end\n")
        handle.flush()
        os.fsync(handle.fileno())


def sample_names(count: int, limit: int = 512) -> list[str]:
    step = max(1, count // limit)
    names = [domain_at(index) for index in range(0, count, step)]
    return names[:limit]


class _StubResolver(threading.Thread):
    """Local upstream so a forwarded name does not wait on the network."""

    def __init__(self) -> None:
        super().__init__(name="piadblock-bench-upstream", daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        # Non-blocking. select's timeout only runs while the socket is idle
        # so the thread can stop. A waiting packet wakes select immediately,
        # so the timeout is not added to a query.
        self.sock.setblocking(False)
        self._halt = threading.Event()

    def run(self) -> None:
        from piadblock.dns import AAAA, build_response

        while not self._halt.is_set():
            try:
                readable, _, _ = select.select([self.sock], [], [], 0.05)
            except (OSError, ValueError):
                return
            if not readable:
                continue
            try:
                data, addr = self.sock.recvfrom(4096)
            except BlockingIOError:
                continue
            except OSError:
                return
            try:
                qtype = parse_message(data).questions[0].qtype
            except Exception:
                continue
            if qtype == AAAA:
                rdata = socket.inet_pton(socket.AF_INET6, "2001:db8::10")
                response = build_response(data, [(AAAA, 60, rdata)])
            else:
                response = build_response(data, [(A, 60, socket.inet_aton("93.184.216.34"))])
            try:
                self.sock.sendto(response, addr)
            except OSError:
                return

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=1)
        try:
            self.sock.close()
        except OSError:
            pass


def _timed_query(sock: socket.socket, packet: bytes) -> float | None:
    """Return latency in milliseconds, or None on timeout."""
    started = time.perf_counter()
    try:
        sock.send(packet)
        data = sock.recv(65535)
    except (socket.timeout, TimeoutError):
        return None
    except OSError:
        return None
    if len(data) < 12 or data[:2] != packet[:2]:
        return None
    return (time.perf_counter() - started) * 1000.0


def _query_worker(
    sock: socket.socket,
    packets: list[bytes],
    recording: threading.Event,
    stop: threading.Event,
    sink: list,
    lock: threading.Lock,
) -> None:
    cursor = 0
    while not stop.is_set():
        if not recording.is_set():
            time.sleep(0.001)
            continue
        elapsed = _timed_query(sock, packets[cursor])
        cursor = (cursor + 1) % len(packets)
        if not recording.is_set() and elapsed is None:
            continue
        with lock:
            sink.append(elapsed)


def _silence_lookups() -> tuple[object, object]:
    """Drop per-query stdout. Printing every lookup would time the terminal."""
    previous = sys.stdout
    devnull = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = devnull
    return previous, devnull


def _restore_lookups(previous, devnull) -> None:
    sys.stdout = previous
    devnull.close()


def measure(count: int, port: int) -> dict:
    """Run one size. ``port`` 0 lets the kernel pick a free high port."""
    if port == 53 or (port != 0 and port < 1024):
        raise SystemExit(f"refusing port {port}; this benchmark does not bind port 53")
    directory = tempfile.TemporaryDirectory(prefix=TEMP_PREFIX)
    server: DnsServer | None = None
    upstream: _StubResolver | None = None
    threads: list[threading.Thread] = []
    sockets: list[socket.socket] = []
    previous_stdout = None
    devnull = None
    try:
        root = Path(directory.name)
        denylist = root / "blocklist.txt"
        allowlist = root / "allowlist.txt"
        allowlist.write_text("# empty\n", encoding="utf-8")
        (root / "merged.txt").write_text("", encoding="utf-8")
        write_domains(denylist, count)

        upstream = _StubResolver()
        upstream.start()
        config = Config(
            listen="127.0.0.1",
            port=port,
            tcp=False,
            ipv6=False,
            upstream=["127.0.0.1"],
            upstream_port=upstream.port,
            upstream_timeout=0.5,
            cache_size=128,
            workers=8,
            status_enabled=False,
            auto_update=False,
            auto_reload=False,
            log_file="",
            stats_file="",
            blocklists=[],
            blocklist_dir=str(root),
            allowlist=str(allowlist),
            denylist=str(denylist),
        )
        logger = logging.getLogger("piadblock")
        logger.handlers.clear()
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.ERROR)
        logger.propagate = False

        started = time.perf_counter()
        server = DnsServer(config)
        cold_s = time.perf_counter() - started
        if server._load_error:
            raise RuntimeError(server._load_error)
        if server.details()["deny_count"] != count:
            raise RuntimeError(
                f"loaded {server.details()['deny_count']} names, expected {count}"
            )
        server.start_background()
        bound = server.port
        if bound == 53 or bound < 1024:
            raise RuntimeError(f"server bound privileged port {bound}")

        previous_stdout, devnull = _silence_lookups()
        names = sample_names(count)
        warmup = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sockets.append(warmup)
        warmup.connect(("127.0.0.1", bound))
        warmup.settimeout(2)
        probe = build_query(names[0], A, qid=1)
        warmup.send(probe)
        reply = parse_message(warmup.recv(65535))
        if not reply.answers or rdata_ip(reply.answers[0]) != "0.0.0.0":
            raise RuntimeError(f"warmup query for {names[0]} was not blocked")

        # Idle forwards, before any reload. These names are not on the list,
        # so each one goes to the stub. The reload samples below are a
        # different number: they are blocked names while the parser runs.
        forward_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sockets.append(forward_sock)
        forward_sock.connect(("127.0.0.1", bound))
        forward_sock.settimeout(1.0)
        forward_samples: list[float] = []
        for index in range(32):
            packet = build_query(f"idle-{index}.example", A, qid=5000 + index)
            elapsed = _timed_query(forward_sock, packet)
            if elapsed is None:
                raise RuntimeError("idle forward timed out")
            forward_samples.append(elapsed)
        forward_p50 = percentile(forward_samples, 50)

        # Rewrite the same names so reload parses a full file of this size.
        write_domains(denylist, count)

        recording = threading.Event()
        stop = threading.Event()
        sink: list[float | None] = []
        lock = threading.Lock()
        for client in range(CLIENTS):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets.append(sock)
            sock.connect(("127.0.0.1", bound))
            sock.settimeout(QUERY_TIMEOUT_S)
            packets = [
                build_query(name, A, qid=(client + 1) * 10000 + index + 1)
                for index, name in enumerate(names)
            ]
            thread = threading.Thread(
                target=_query_worker,
                args=(sock, packets, recording, stop, sink, lock),
                name=f"piadblock-bench-client-{client}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        reload_started = time.perf_counter()
        server._start_reload()
        reload_thread = server._reload_thread
        recording.set()
        # Queries below are sent while the reload thread is alive. One query
        # always runs so a short reload still produces a sample.
        main_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sockets.append(main_sock)
        main_sock.connect(("127.0.0.1", bound))
        main_sock.settimeout(QUERY_TIMEOUT_S)
        main_packet = build_query(names[0], A, qid=9)
        deadline = time.perf_counter() + 180
        while True:
            elapsed = _timed_query(main_sock, main_packet)
            with lock:
                sink.append(elapsed)
            if reload_thread is None or not reload_thread.is_alive():
                break
            if time.perf_counter() > deadline:
                raise RuntimeError("reload did not finish")
        if reload_thread is not None:
            reload_thread.join(timeout=5)
        reload_s = time.perf_counter() - reload_started
        recording.clear()
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
        if server.details()["deny_count"] != count:
            raise RuntimeError("reload did not keep a list of the expected size")
        rss_kb, hwm_kb = read_status_kb()
        samples = [item for item in sink if item is not None]
        timeouts = sum(1 for item in sink if item is None)
        if not samples:
            raise RuntimeError("no DNS responses during reload")
        return {
            "domains": count,
            "cold_s": cold_s,
            "reload_s": reload_s,
            "rss_mb": rss_kb / 1024.0,
            "peak_mb": hwm_kb / 1024.0,
            "p50_ms": percentile(samples, 50),
            "p95_ms": percentile(samples, 95),
            "max_ms": max(samples),
            "forward_p50_ms": forward_p50,
            "timeouts": timeouts,
            "queries": len(sink),
            "port": bound,
        }
    finally:
        if previous_stdout is not None and devnull is not None:
            _restore_lookups(previous_stdout, devnull)
        if server is not None:
            server.stop()
        if upstream is not None:
            upstream.stop()
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
        directory.cleanup()


def fits(row: dict, budget_mb: float, p95_limit_ms: float) -> bool:
    return (
        row.get("error") is None
        and row["timeouts"] == 0
        and row["queries"] > 0
        and row["p95_ms"] <= p95_limit_ms
        and row["peak_mb"] <= budget_mb
    )


def choose_max(rows: list[dict], budget_mb: float, p95_limit_ms: float) -> dict | None:
    ok = [row for row in rows if fits(row, budget_mb, p95_limit_ms)]
    if not ok:
        return None
    return max(ok, key=lambda row: row["domains"])


def format_report(rows: list[dict], info: dict, p95_limit_ms: float) -> str:
    lines = [
        "piadblock capacity benchmark",
        f"host: {info['label']}",
        (
            f"MemTotal: {info['mem_mb']:.0f} MB"
            if info["mem_mb"]
            else "MemTotal: unavailable, assuming 2048 MB"
        ),
        f"rss budget: {info['budget_reason']}",
        f"p95 limit during reload: {p95_limit_ms:.0f} ms",
        "query timeout: "
        + (
            f"{QUERY_TIMEOUT_S * 1000:.0f} ms, one client streaming queries during the reload"
            if CLIENTS == 0
            else f"{QUERY_TIMEOUT_S * 1000:.0f} ms, {CLIENTS} extra clients plus the main thread"
        ),
        "upstream: local stub on 127.0.0.1 (latency is the blocker, not 1.1.1.1)",
        "port 53 is not used; the live service is not modified",
        "cold_s is the time to build the matcher before the server listens",
        "peak_mb is VmHWM; rss_mb is VmRSS after reload",
        "p50_ms is blocked-name latency while that reload is parsing",
        "fwd_p50 is idle forwarded latency against the stub, before the reload",
        "",
        (
            f"{'size':>8}  {'cold_s':>8}  {'reload_s':>8}  {'rss_mb':>8}  "
            f"{'peak_mb':>8}  {'p50_ms':>8}  {'p95_ms':>8}  {'max_ms':>8}  "
            f"{'fwd_p50':>8}  {'timeouts':>8}  {'queries':>8}"
        ),
    ]
    for row in rows:
        if row.get("error"):
            lines.append(f"{row['domains']:>8}  ERROR  {row['error']}")
            continue
        lines.append(
            f"{row['domains']:>8}  {row['cold_s']:8.3f}  {row['reload_s']:8.3f}  "
            f"{row['rss_mb']:8.1f}  {row['peak_mb']:8.1f}  {row['p50_ms']:8.2f}  "
            f"{row['p95_ms']:8.2f}  {row['max_ms']:8.2f}  {row['forward_p50_ms']:8.2f}  "
            f"{row['timeouts']:8d}  {row['queries']:8d}"
        )
    lines.append("")
    best = choose_max(
        [row for row in rows if not row.get("error")],
        info["budget_mb"],
        p95_limit_ms,
    )
    if best is None:
        lines.append("recommended max list size: none")
        lines.append(
            "reason: no measured size kept reload p95 under "
            f"{p95_limit_ms:.0f} ms with zero timeouts and peak RSS under "
            f"{info['budget_mb']:.0f} MB"
        )
    else:
        lines.append(f"recommended max list size: {best['domains']} domains")
        lines.append(
            "reason: largest measured size with reload p95 "
            f"{best['p95_ms']:.2f} ms under {p95_limit_ms:.0f} ms, "
            f"zero timeouts, and peak RSS {best['peak_mb']:.1f} MB under "
            f"{info['budget_mb']:.0f} MB"
        )
    if not info["is_pi"]:
        lines.append(
            "note: these numbers are from this machine, not from a Raspberry Pi. "
            "Latency will be different on a Pi. Run the same command there."
        )
    return "\n".join(lines) + "\n"


def _run_worker(count: int, port: int) -> int:
    try:
        result = measure(count, port)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


def _run_sizes(sizes: list[int], port: int, p95_limit_ms: float) -> int:
    info = host_info()
    rows: list[dict] = []
    script = str(Path(__file__).resolve())
    for count in sizes:
        print(f"measuring {count} domains...", file=sys.stderr, flush=True)
        completed = subprocess.run(
            [
                sys.executable,
                script,
                "--worker",
                "--sizes",
                str(count),
                "--port",
                str(port),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "worker failed").strip()
            rows.append({"domains": count, "error": detail.splitlines()[-1]})
            continue
        line = ""
        for candidate in reversed(completed.stdout.splitlines()):
            if candidate.startswith("{"):
                line = candidate
                break
        if not line:
            rows.append({"domains": count, "error": "worker printed no result"})
            continue
        rows.append(json.loads(line))
    sys.stdout.write(format_report(rows, info, p95_limit_ms))
    return 0 if any(not row.get("error") for row in rows) else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure piadblock load time, memory, and latency during reload.",
    )
    parser.add_argument(
        "--sizes",
        default=",".join(str(size) for size in DEFAULT_SIZES),
        help="comma-separated domain counts (default: 100000,250000,500000)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"UDP port on 127.0.0.1 (default: {DEFAULT_PORT}; 0 picks a free port)",
    )
    parser.add_argument(
        "--p95-ms",
        type=float,
        default=P95_LIMIT_MS,
        help=f"reload p95 limit used for the recommendation (default: {P95_LIMIT_MS:.0f})",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    sizes = []
    for piece in str(args.sizes).split(","):
        piece = piece.strip()
        if not piece:
            continue
        count = int(piece)
        if count < 1:
            parser.error("sizes must be positive")
        sizes.append(count)
    if not sizes:
        parser.error("provide at least one size")
    if args.port == 53 or (args.port != 0 and (args.port < 0 or args.port > 65535)):
        parser.error("port must be 0 or 1024-65535; port 53 is refused")
    if args.port != 0 and args.port < 1024:
        parser.error(f"refusing port {args.port}; use a port above 1024 so port 53 stays untouched")
    args.sizes_list = sizes
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.worker:
        if len(args.sizes_list) != 1:
            print("worker mode takes one size", file=sys.stderr)
            return 2
        return _run_worker(args.sizes_list[0], args.port)
    return _run_sizes(args.sizes_list, args.port, args.p95_ms)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
