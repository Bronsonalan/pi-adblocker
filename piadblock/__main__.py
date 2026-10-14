"""Command line: ``python3 -m piadblock serve|update|check|stats``."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from piadblock import __version__
from piadblock.blocklist import Matcher
from piadblock.config import ConfigError, load_config
from piadblock.log import setup_logging
from piadblock.paths import SERVICE_NAME
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
    parser.error(f"unknown command {args.command}")
    return 2


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
    matcher = Matcher.load(config.merged_path, config.allowlist, config.denylist)
    decision = matcher.decide(domain)
    print(domain)
    print("blocked" if decision.blocked else "allowed")
    print(f"reason: {decision.reason}")
    print(
        f"lists: {matcher.blocked_count} blocked, "
        f"{matcher.allow_count} allowed, {matcher.deny_count} denied"
    )
    return 0


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
    print(f"uptime:    {data.get('uptime_seconds', 0)}s")
    print(f"file:      {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
