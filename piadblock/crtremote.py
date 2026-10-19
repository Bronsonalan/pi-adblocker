"""Read-only CRT client for a Pi Zero, and the composite installer.

The Zero draws the same frames as ``piadblock-crt``. It GETs ``/api/crt``
from the main Pi and does not run DNS. A down server stays on a waiting
screen; the process keeps sleeping and trying.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from piadblock.crt import run_display

DEFAULT_BASE_URL = "http://172.20.1.217:8080"
CANONICAL_ROOT = "/opt/piadblock-crt-remote"
CANONICAL_CONFIG = "/etc/piadblock-crt-remote/config.json"
CANONICAL_UNIT_DIR = "/etc/systemd/system"
SERVICE_NAME = "piadblock-crt-remote.service"
MARK_BEGIN = "# BEGIN piadblock-crt-remote"
MARK_END = "# END piadblock-crt-remote"
COMPOSITE_VIDEO = "video=Composite-1:720x480@60ie"
BACKUP_SUFFIX = ".piadblock-bak"
POLL_SECONDS = 1.0
WAIT_SECONDS = 2.0
FETCH_TIMEOUT = 1.5

_BLOCK = re.compile(
    re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?",
    re.DOTALL,
)

CLIENT_FILES = (
    "crt-remote.py",
    "piadblock/__init__.py",
    "piadblock/crt.py",
    "piadblock/crtremote.py",
)


def under(prefix: str, canonical: str) -> Path:
    if not prefix:
        return Path(canonical)
    return Path(prefix) / canonical.lstrip("/")


def normalize_base(url: str) -> str:
    text = url.strip().rstrip("/")
    for suffix in ("/api/crt", "/api/stats"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text.rstrip("/")


def feed_url(base: str) -> str:
    return normalize_base(base) + "/api/crt"


def resolve_base_url(flag: str, env: str | None, config_url: str | None) -> str:
    """Flag, then ``PIADBLOCK_CRT_URL``, then the config file, then the default."""
    for candidate in (flag, env or "", config_url or "", DEFAULT_BASE_URL):
        if candidate and str(candidate).strip():
            return normalize_base(str(candidate))
    return DEFAULT_BASE_URL


def host_of(base: str) -> str:
    parsed = urlparse(base if "://" in base else "http://" + base)
    return parsed.hostname or base


def waiting_label_for(base: str) -> str:
    return f"waiting for {host_of(base)}"


def load_config_url(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    url = data.get("url")
    if isinstance(url, str) and url.strip():
        return url.strip()
    return None


def read_model_text(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return " ".join(raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").split())


def board_kind(model: str) -> str:
    """Zero 2 W and Zero W are the two boards this client is built for."""
    if "Zero 2 W" in model:
        return "Zero 2 W"
    if "Zero W" in model:
        return "Zero W"
    if "Zero" in model:
        return "Zero"
    return "unknown"


def _kms_value(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or not stripped.startswith("dtoverlay="):
        return ""
    value = stripped.split("=", 1)[1].strip()
    if value == "vc4-kms-v3d" or value.startswith("vc4-kms-v3d,"):
        return value
    return ""


def uses_kms(text: str) -> bool:
    return any(_kms_value(line) for line in text.splitlines())


def _add_composite(line: str) -> str:
    value = _kms_value(line)
    if not value:
        return line
    parts = [part for part in value.split(",") if part]
    if "composite" in parts:
        return line
    indent = line[: len(line) - len(line.lstrip())]
    return indent + "dtoverlay=" + ",".join(parts + ["composite"])


def patch_kms_overlay(text: str) -> str:
    lines = text.splitlines()
    out = [_add_composite(line) for line in lines]
    if out == lines:
        return text
    body = "\n".join(out)
    if text.endswith("\n"):
        body += "\n"
    return body


def _marked_block(kms: bool) -> str:
    if kms:
        inner = (
            "# composite NTSC via dtoverlay vc4-kms-v3d,composite "
            "and cmdline video=Composite-1:720x480@60ie"
        )
        return "\n".join((MARK_BEGIN, inner, MARK_END))
    return "\n".join((MARK_BEGIN, "enable_tvout=1", "sdtv_mode=0", MARK_END))


def upsert_block(text: str, block: str) -> str:
    replacement = block + "\n"
    if _BLOCK.search(text):
        updated = _BLOCK.sub(replacement, text, count=1)
    else:
        base = text
        if base and not base.endswith("\n"):
            base += "\n"
        updated = base + replacement
    return text if updated == text else updated


def ensure_cmdline(text: str) -> str:
    if COMPOSITE_VIDEO in text.split():
        return text
    if not text.strip():
        return COMPOSITE_VIDEO + "\n"
    ends_nl = text.endswith("\n")
    body = text[:-1] if ends_nl else text
    first, sep, rest = body.partition("\n")
    if COMPOSITE_VIDEO in first.split():
        return text
    first = (first.rstrip() + " " + COMPOSITE_VIDEO).strip()
    merged = first if not sep else first + "\n" + rest
    if ends_nl or not merged.endswith("\n"):
        if not merged.endswith("\n"):
            merged += "\n"
    return merged


def boot_config_path(prefix: str) -> Path | None:
    """Bookworm and later keep the file under /boot/firmware. Older releases use /boot."""
    firmware = under(prefix, "/boot/firmware/config.txt")
    legacy = under(prefix, "/boot/config.txt")
    if firmware.is_file():
        return firmware
    if legacy.is_file():
        return legacy
    return None


def _backup_once(path: Path) -> Path | None:
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.exists():
        return None
    shutil.copy2(path, backup)
    return backup


def apply_composite(config_path: Path) -> list[str]:
    """Edit config.txt and, for KMS, cmdline.txt. Return the lines to print."""
    original = config_path.read_text(encoding="utf-8")
    kms = uses_kms(original)
    updated = patch_kms_overlay(original) if kms else original
    updated = upsert_block(updated, _marked_block(kms))
    cmdline_path = config_path.parent / "cmdline.txt"
    cmdline_original = ""
    cmdline_updated = ""
    lines: list[str] = []
    if kms and cmdline_path.is_file():
        cmdline_original = cmdline_path.read_text(encoding="utf-8")
        cmdline_updated = ensure_cmdline(cmdline_original)
    elif kms:
        lines.append(f"cmdline: {cmdline_path} is missing")
    config_changed = updated != original
    cmdline_changed = bool(kms and cmdline_path.is_file() and cmdline_updated != cmdline_original)
    if config_changed:
        wrote = _backup_once(config_path)
        if wrote is not None:
            lines.append(f"backup: wrote {wrote}")
        config_path.write_text(updated, encoding="utf-8")
        if kms:
            lines.append(f"composite: KMS, set dtoverlay composite in {config_path}")
        else:
            lines.append(
                f"composite: legacy firmware, set enable_tvout=1 and sdtv_mode=0 in {config_path}"
            )
    elif kms:
        lines.append(f"composite: {config_path} already set for KMS")
    else:
        lines.append(f"composite: {config_path} already set for legacy firmware")
    if kms and cmdline_path.is_file():
        if cmdline_changed:
            wrote = _backup_once(cmdline_path)
            if wrote is not None:
                lines.append(f"backup: wrote {wrote}")
            cmdline_path.write_text(cmdline_updated, encoding="utf-8")
            lines.append(f"cmdline: added {COMPOSITE_VIDEO} to {cmdline_path}")
        else:
            lines.append(f"cmdline: {cmdline_path} already has {COMPOSITE_VIDEO}")
    if config_changed or cmdline_changed:
        lines.append("reboot: required for composite NTSC. Not rebooting.")
    return lines


def remote_service_unit(python: str, install_root: str, config_path: str) -> str:
    from piadblock.units import console_restore_command

    restore = console_restore_command(SERVICE_NAME)
    launcher = str(Path(install_root) / "crt-remote.py")
    return f"""[Unit]
Description=Pi Adblock remote CRT display
After=network-online.target getty@tty1.service
Wants=network-online.target
Conflicts=getty@tty1.service

[Service]
Type=simple
Nice=10
WorkingDirectory={install_root}
Environment=PYTHONUNBUFFERED=1
Environment=PIADBLOCK_CRT_CONFIG={config_path}
ExecStart={python} {launcher} --tty /dev/tty1 --config {config_path}
StandardInput=null
StandardOutput=journal
StandardError=journal
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
Restart=on-failure
RestartSec=2
ExecStopPost=-/bin/sh -c '{restore}'

[Install]
WantedBy=multi-user.target
"""


def _copy_client(source: Path, dest_root: Path) -> list[str]:
    lines: list[str] = []
    for rel in CLIENT_FILES:
        src = source / rel
        dst = dest_root / rel
        data = src.read_bytes()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file() and dst.read_bytes() == data:
            lines.append(f"files: {rel} unchanged")
            continue
        dst.write_bytes(data)
        dst.chmod(0o755 if rel == "crt-remote.py" else 0o644)
        lines.append(f"files: installed {dst}")
    return lines


def _ensure_config(path: Path) -> list[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        return [f"config: kept {path}"]
    path.write_text(json.dumps({"url": DEFAULT_BASE_URL}, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)
    return [f"config: wrote {path}"]


def _default_systemctl(args: list[str]) -> None:
    subprocess.run(["systemctl", *args], check=False)


def install_remote(
    *,
    source: Path,
    prefix: str = "",
    model_path: Path | None = None,
    systemctl=None,
) -> list[str]:
    """Install the client under ``prefix`` (empty means the real Pi paths)."""
    lines: list[str] = []
    model = read_model_text(model_path if model_path is not None else Path("/proc/device-tree/model"))
    if model:
        lines.append(f"model: {model} ({board_kind(model)})")
    else:
        lines.append("model: unknown")
    root = under(prefix, CANONICAL_ROOT)
    config = under(prefix, CANONICAL_CONFIG)
    unit_dir = under(prefix, CANONICAL_UNIT_DIR)
    lines.extend(_copy_client(source, root))
    lines.extend(_ensure_config(config))
    boot = boot_config_path(prefix)
    if boot is None:
        lines.append("composite: no config.txt found")
    else:
        lines.append(f"boot config: {boot}")
        lines.extend(apply_composite(boot))
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = unit_dir / SERVICE_NAME
    python = shutil.which("python3") or sys.executable
    text = remote_service_unit(python, str(root), str(config))
    if unit_path.is_file() and unit_path.read_text(encoding="utf-8") == text:
        lines.append(f"systemd: {unit_path} unchanged")
    else:
        unit_path.write_text(text, encoding="utf-8")
        lines.append(f"systemd: wrote {unit_path}")
    runner = systemctl or _default_systemctl
    runner(["daemon-reload"])
    runner(["enable", "--now", SERVICE_NAME])
    lines.append(f"systemd: enable --now {SERVICE_NAME}")
    return lines


def install_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crt_remote_install.py")
    parser.add_argument("--source", default="")
    parser.add_argument("--prefix", default="")
    parser.add_argument("--model", default="")
    args = parser.parse_args(argv)
    source = Path(args.source) if args.source else Path(__file__).resolve().parents[1]
    prefix = args.prefix or os.environ.get("PIADBLOCK_PREFIX", "")
    model_env = args.model or os.environ.get("PIADBLOCK_CRT_MODEL", "")
    model = Path(model_env) if model_env else Path("/proc/device-tree/model")
    for line in install_remote(source=source, prefix=prefix, model_path=model):
        print(line, flush=True)
    print("done", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crt-remote", description="Fullscreen CRT fed by the main Pi")
    parser.add_argument("--tty", default="/dev/tty1")
    parser.add_argument("--url", default="", help="status page base URL; overrides the env and the config file")
    parser.add_argument("--config", default="", help="config.json path")
    args = parser.parse_args(argv)
    if args.config:
        config_path = Path(args.config)
    else:
        config_path = Path(os.environ.get("PIADBLOCK_CRT_CONFIG", CANONICAL_CONFIG))
    base = resolve_base_url(args.url, os.environ.get("PIADBLOCK_CRT_URL"), load_config_url(config_path))
    return run_display(
        args.tty,
        feed_url(base),
        waiting_label=waiting_label_for(base),
        period=POLL_SECONDS,
        wait_period=WAIT_SECONDS,
        timeout=FETCH_TIMEOUT,
    )
