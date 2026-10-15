"""Default filesystem locations used by the installer and the server."""

from __future__ import annotations

import os
from pathlib import Path

# Paths as they are on a real Pi. config.example.json stores these strings.
CANONICAL_OPT_ROOT = "/opt/piadblock"
CANONICAL_CONFIG_DIR = "/etc/piadblock"
CANONICAL_STATE_DIR = "/var/lib/piadblock"
CANONICAL_LOG_DIR = "/var/log/piadblock"
CANONICAL_UNIT_DIR = "/etc/systemd/system"
CANONICAL_RESOLVED_DROPIN = "/etc/systemd/resolved.conf.d/piadblock.conf"
CANONICAL_NM_CONF = "/etc/NetworkManager/conf.d/piadblock-dns.conf"
CANONICAL_DHCPCD_CONF = "/etc/dhcpcd.conf"
CANONICAL_RESOLV_CONF = "/etc/resolv.conf"

# Config keys the example file sets to absolute paths. A prefixed install
# rewrites these so the service reads the copy under PIADBLOCK_PREFIX.
CANONICAL_CONFIG_PATHS = {
    "blocklist_dir": CANONICAL_STATE_DIR + "/blocklists",
    "allowlist": CANONICAL_CONFIG_DIR + "/allowlist.txt",
    "denylist": CANONICAL_OPT_ROOT + "/blocklist.txt",
    "log_file": CANONICAL_LOG_DIR + "/piadblock.log",
    "stats_file": CANONICAL_STATE_DIR + "/stats.json",
}


def _snapshot_prefix() -> str:
    """Read PIADBLOCK_PREFIX once, when this module is first imported.

    Unset (the normal install) keeps every path at its canonical location.
    A test sets the variable to a temp directory before starting Python so
    the installer and the service it launches both stay inside that tree.
    """
    raw = os.environ.get("PIADBLOCK_PREFIX", "").strip()
    if not raw:
        return ""
    return str(Path(raw).expanduser().resolve())


PREFIX = _snapshot_prefix()


def locate(canonical: str) -> str:
    """Return ``canonical``, or that absolute path under ``PIADBLOCK_PREFIX``."""
    if not PREFIX:
        return canonical
    return str(Path(PREFIX) / canonical.lstrip("/"))


OPT_ROOT = locate(CANONICAL_OPT_ROOT)
CONFIG_DIR = locate(CANONICAL_CONFIG_DIR)
STATE_DIR = locate(CANONICAL_STATE_DIR)
LOG_DIR = locate(CANONICAL_LOG_DIR)
CONFIG_PATH = CONFIG_DIR + "/config.json"
ALLOWLIST_PATH = CONFIG_DIR + "/allowlist.txt"
DENYLIST_PATH = OPT_ROOT + "/blocklist.txt"
LEGACY_DENYLIST_PATH = CONFIG_DIR + "/denylist.txt"
BLOCKLIST_DIR = STATE_DIR + "/blocklists"
MERGED_PATH = BLOCKLIST_DIR + "/merged.txt"
META_PATH = BLOCKLIST_DIR + "/meta.json"
STATS_PATH = STATE_DIR + "/stats.json"
LOG_PATH = LOG_DIR + "/piadblock.log"
INSTALL_STATE_PATH = STATE_DIR + "/install-state.json"
UPDATE_LOCK_PATH = STATE_DIR + "/update.lock"
UNIT_DIR = locate(CANONICAL_UNIT_DIR)

SERVICE_NAME = "piadblock"
UPDATE_SERVICE_NAME = "piadblock-update"
UPDATE_TIMER_NAME = "piadblock-update.timer"
PULL_SERVICE_NAME = "piadblock-pull.service"
PULL_TIMER_NAME = "piadblock-pull.timer"
HEARTBEAT_SERVICE_NAME = "piadblock-heartbeat.service"
HEARTBEAT_TIMER_NAME = "piadblock-heartbeat.timer"
STATS_SERVICE_NAME = "piadblock-stats.service"
STATS_TIMER_NAME = "piadblock-stats.timer"
CRT_SERVICE_NAME = "piadblock-crt.service"

# SSH remote. The Pi pushes and pulls with a deploy key, not HTTPS.
DEFAULT_REMOTE = "git@github.com:Bronsonalan/pi-adblocker.git"

# Marker wrapped around the lines we add to dhcpcd.conf so uninstall can
# remove them without touching the rest of the file.
DHCPCD_BEGIN = "# BEGIN piadblock"
DHCPCD_END = "# END piadblock"

RESOLVED_DROPIN = locate(CANONICAL_RESOLVED_DROPIN)
NM_CONF = locate(CANONICAL_NM_CONF)
DHCPCD_CONF = locate(CANONICAL_DHCPCD_CONF)
RESOLV_CONF = locate(CANONICAL_RESOLV_CONF)

# No remote lists in v1. The denylist is the block list. URLs can be added later.
DEFAULT_BLOCKLISTS = ()
