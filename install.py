#!/usr/bin/env python3
"""Install piadblock as a systemd service that starts on boot.

    sudo python3 install.py

The same file is what ``bootstrap.sh`` runs after it downloads the archive.
Only the Python standard library is used. Nothing is installed with pip or apt.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from piadblock import __version__  # noqa: E402
from piadblock.blocklist import Matcher  # noqa: E402
from piadblock.config import ConfigError, load_config, migrate_legacy_upstream  # noqa: E402
from piadblock.dns import A, AAAA, DnsError, rdata_ip  # noqa: E402
from piadblock.files import atomic_write  # noqa: E402
from piadblock.forward import lookup  # noqa: E402
from piadblock.gitrepo import discover_git_access, save_access  # noqa: E402
from piadblock.gitrepo import Repo as GitRepo  # noqa: E402
from piadblock.paths import (  # noqa: E402
    BLOCKLIST_DIR,
    CANONICAL_CONFIG_PATHS,
    CONFIG_DIR,
    CONFIG_PATH,
    DENYLIST_PATH,
    DHCPCD_CONF,
    HEARTBEAT_SERVICE_NAME,
    HEARTBEAT_TIMER_NAME,
    INSTALL_STATE_PATH,
    LEGACY_DENYLIST_PATH,
    LOG_DIR,
    NM_CONF,
    OPT_ROOT,
    PREFIX,
    PULL_SERVICE_NAME,
    PULL_TIMER_NAME,
    RESOLV_CONF,
    RESOLVED_DROPIN,
    STATE_DIR,
    STATS_SERVICE_NAME,
    STATS_TIMER_NAME,
    UNIT_DIR,
    locate,
)
from piadblock.portcheck import describe, listeners_on  # noqa: E402
from piadblock.systemfix import (  # noqa: E402
    classify,
    render_dhcpcd_block,
    render_nm_conf,
    piadblock_owns_resolv,
    render_resolv_conf,
    render_resolved_dropin,
    resolver_should_be_replaced,
    upsert_marked_block,
)
from piadblock.units import calendar_timer_unit, git_oneshot_unit, service_unit  # noqa: E402
from piadblock.updater import update_blocklists  # noqa: E402


class InstallError(Exception):
    """The install stopped. The message is safe to show as-is."""


def say(message: str = "") -> None:
    print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    del argv  # reserved so a later flag does not change the call shape
    try:
        _require_python()
        _require_root()
        say(f"piadblock installer {__version__}")
        say(f"python3: Python {sys.version.split()[0]}")
        _install_files()
        _ensure_dirs()
        _ensure_config()
        _migrate_denylist()
        _migrate_upstream()
        _ensure_lists()
        config = load_config(CONFIG_PATH)
        _download(config)
        _free_port_53(config)
        _fix_resolver(config)
        access = _setup_git()
        _install_units(access)
        _start_service()
        _self_check(config)
        _start_git_timers()
        _finish(config)
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _require_python() -> None:
    if sys.version_info < (3, 9):
        raise InstallError(
            f"Python {sys.version.split()[0]} is too old. piadblock needs Python 3.9 or newer "
            "(Raspberry Pi OS Bullseye and later already include it)."
        )


def _require_root() -> None:
    # PIADBLOCK_PREFIX is the test install root. It is unset for a real Pi install.
    if PREFIX:
        return
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise InstallError(
            "This installer needs root so it can listen on port 53 and install a systemd service.\n"
            "Run: sudo python3 install.py"
        )


def _install_files() -> None:
    dest = Path(OPT_ROOT)
    if ROOT.resolve() == dest.resolve():
        say(f"installing files to {dest}")
        return
    if (dest / ".git").is_dir():
        say(f"checkout: keeping the git repo at {dest}")
        return
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        ROOT,
        dest,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"),
    )
    say(f"installing files to {dest}")


def _ensure_dirs() -> None:
    for path in (CONFIG_DIR, STATE_DIR, LOG_DIR, BLOCKLIST_DIR):
        Path(path).mkdir(parents=True, exist_ok=True)


def _ensure_config() -> None:
    path = Path(CONFIG_PATH)
    if path.exists():
        say(f"config: keeping {path}")
        return
    example = ROOT / "config.example.json"
    if not example.exists():
        raise InstallError(f"missing {example}")
    if PREFIX:
        data = json.loads(example.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise InstallError(f"{example} is not a JSON object")
        for key, canonical in CANONICAL_CONFIG_PATHS.items():
            if data.get(key) == canonical:
                data[key] = locate(canonical)
        atomic_write(path, json.dumps(data, indent=2) + "\n")
    else:
        shutil.copy(example, path)
    os.chmod(path, 0o644)
    say(f"config: wrote {path}")


def _ensure_lists() -> None:
    dest = Path(CONFIG_DIR) / "allowlist.txt"
    if dest.exists():
        say(f"allowlist.txt: keeping {dest}")
    else:
        shutil.copy(ROOT / "lists" / "allowlist.txt", dest)
        os.chmod(dest, 0o644)
        say(f"allowlist.txt: wrote {dest}")
    blocklist = Path(DENYLIST_PATH)
    if blocklist.is_file():
        say(f"blocklist: {blocklist}")
    else:
        say(f"warning: {blocklist} is missing. Blocking still starts once that file is present.")


def _migrate_upstream() -> None:
    """Replace the old single-resolver default so a reinstall picks up fallbacks.

    ``_ensure_config`` keeps an existing file. Without this step a Pi that
    still has ``["1.1.1.1"]`` and a 2 second timeout never tries the next
    server, and one dead resolver is a client-visible failure.
    """
    path = Path(CONFIG_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    updated = migrate_legacy_upstream(data)
    if updated is None:
        return
    atomic_write(path, json.dumps(updated, indent=2) + "\n")
    os.chmod(path, 0o644)
    servers = " ".join(str(item) for item in updated.get("upstream", []))
    say(f"config: upstream is {servers}, timeout {updated.get('upstream_timeout')}s per try")


def _migrate_denylist() -> None:
    path = Path(CONFIG_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    current = data.get("denylist")
    legacy = {LEGACY_DENYLIST_PATH, "/etc/piadblock/denylist.txt"}
    if current in legacy and current != DENYLIST_PATH:
        data["denylist"] = DENYLIST_PATH
        atomic_write(path, json.dumps(data, indent=2) + "\n")
        os.chmod(path, 0o644)
        say(f"config: denylist is now {DENYLIST_PATH}")


def _ensure_git() -> None:
    found = shutil.which("git")
    if found:
        say(f"git: already installed ({found})")
        return
    say("git: not found, installing it with apt (Raspberry Pi OS normally includes git)")
    if PREFIX:
        say("warning: git is missing and apt is not used for a prefixed install. Blocking is unchanged.")
        return
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    subprocess.run(["apt-get", "update"], check=False, env=env)
    installed = subprocess.run(["apt-get", "install", "-y", "git"], check=False, env=env)
    if installed.returncode != 0 or shutil.which("git") is None:
        say("warning: could not install git. The copied blocklist still works; pull and push will retry.")


def _setup_git():
    _ensure_git()
    try:
        access = discover_git_access(generate=True)
    except Exception as exc:
        say(f"warning: deploy key setup failed ({exc}). Blocking is unchanged.")
        access = discover_git_access(generate=False)
    save_access(access)
    _say_access(access)
    _chown_checkout(access.user)
    if shutil.which("git") is None:
        return access
    warning = GitRepo(OPT_ROOT, access).ensure()
    if warning:
        say(f"warning: {warning}")
    else:
        say(f"git: checkout {OPT_ROOT} tracking {access.remote}")
    _chown_checkout(access.user)
    return access


def _say_access(access) -> None:
    say(f"git user: {access.user} (git pull and push run as this user; the DNS service stays root)")
    if access.source == "ssh-config":
        say(f"deploy key: reusing {access.key} (from ~/.ssh/config Host github.com)")
    elif access.source == "existing-key":
        say(f"deploy key: reusing {access.key}")
    elif access.source == "environment":
        say(f"deploy key: reusing {access.key}")
    elif access.source == "generated":
        say(f"deploy key: wrote {access.key}")
        public = access.key + ".pub"
        text = ""
        if Path(public).is_file():
            text = Path(public).read_text(encoding="utf-8").strip()
        if text:
            say("Add this public key on GitHub as a deploy key with write access:")
            say(text)
    else:
        say("warning: no deploy key found. Pull and push stay off until one is added. Blocking is unchanged.")
    say(f"git remote: {access.remote}")


def _chown_checkout(user: str) -> None:
    if os.geteuid() != 0 or not user or user == "root":
        return
    import pwd

    try:
        pwd.getpwnam(user)
    except KeyError:
        say(f"warning: unix user {user} does not exist; leaving {OPT_ROOT} owned by root")
        return
    for directory, dirnames, filenames in os.walk(OPT_ROOT):
        shutil.chown(directory, user)
        for name in dirnames:
            shutil.chown(Path(directory) / name, user)
        for name in filenames:
            shutil.chown(Path(directory) / name, user)


def _download(config) -> None:
    if not config.blocklists:
        say("blocklists: none configured, download skipped")
        return
    say("downloading blocklists")
    result = update_blocklists(config, force=True)
    for source in result.sources:
        if source.get("ok"):
            say(f"  ok  {source['url']}  ({source['domains']} domains)")
        else:
            say(f"  fail  {source['url']}  ({source.get('error') or 'failed'})")
    if result.ok:
        say(f"merged {result.domain_count} domains into {config.merged_path}")
        return
    if result.kept_existing:
        say(
            f"warning: download failed ({result.error}); "
            f"keeping the existing blocklist ({result.domain_count} domains)"
        )
        return
    raise InstallError(result.error or "blocklist download failed")


def _free_port_53(config) -> None:
    """Stop our previous process, then systemd-resolved's stub and dnsmasq.

    Anything else bound to the configured DNS port is reported and left
    running. piadblock will not kill an unknown daemon. The default port is 53.
    """
    state = _load_state()
    port = config.port
    for _ in range(4):
        groups = classify(listeners_on(port))
        if not any(groups.values()):
            say(f"port {port}: free")
            return
        if groups["ours"]:
            say(f"port {port}: stopping the piadblock service already running")
            _systemctl("stop", "piadblock.service")
            time.sleep(0.4)
            continue
        if groups["resolved"]:
            say(f"port {port}: systemd-resolved is using port {port}; disabling DNSStubListener")
            _disable_resolved_stub(config, state)
            time.sleep(0.6)
            continue
        if groups["dnsmasq"]:
            say(f"port {port}: stopping and disabling dnsmasq")
            _systemctl("stop", "dnsmasq.service")
            _systemctl("disable", "dnsmasq.service")
            state["disabled_dnsmasq"] = True
            _save_state(state)
            time.sleep(0.4)
            continue
        break

    listeners = listeners_on(port)
    groups = classify(listeners)
    if any(groups.values()):
        raise InstallError(_port_busy_message(listeners, groups, port))
    say(f"port {port}: free")


def _port_busy_message(listeners, groups, port: int) -> str:
    lines = [f"port {port} is still in use:"]
    for item in listeners:
        lines.append(f"  {describe(item)}")
    lines.append("Stop that program, then run the installer again.")
    if groups["resolved"]:
        lines.extend(
            [
                "systemd-resolved is still bound. Do this by hand, then re-run the installer:",
                "  sudo mkdir -p /etc/systemd/resolved.conf.d",
                "  printf '[Resolve]\\nDNSStubListener=no\\n' | sudo tee /etc/systemd/resolved.conf.d/piadblock.conf",
                "  sudo systemctl restart systemd-resolved",
            ]
        )
    if groups["dnsmasq"]:
        lines.extend(
            [
                "dnsmasq is still bound. Do this by hand, then re-run the installer:",
                "  sudo systemctl disable --now dnsmasq",
            ]
        )
    if groups["other"]:
        lines.append("piadblock does not stop an unknown program for you.")
    return "\n".join(lines)


def _disable_resolved_stub(config, state: dict) -> None:
    dropin = Path(RESOLVED_DROPIN)
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text(render_resolved_dropin(list(config.upstream)), encoding="utf-8")
    state["disabled_resolved_stub"] = True
    _save_state(state)
    result = _systemctl("restart", "systemd-resolved.service")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "systemctl restart systemd-resolved failed").strip()
        say(f"warning: {detail}")


def _fix_resolver(config) -> None:
    """Keep this Pi's own lookups on the public upstream.

    Once the router sends every device to the Pi, a Pi that still asks the
    router (or 127.0.0.53) for DNS will loop. A public resolver the user
    wrote is left as-is. A file this installer wrote is refreshed so it
    keeps the current upstream list and a 1 second resolver timeout.
    """
    state = _load_state()
    resolv = Path(RESOLV_CONF)
    try:
        current = resolv.read_text(encoding="utf-8", errors="replace") if resolv.exists() else ""
    except OSError:
        current = ""

    desired = render_resolv_conf(list(config.upstream))
    owned = piadblock_owns_resolv(current)
    needs_resolv = resolver_should_be_replaced(current)
    should_write = needs_resolv or (owned and current != desired)
    if needs_resolv and not state.get("changed_resolv_conf"):
        state["resolv_backup"] = _snapshot_resolv(resolv)
        state["changed_resolv_conf"] = True
        _save_state(state)
        say(f"resolver: backed up {RESOLV_CONF}")
    elif not should_write:
        if owned:
            say(f"resolver: {RESOLV_CONF} already points at the upstream list")
        else:
            say(f"resolver: {RESOLV_CONF} already uses a public DNS server; leaving it")

    dhcpcd = Path(DHCPCD_CONF)
    if dhcpcd.exists():
        try:
            original = dhcpcd.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            say(f"warning: could not read {DHCPCD_CONF}: {exc}")
        else:
            updated = upsert_marked_block(original, render_dhcpcd_block(list(config.upstream)))
            if updated != original:
                dhcpcd.write_text(updated, encoding="utf-8")
                state["modified_dhcpcd"] = True
                _save_state(state)
                say(f"resolver: pinned upstream DNS in {DHCPCD_CONF}")

    if _is_active("NetworkManager"):
        nm_path = Path(NM_CONF)
        try:
            nm_path.parent.mkdir(parents=True, exist_ok=True)
            nm_path.write_text(render_nm_conf(), encoding="utf-8")
        except OSError as exc:
            say(f"warning: could not write {NM_CONF}: {exc}")
        else:
            state["modified_nm"] = True
            _save_state(state)
            say(f"resolver: told NetworkManager not to overwrite {RESOLV_CONF}")
            _systemctl("reload", "NetworkManager")

    if should_write:
        # Write this last so a NetworkManager reload cannot put the router back.
        try:
            _write_resolv(list(config.upstream))
        except OSError as exc:
            say(f"warning: could not write {RESOLV_CONF}: {exc}")
        else:
            say(f"resolver: pointed this Pi at {' '.join(config.upstream)} (timeout 1s, 1 attempt)")


def _snapshot_resolv(resolv: Path) -> dict:
    if resolv.is_symlink():
        return {"type": "symlink", "target": os.readlink(resolv)}
    if resolv.exists():
        return {"type": "file", "content": resolv.read_text(encoding="utf-8", errors="replace")}
    return {"type": "missing"}


def _write_resolv(upstreams: list[str]) -> None:
    resolv = Path(RESOLV_CONF)
    if resolv.is_symlink() or resolv.exists():
        resolv.unlink()
    resolv.write_text(render_resolv_conf(upstreams), encoding="utf-8")
    os.chmod(resolv, 0o644)


def _install_units(access) -> None:
    if shutil.which("systemctl") is None:
        raise InstallError(
            "systemctl was not found, so the service could not be installed.\n"
            "Raspberry Pi OS includes systemd. The program files are in /opt/piadblock.\n"
            "You can still run: sudo python3 -m piadblock serve"
        )
    python = shutil.which("python3") or sys.executable
    kill = shutil.which("kill") or "/bin/kill"
    unit_dir = Path(UNIT_DIR)
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "piadblock.service").write_text(
        service_unit(python, OPT_ROOT, kill, prefix=PREFIX),
        encoding="utf-8",
    )
    say("systemd: installed piadblock.service")
    python_path = OPT_ROOT
    jobs = (
        ("Pull the piadblock repo and reload when blocklist.txt changes", PULL_SERVICE_NAME, PULL_TIMER_NAME, "minutely", "pull"),
        ("Write and push the piadblock heartbeat", HEARTBEAT_SERVICE_NAME, HEARTBEAT_TIMER_NAME, "hourly", "heartbeat"),
        ("Write and push the piadblock daily stats", STATS_SERVICE_NAME, STATS_TIMER_NAME, "*:0/5", "daily"),
    )
    for description, service_name, timer_name, calendar, command in jobs:
        (unit_dir / service_name).write_text(
            git_oneshot_unit(
                description,
                python,
                python_path,
                command,
                prefix=PREFIX,
                git_user=access.user,
                remote=access.remote,
            ),
            encoding="utf-8",
        )
        (unit_dir / timer_name).write_text(calendar_timer_unit(description, calendar), encoding="utf-8")
        say(f"systemd: installed {timer_name}")
    reload = _systemctl("daemon-reload")
    if reload.returncode != 0:
        detail = (reload.stderr or reload.stdout or "daemon-reload failed").strip()
        raise InstallError(detail)
    # enable installs the boot symlink. The DNS service is started below with
    # restart. A timer that is only enabled stays inactive (dead) with
    # Trigger: n/a until the next boot, so --now starts it in this session too.
    _systemctl("enable", "piadblock.service")
    for timer_name in (PULL_TIMER_NAME, HEARTBEAT_TIMER_NAME, STATS_TIMER_NAME):
        result = _systemctl("enable", "--now", timer_name)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "systemctl enable --now failed").strip()
            say(f"warning: {timer_name} was not started ({detail}). Blocking is unchanged.")
        else:
            say(f"systemd: started {timer_name}")


def _start_git_timers() -> None:
    """Run one pull, heartbeat, and stats pass now.

    The timers were already started with ``enable --now``. These oneshots
    are the install-time run, so the first pull does not wait for the next
    minute. A git failure must not abort install.
    """
    for service in (PULL_SERVICE_NAME, HEARTBEAT_SERVICE_NAME, STATS_SERVICE_NAME):
        result = _systemctl("start", service)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "systemctl start failed").strip()
            say(f"warning: {service} did not finish ({detail}). Blocking is unchanged.")
        else:
            say(f"systemd: started {service}")


def _start_service() -> None:
    started = _systemctl("restart", "piadblock.service")
    if started.returncode != 0:
        detail = (started.stderr or started.stdout or "restart failed").strip()
        say(detail)
        _dump_journal()
        raise InstallError("piadblock.service failed to start. See the journal output above.")
    say("systemd: started piadblock.service")


def _self_check(config) -> None:
    report = Matcher.prepare(config.merged_path, config.allowlist, config.denylist)
    if report.error:
        say(f"warning: blocklist load failed ({report.error})")
    elif report.skipped:
        say(f"warning: skipped {report.skipped} malformed blocklist lines")
    matcher = report.matcher
    decision = matcher.decide("doubleclick.net")
    if not decision.blocked:
        say("warning: doubleclick.net is not blocked, so the block self-check was skipped")
        say(f"warning: check {config.denylist}")
        return

    host, port = _query_target(config)
    say(f"self-check: querying {host}")
    deadline = time.time() + 15
    last_error = "no response"
    blocked = None
    while time.time() < deadline:
        try:
            blocked = lookup(host, port, "doubleclick.net", A, timeout=1.0)
        except (OSError, DnsError) as exc:
            last_error = str(exc)
            time.sleep(0.4)
            continue
        address = rdata_ip(blocked.answers[0]) if blocked.answers else None
        if address == config.block_ipv4:
            say(f"self-check: doubleclick.net A -> {address}")
            break
        last_error = f"unexpected answer {address!r}"
        time.sleep(0.4)
        blocked = None
    if blocked is None:
        _dump_journal()
        raise InstallError(f"self-check failed for doubleclick.net ({last_error})")

    try:
        aaaa = lookup(host, port, "doubleclick.net", AAAA, timeout=2.0)
    except (OSError, DnsError) as exc:
        raise InstallError(f"AAAA self-check failed: {exc}") from exc
    address6 = rdata_ip(aaaa.answers[0]) if aaaa.answers else None
    say(f"self-check: doubleclick.net AAAA -> {address6}")
    if address6 != config.block_ipv6:
        raise InstallError(f"AAAA self-check returned {address6!r}, expected {config.block_ipv6}")

    try:
        forwarded = lookup(host, port, "example.com", A, timeout=config.upstream_timeout + 1)
    except (OSError, DnsError) as exc:
        say(f"warning: example.com forward failed ({exc}). Blocking works; check the upstream resolver.")
        return
    address = rdata_ip(forwarded.answers[0]) if forwarded.answers else None
    if address and address != config.block_ipv4:
        say(f"self-check: example.com A -> {address}")
        return
    say(
        f"warning: example.com did not return a public address ({address!r}). "
        "Blocking works; check the upstream resolver."
    )


def _query_target(config) -> tuple[str, int]:
    listen = config.listen
    host = "127.0.0.1" if listen in {"0.0.0.0", "::", ""} else listen
    return host, config.port


def _finish(config) -> None:
    ip = _lan_ip()
    say("")
    say("Pi Adblock is installed and will start on boot.")
    say(f"DNS is listening on port {config.port}.")
    say(f"Point your router DHCP DNS at {ip}.")
    say(f"Status page: http://{ip}:8080/")
    say(f"Check a block: nslookup doubleclick.net {ip}")
    say("done")


def _lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _dump_journal() -> None:
    if shutil.which("journalctl") is None:
        return
    subprocess.run(
        ["journalctl", "-u", "piadblock", "-n", "40", "--no-pager"],
        check=False,
    )


def _is_active(unit: str) -> bool:
    result = _systemctl("is-active", unit)
    return result.returncode == 0


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    if shutil.which("systemctl") is None:
        return subprocess.CompletedProcess(["systemctl", *args], 127, "", "systemctl not found")
    return subprocess.run(["systemctl", *args], check=False, text=True, capture_output=True)


def _load_state() -> dict:
    try:
        data = json.loads(Path(INSTALL_STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    atomic_write(INSTALL_STATE_PATH, json.dumps(state, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
