#!/usr/bin/env python3
"""Remove the piadblock service and undo the installer's DNS changes.

    sudo python3 uninstall.py
    sudo python3 uninstall.py --purge

Without ``--purge`` the config, allow/deny lists, downloaded blocklists, and
logs are kept so a later install picks up where this one left off.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from piadblock.files import atomic_write  # noqa: E402
from piadblock.paths import (  # noqa: E402
    CONFIG_DIR,
    DHCPCD_CONF,
    INSTALL_STATE_PATH,
    LOG_DIR,
    NM_CONF,
    OPT_ROOT,
    RESOLV_CONF,
    RESOLVED_DROPIN,
    STATE_DIR,
    UNIT_DIR,
)
from piadblock.systemfix import remove_marked_block  # noqa: E402


def say(message: str = "") -> None:
    print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uninstall.py", description="Remove piadblock")
    parser.add_argument(
        "--purge",
        action="store_true",
        help="also delete /etc/piadblock, /var/lib/piadblock, and /var/log/piadblock",
    )
    args = parser.parse_args(argv)
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("error: uninstall needs root.\nRun: sudo python3 uninstall.py", file=sys.stderr)
        return 1

    say("piadblock uninstall")
    units = (
        "piadblock-pull.timer",
        "piadblock-heartbeat.timer",
        "piadblock-stats.timer",
        "piadblock-update.timer",
        "piadblock-pull.service",
        "piadblock-heartbeat.service",
        "piadblock-stats.service",
        "piadblock-update.service",
        "piadblock.service",
    )
    for name in units:
        _systemctl("stop", name)
        _systemctl("disable", name)
    say("stopped piadblock.service")

    unit_dir = Path(UNIT_DIR)
    for name in units:
        path = unit_dir / name
        if path.exists() or path.is_symlink():
            path.unlink()
            say(f"removed {path}")
    _systemctl("daemon-reload")
    _systemctl("reset-failed", "piadblock.service")

    state = _load_state()
    _restore_resolver(state)
    if not args.purge:
        _save_state(state)

    opt = Path(OPT_ROOT)
    if opt.exists() and opt.resolve() != ROOT.resolve():
        shutil.rmtree(opt, ignore_errors=True)
        say(f"removed {opt}")
    elif opt.exists():
        say(f"left {opt} in place because uninstall.py is running from that directory")
        say("Remove it after this command finishes: sudo rm -rf /opt/piadblock")

    if args.purge:
        for path in (CONFIG_DIR, STATE_DIR, LOG_DIR):
            shutil.rmtree(path, ignore_errors=True)
            say(f"removed {path}")
    else:
        say(f"kept {CONFIG_DIR} (run uninstall.py --purge to remove config, lists, and logs)")
    say("done")
    return 0


def _restore_resolver(state: dict) -> None:
    if state.get("changed_resolv_conf"):
        try:
            _restore_resolv(state.get("resolv_backup") or {"type": "missing"})
            say(f"restored {RESOLV_CONF}")
        except OSError as exc:
            say(f"warning: could not restore {RESOLV_CONF}: {exc}")
        state["changed_resolv_conf"] = False
        state.pop("resolv_backup", None)

    if state.get("modified_dhcpcd"):
        dhcpcd = Path(DHCPCD_CONF)
        if dhcpcd.exists():
            try:
                original = dhcpcd.read_text(encoding="utf-8", errors="replace")
                dhcpcd.write_text(remove_marked_block(original), encoding="utf-8")
                say(f"removed piadblock lines from {DHCPCD_CONF}")
            except OSError as exc:
                say(f"warning: could not edit {DHCPCD_CONF}: {exc}")
        state["modified_dhcpcd"] = False

    if state.get("modified_nm"):
        nm_path = Path(NM_CONF)
        if nm_path.exists():
            nm_path.unlink()
            say(f"removed {nm_path}")
        state["modified_nm"] = False

    if state.get("disabled_resolved_stub"):
        dropin = Path(RESOLVED_DROPIN)
        if dropin.exists():
            dropin.unlink()
        _systemctl("restart", "systemd-resolved.service")
        say("restored systemd-resolved DNSStubListener")
        state["disabled_resolved_stub"] = False

    if state.get("disabled_dnsmasq"):
        say("dnsmasq was disabled by piadblock and was not started again.")
        say("To turn it back on: sudo systemctl enable --now dnsmasq")
        state["disabled_dnsmasq"] = False


def _restore_resolv(backup: dict) -> None:
    resolv = Path(RESOLV_CONF)
    if resolv.is_symlink() or resolv.exists():
        resolv.unlink()
    kind = backup.get("type")
    if kind == "symlink":
        resolv.symlink_to(backup.get("target", ""))
    elif kind == "file":
        resolv.write_text(backup.get("content", ""), encoding="utf-8")
        os.chmod(resolv, 0o644)
    # "missing" leaves resolv.conf absent, which is what we found.


def _load_state() -> dict:
    try:
        data = json.loads(Path(INSTALL_STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    try:
        atomic_write(INSTALL_STATE_PATH, json.dumps(state, indent=2) + "\n")
    except OSError as exc:
        say(f"warning: could not update install state: {exc}")


def _systemctl(*args: str) -> None:
    if shutil.which("systemctl") is None:
        return
    subprocess.run(["systemctl", *args], check=False, capture_output=True, text=True)


if __name__ == "__main__":
    sys.exit(main())
