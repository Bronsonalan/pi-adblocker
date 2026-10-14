"""Download blocklists and merge them into one domain file."""

from __future__ import annotations

import fcntl
import json
import logging
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from piadblock.blocklist import parse_domain_list
from piadblock.config import Config
from piadblock.files import atomic_write

logger = logging.getLogger("piadblock")


class UpdaterBusy(Exception):
    """Another update holds the lock."""


@dataclass
class UpdateResult:
    ok: bool
    domain_count: int = 0
    kept_existing: bool = False
    skipped: bool = False
    sources: list[dict] = field(default_factory=list)
    error: str = ""


def blocklist_is_due(config: Config, now: float | None = None) -> bool:
    path = Path(config.merged_path)
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return True
    return age >= config.update_interval_hours * 3600


def _existing_count(config: Config) -> int:
    meta_path = Path(config.meta_path)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return int(meta.get("domain_count", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        text = Path(config.merged_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if line and not line.startswith("#"))


def _download(url: str, timeout: float, max_bytes: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "piadblock/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise OSError(f"response larger than {max_bytes} bytes")
            chunks.append(chunk)
        return b"".join(chunks)


@contextmanager
def _lock(path: str):
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise UpdaterBusy("another update is running") from exc
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def update_blocklists(config: Config, *, force: bool = False, lock_path: str | None = None) -> UpdateResult:
    """Download every configured list and atomically replace the merged file.

    With ``force=False`` the download is skipped when the merged file is
    younger than ``update_interval_hours``. A failed download never replaces
    a good merged file with an empty one. An empty ``blocklists`` list
    (the default) does not contact the network.
    """
    if not config.blocklists:
        return UpdateResult(
            ok=True,
            domain_count=_existing_count(config),
            kept_existing=True,
            skipped=True,
            error="no blocklist URLs configured",
        )
    if not force and not blocklist_is_due(config):
        return UpdateResult(
            ok=True,
            domain_count=_existing_count(config),
            kept_existing=True,
            skipped=True,
        )

    lock = lock_path or str(Path(config.blocklist_dir) / "update.lock")
    try:
        with _lock(lock):
            return _update_locked(config)
    except UpdaterBusy as exc:
        logger.info("%s", exc)
        return UpdateResult(
            ok=True,
            domain_count=_existing_count(config),
            kept_existing=True,
            skipped=True,
            error=str(exc),
        )


def _update_locked(config: Config) -> UpdateResult:
    merged: set[str] = set()
    sources: list[dict] = []
    any_ok = False
    for url in config.blocklists:
        entry = {"url": url, "ok": False, "domains": 0, "error": ""}
        try:
            payload = _download(url, config.download_timeout, config.download_max_bytes)
            text = payload.decode("utf-8-sig", errors="replace")
            found = parse_domain_list(text, skip_never_block=True)
            entry["domains"] = len(found)
            entry["ok"] = True
            any_ok = True
            merged |= found
            logger.info("blocklist %s: %s domains", url, len(found))
        except (OSError, urllib.error.URLError, UnicodeError, TimeoutError) as exc:
            entry["error"] = str(exc)[:300]
            logger.warning("blocklist %s failed: %s", url, entry["error"])
        sources.append(entry)

    existing = Path(config.merged_path)
    if not merged:
        error = "no domains downloaded" if not any_ok else "lists contained no domains"
        if existing.exists():
            logger.warning("%s; keeping the existing blocklist", error)
            return UpdateResult(
                ok=False,
                domain_count=_existing_count(config),
                kept_existing=True,
                sources=sources,
                error=error,
            )
        _write_meta(config, 0, sources, ok=False, error=error)
        return UpdateResult(ok=False, domain_count=0, sources=sources, error=error)

    header = (
        f"# piadblock merged {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"domains={len(merged)}\n"
    )
    body = header + "\n".join(sorted(merged)) + "\n"
    atomic_write(config.merged_path, body)
    _write_meta(config, len(merged), sources, ok=True, error="")
    logger.info("merged %s domains into %s", len(merged), config.merged_path)
    return UpdateResult(ok=True, domain_count=len(merged), sources=sources)


def _write_meta(config: Config, count: int, sources: list[dict], *, ok: bool, error: str) -> None:
    meta = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ok": ok,
        "domain_count": count,
        "error": error,
        "sources": sources,
    }
    try:
        atomic_write(config.meta_path, json.dumps(meta, indent=2) + "\n")
    except OSError as exc:
        logger.warning("could not write %s: %s", config.meta_path, exc)
