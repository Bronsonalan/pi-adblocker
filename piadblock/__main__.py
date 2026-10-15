"""Command line: ``python3 -m piadblock serve|update|check|stats|crt``."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from piadblock import __version__
from piadblock.blocklist import Matcher
from piadblock.config import ConfigError, load_config
from piadblock.crt import run_display
from piadblock.gitrepo import Repo, blocklist_changed_message, load_access
from piadblock.log import setup_logging
from piadblock.paths import OPT_ROOT, SERVICE_NAME
from piadblock.report import dump_document, heartbeat_document, write_daily_files
from piadblock.server import DnsServer
from piadblock.updater import update_blocklists


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="piadblock",
        description="Network-wide DNS ad blocker",
    )
    parser.add_argument("--version", action="version", version=f"piadblock {__version__}")
    parser.add_argument("--config", help="path to config.json (default: /etc/piadblock/config.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the DNS server in the foreground")

    update = sub.add_parser("update", help="download and merge blocklists")
    update.add_argument(
        "--if-due",
        action="store_true",
        help="skip the download when the merged list is newer than update_interval_hours",
    )

    check = sub.add_parser("check", help="show whether a domain would be blocked")
    check.add_argument("domain")

    sub.add_parser("stats", help="print the counters last saved by the server")
    sub.add_parser("pull", help="git pull --rebase and reload if blocklist.txt changed")
    sub.add_parser("heartbeat", help="write heartbeat.json and push it")
    sub.add_parser("daily", help="write stats/YYYY-MM-DD.json and push it")
    crt = sub.add_parser("crt", help="fullscreen lookup feed on the console")
    crt.add_argument("--tty", default="/dev/tty1", help="console device (default: /dev/tty1)")
    crt.add_argument("--url", default="", help="lookup JSON URL (default: this machine's status page)")

    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.log_file, config.log_level)
    if args.command == "serve":
        return _serve(config)
    if args.command == "update":
        return _update(config, force=not args.if_due)
    if args.command == "check":
        return _check(config, args.domain)
    if args.command == "stats":
        return _stats(config)
    if args.command == "pull":
        return _pull()
    if args.command == "heartbeat":
        return _heartbeat(config)
    if args.command == "daily":
        return _daily(config)
    if args.command == "crt":
        return run_display(args.tty, args.url or _crt_url(config))
    parser.error(f"unknown command {args.command}")
    return 2


def _crt_url(config) -> str:
    host = config.status_listen
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    return f"http://{host}:{config.status_port}/api/crt"


def _serve(config) -> int:
    server = DnsServer(config)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.stop()
    return 0


def _update(config, *, force: bool) -> int:
    result = update_blocklists(config, force=force)
    for source in result.sources:
        if source.get("ok"):
            print(f"  ok  {source['url']}  ({source['domains']} domains)")
        else:
            print(f"  fail  {source['url']}  ({source.get('error') or 'failed'})")
    if result.skipped:
        print(f"blocklist is fresh ({result.domain_count} domains)")
        return 0
    if not result.ok:
        print(f"error: {result.error or 'blocklist update failed'}", file=sys.stderr)
        return 1
    print(f"merged {result.domain_count} domains into {config.merged_path}")
    completed = subprocess.run(
        ["systemctl", "kill", "-s", "HUP", SERVICE_NAME],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        print("reloaded the running server")
    return 0


def _check(config, domain: str) -> int:
    report = Matcher.prepare(config.merged_path, config.allowlist, config.denylist)
    if report.error:
        print(f"error: {report.error}", file=sys.stderr)
    elif report.skipped:
        print(f"warning: skipped {report.skipped} malformed blocklist lines", file=sys.stderr)
    matcher = report.matcher
    decision = matcher.decide(domain)
    print(domain)
    print("blocked" if decision.blocked else "allowed")
    print(f"reason: {decision.reason}")
    print(
        f"lists: {matcher.blocklist_count} blocklist, "
        f"{matcher.allow_count} allowlist"
    )
    return 1 if report.error else 0


def _warn(message: str) -> None:
    if message:
        print(f"warning: {message}" if not message.startswith("warning:") else message, flush=True)


def _pull() -> int:
    try:
        repo = Repo(OPT_ROOT, load_access())
        changed, warning = repo.pull()
        _warn(warning)
        if changed:
            _request_reload()
        print(blocklist_changed_message(changed), flush=True)
    except Exception as exc:
        print(f"warning: git pull failed ({exc}). Blocking is unchanged.", flush=True)
    return 0


def _read_stats_snapshot(config) -> dict:
    path = Path(config.stats_file) if config.stats_file else None
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _heartbeat(config) -> int:
    try:
        from datetime import datetime, timezone

        data = _read_stats_snapshot(config)
        try:
            uptime = int(data.get("uptime_seconds", 0))
        except (TypeError, ValueError):
            uptime = 0
        document = heartbeat_document(datetime.now(timezone.utc), uptime, __version__)
        repo = Repo(OPT_ROOT, load_access())
        with repo.lock():
            target = Path(repo.root) / "heartbeat.json"
            target.write_text(dump_document(document), encoding="utf-8")
            warning = repo.publish_holding_lock(["heartbeat.json"], f"heartbeat: {document['timestamp']}")
        _warn(warning)
        if warning:
            print("heartbeat: not pushed", flush=True)
        else:
            print(f"heartbeat: {target}", flush=True)
    except Exception as exc:
        print(f"warning: heartbeat failed ({exc}). Blocking is unchanged.", flush=True)
    return 0


def sync_daily_stats(snapshot: dict, repo: Repo) -> tuple[list[str], str]:
    """Write the live UTC-day counters into the checkout and push them.

    The file write and the push share ``repo``'s lock with ``pull``, so a
    minutely ``git checkout -- .`` cannot revert the day's file between the
    write and the commit.
    """
    days = snapshot.get("days") if isinstance(snapshot.get("days"), dict) else {}
    if not days:
        return [], ""
    with repo.lock():
        written = write_daily_files(snapshot, Path(repo.root))
        if not written:
            return [], ""
        labels = [path.removeprefix("stats/").removesuffix(".json") for path in written]
        warning = repo.publish_holding_lock(written, "stats: " + ", ".join(labels))
    return written, warning


def _daily(config) -> int:
    try:
        snapshot = _read_stats_snapshot(config)
        days = snapshot.get("days") if isinstance(snapshot.get("days"), dict) else {}
        if not days:
            print("daily: no per-domain counts saved yet", flush=True)
            return 0
        repo = Repo(OPT_ROOT, load_access())
        written, warning = sync_daily_stats(snapshot, repo)
        if not written and not warning:
            print("daily: stats unchanged", flush=True)
            return 0
        _warn(warning)
        if warning:
            print("daily: not pushed", flush=True)
        else:
            print("daily: " + " ".join(written), flush=True)
    except Exception as exc:
        print(f"warning: daily stats failed ({exc}). Blocking is unchanged.", flush=True)
    return 0


def _request_reload() -> None:
    import shutil

    systemctl = shutil.which("systemctl")
    if systemctl is None:
        print(
            "warning: systemctl is missing; the server re-reads blocklist.txt within a few seconds",
            flush=True,
        )
        return
    result = subprocess.run(
        [systemctl, "kill", "-s", "HUP", SERVICE_NAME],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "systemctl kill failed").strip()
        print(
            f"warning: could not signal reload ({detail}). "
            "The server re-reads blocklist.txt within a few seconds.",
            flush=True,
        )


def _stats(config) -> int:
    path = Path(config.stats_file)
    if not config.stats_file or not path.exists():
        where = config.stats_file or "(stats_file is empty)"
        print(f"no stats file yet at {where}")
        print("is the service running?  systemctl status piadblock")
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: could not read {path}: {exc}", file=sys.stderr)
        return 1
    print(f"queries:   {data.get('queries', 0)}")
    print(f"blocked:   {data.get('blocked', 0)} ({data.get('blocked_percent', 0)}%)")
    print(f"cached:    {data.get('cached', 0)}")
    print(f"forwarded: {data.get('forwarded', 0)}")
    print(f"errors:    {data.get('errors', 0)}")
    reasons = data.get("upstream_errors") if isinstance(data.get("upstream_errors"), dict) else {}
    if reasons:
        print(
            "upstream:  "
            + " ".join(f"{name}={int(reasons.get(name, 0) or 0)}" for name in ("timeout", "servfail", "refused", "socket", "truncation"))
        )
    today = data.get("today") if isinstance(data.get("today"), dict) else {}
    if today:
        print(f"today:     {today.get('date', '')} queries={today.get('queries', 0)} blocked={today.get('blocked', 0)}")
    print(f"uptime:    {data.get('uptime_seconds', 0)}s")
    print(f"file:      {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
