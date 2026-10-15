"""JSON configuration with defaults that work on a Raspberry Pi."""

from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse

from piadblock.paths import (
    ALLOWLIST_PATH,
    BLOCKLIST_DIR,
    CONFIG_PATH,
    DEFAULT_BLOCKLISTS,
    DENYLIST_PATH,
    LOG_PATH,
    STATS_PATH,
)

logger = logging.getLogger("piadblock")

# The previous default was a single resolver and a 2 second timeout. A Pi
# that still has that file is migrated by the installer. Any other list or
# timeout is left alone.
DEFAULT_UPSTREAM = ["1.1.1.1", "1.0.0.1", "9.9.9.9"]
LEGACY_UPSTREAM = ["1.1.1.1"]
LEGACY_UPSTREAM_TIMEOUT = 2.0


class ConfigError(ValueError):
    """The configuration file is missing a required shape or has a bad value."""


def _as_bool(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _as_int(name: str, value: object, *, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer")
    if value < low or value > high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return value


def _as_float(name: str, value: object, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number")
    number = float(value)
    if number < low or number > high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return number


def _as_str(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _as_ip(name: str, value: object, family: int | None = None) -> str:
    text = _as_str(name, value)
    families = (family,) if family is not None else (socket.AF_INET, socket.AF_INET6)
    for fam in families:
        try:
            socket.inet_pton(fam, text)
        except OSError:
            continue
        return text
    expected = "an IP address"
    if family == socket.AF_INET:
        expected = "an IPv4 address"
    elif family == socket.AF_INET6:
        expected = "an IPv6 address"
    raise ConfigError(f"{name} must be {expected}")


def _as_string_list(name: str, value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name} must be a non-empty list")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{name} must contain only non-empty strings")
        items.append(item.strip())
    return items


def _as_url_list(name: str, value: object) -> list[str]:
    """Blocklist URLs. An empty list is allowed (denylist-only mode)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list of http(s) URLs")
    urls = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{name} must contain only URL strings")
        url = item.strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError(f"{name} entry is not an http(s) URL: {url}")
        urls.append(url)
    return urls


@dataclass
class Config:
    """Runtime settings.

    Paths default to the locations the installer creates. Override them in
    ``config.json`` or by constructing a ``Config`` in tests.
    """

    listen: str = "0.0.0.0"
    port: int = 53
    tcp: bool = True
    ipv6: bool = True
    upstream: list[str] = field(default_factory=lambda: list(DEFAULT_UPSTREAM))
    upstream_port: int = 53
    upstream_timeout: float = 1.0
    cache_size: int = 2048
    cache_min_ttl: int = 0
    cache_max_ttl: int = 86400
    cache_negative_ttl: int = 300
    block_ipv4: str = "0.0.0.0"
    block_ipv6: str = "::"
    block_ttl: int = 60
    blocklists: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKLISTS))
    blocklist_dir: str = BLOCKLIST_DIR
    allowlist: str = ALLOWLIST_PATH
    denylist: str = DENYLIST_PATH
    update_interval_hours: int = 24
    log_file: str = LOG_PATH
    log_level: str = "INFO"
    stats_file: str = STATS_PATH
    status_enabled: bool = True
    status_listen: str = "0.0.0.0"
    status_port: int = 8080
    workers: int = 32
    download_timeout: float = 60.0
    download_max_bytes: int = 50_000_000
    auto_update: bool = False
    auto_reload: bool = True

    def to_public_dict(self) -> dict:
        return asdict(self)

    @property
    def merged_path(self) -> str:
        return str(Path(self.blocklist_dir) / "merged.txt")

    @property
    def meta_path(self) -> str:
        return str(Path(self.blocklist_dir) / "meta.json")


def migrate_legacy_upstream(data: dict) -> dict | None:
    """Return a rewritten document, or None when the upstream settings stay.

    Only the old default (exactly ``1.1.1.1``, and a timeout of exactly 2
    seconds) is replaced. A custom list such as ``8.8.8.8`` is a choice.
    """
    if not isinstance(data, dict):
        return None
    updated = dict(data)
    changed = False
    if updated.get("upstream") == LEGACY_UPSTREAM:
        updated["upstream"] = list(DEFAULT_UPSTREAM)
        changed = True
    timeout = updated.get("upstream_timeout")
    if timeout == LEGACY_UPSTREAM_TIMEOUT or timeout == 2:
        updated["upstream_timeout"] = 1.0
        changed = True
    if not changed:
        return None
    return updated


def _validate(data: dict) -> Config:
    listen = _as_ip("listen", data["listen"]) if "listen" in data else "0.0.0.0"
    port = _as_int("port", data["port"], low=0, high=65535) if "port" in data else 53
    tcp = _as_bool("tcp", data["tcp"]) if "tcp" in data else True
    ipv6 = _as_bool("ipv6", data["ipv6"]) if "ipv6" in data else True
    if "upstream" in data:
        upstream = [_as_ip(f"upstream[{i}]", item) for i, item in enumerate(_as_string_list("upstream", data["upstream"]))]
    else:
        upstream = list(DEFAULT_UPSTREAM)
    upstream_port = (
        _as_int("upstream_port", data["upstream_port"], low=1, high=65535)
        if "upstream_port" in data
        else 53
    )
    upstream_timeout = (
        _as_float("upstream_timeout", data["upstream_timeout"], low=0.1, high=30)
        if "upstream_timeout" in data
        else 1.0
    )
    cache_size = (
        _as_int("cache_size", data["cache_size"], low=0, high=1_000_000)
        if "cache_size" in data
        else 2048
    )
    cache_min_ttl = (
        _as_int("cache_min_ttl", data["cache_min_ttl"], low=0, high=86400 * 7)
        if "cache_min_ttl" in data
        else 0
    )
    cache_max_ttl = (
        _as_int("cache_max_ttl", data["cache_max_ttl"], low=0, high=86400 * 7)
        if "cache_max_ttl" in data
        else 86400
    )
    if cache_max_ttl and cache_min_ttl > cache_max_ttl:
        raise ConfigError("cache_min_ttl cannot be greater than cache_max_ttl")
    cache_negative_ttl = (
        _as_int("cache_negative_ttl", data["cache_negative_ttl"], low=0, high=86400)
        if "cache_negative_ttl" in data
        else 300
    )
    block_ipv4 = (
        _as_ip("block_ipv4", data["block_ipv4"], socket.AF_INET)
        if "block_ipv4" in data
        else "0.0.0.0"
    )
    block_ipv6 = (
        _as_ip("block_ipv6", data["block_ipv6"], socket.AF_INET6)
        if "block_ipv6" in data
        else "::"
    )
    block_ttl = (
        _as_int("block_ttl", data["block_ttl"], low=0, high=86400 * 7)
        if "block_ttl" in data
        else 60
    )
    blocklists = _as_url_list("blocklists", data["blocklists"]) if "blocklists" in data else list(DEFAULT_BLOCKLISTS)
    blocklist_dir = _as_str("blocklist_dir", data["blocklist_dir"]) if "blocklist_dir" in data else BLOCKLIST_DIR
    allowlist = data.get("allowlist", ALLOWLIST_PATH)
    denylist = data.get("denylist", DENYLIST_PATH)
    if not isinstance(allowlist, str):
        raise ConfigError("allowlist must be a path string")
    if not isinstance(denylist, str):
        raise ConfigError("denylist must be a path string")
    update_interval_hours = (
        _as_int("update_interval_hours", data["update_interval_hours"], low=1, high=24 * 30)
        if "update_interval_hours" in data
        else 24
    )
    log_file = data.get("log_file", LOG_PATH)
    if not isinstance(log_file, str):
        raise ConfigError("log_file must be a string (empty disables the log file)")
    log_level = str(data.get("log_level", "INFO")).upper()
    if log_level not in logging._nameToLevel:
        raise ConfigError(f"log_level must be one of {', '.join(sorted(logging._nameToLevel))}")
    stats_file = data.get("stats_file", STATS_PATH)
    if not isinstance(stats_file, str):
        raise ConfigError("stats_file must be a string")
    status_enabled = _as_bool("status_enabled", data["status_enabled"]) if "status_enabled" in data else True
    status_listen = (
        _as_ip("status_listen", data["status_listen"]) if "status_listen" in data else "0.0.0.0"
    )
    status_port = (
        _as_int("status_port", data["status_port"], low=1, high=65535)
        if "status_port" in data
        else 8080
    )
    workers = _as_int("workers", data["workers"], low=1, high=256) if "workers" in data else 32
    download_timeout = (
        _as_float("download_timeout", data["download_timeout"], low=1, high=600)
        if "download_timeout" in data
        else 60.0
    )
    download_max_bytes = (
        _as_int("download_max_bytes", data["download_max_bytes"], low=1000, high=100_000_000)
        if "download_max_bytes" in data
        else 50_000_000
    )
    auto_update = _as_bool("auto_update", data["auto_update"]) if "auto_update" in data else False
    auto_reload = _as_bool("auto_reload", data["auto_reload"]) if "auto_reload" in data else True
    return Config(
        listen=listen,
        port=port,
        tcp=tcp,
        ipv6=ipv6,
        upstream=upstream,
        upstream_port=upstream_port,
        upstream_timeout=upstream_timeout,
        cache_size=cache_size,
        cache_min_ttl=cache_min_ttl,
        cache_max_ttl=cache_max_ttl,
        cache_negative_ttl=cache_negative_ttl,
        block_ipv4=block_ipv4,
        block_ipv6=block_ipv6,
        block_ttl=block_ttl,
        blocklists=blocklists,
        blocklist_dir=blocklist_dir,
        allowlist=allowlist,
        denylist=denylist,
        update_interval_hours=update_interval_hours,
        log_file=log_file,
        log_level=log_level,
        stats_file=stats_file,
        status_enabled=status_enabled,
        status_listen=status_listen,
        status_port=status_port,
        workers=workers,
        download_timeout=download_timeout,
        download_max_bytes=download_max_bytes,
        auto_update=auto_update,
        auto_reload=auto_reload,
    )


def load_config(path: str | None = None) -> Config:
    """Load ``path``, or the installed config, or built-in defaults.

    Search order when ``path`` is omitted:

    1. ``PIADBLOCK_CONFIG`` environment variable
    2. ``/etc/piadblock/config.json`` if that file exists
    3. Built-in defaults
    """
    if path is None:
        path = os.environ.get("PIADBLOCK_CONFIG") or None
    if path is None and os.path.exists(CONFIG_PATH):
        path = CONFIG_PATH
    if path is None:
        return Config()

    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: config must be a JSON object")

    known = {item.name for item in fields(Config)}
    unknown = sorted(key for key in data if key not in known)
    for key in unknown:
        logger.warning("ignoring unknown config key %s", key)
    config = _validate(data)
    config.unknown_keys = unknown  # type: ignore[attr-defined]
    return config
