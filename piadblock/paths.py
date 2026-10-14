"""Default filesystem locations used by the installer and the server."""

from __future__ import annotations

OPT_ROOT = "/opt/piadblock"
CONFIG_DIR = "/etc/piadblock"
STATE_DIR = "/var/lib/piadblock"
LOG_DIR = "/var/log/piadblock"
CONFIG_PATH = CONFIG_DIR + "/config.json"
ALLOWLIST_PATH = CONFIG_DIR + "/allowlist.txt"
DENYLIST_PATH = CONFIG_DIR + "/denylist.txt"
BLOCKLIST_DIR = STATE_DIR + "/blocklists"
MERGED_PATH = BLOCKLIST_DIR + "/merged.txt"
META_PATH = BLOCKLIST_DIR + "/meta.json"
STATS_PATH = STATE_DIR + "/stats.json"
LOG_PATH = LOG_DIR + "/piadblock.log"
INSTALL_STATE_PATH = STATE_DIR + "/install-state.json"
UPDATE_LOCK_PATH = STATE_DIR + "/update.lock"

SERVICE_NAME = "piadblock"
UPDATE_SERVICE_NAME = "piadblock-update"
UPDATE_TIMER_NAME = "piadblock-update.timer"

# Marker wrapped around the lines we add to dhcpcd.conf so uninstall can
# remove them without touching the rest of the file.
DHCPCD_BEGIN = "# BEGIN piadblock"
DHCPCD_END = "# END piadblock"

RESOLVED_DROPIN = "/etc/systemd/resolved.conf.d/piadblock.conf"
NM_CONF = "/etc/NetworkManager/conf.d/piadblock-dns.conf"
DHCPCD_CONF = "/etc/dhcpcd.conf"
RESOLV_CONF = "/etc/resolv.conf"

# No remote lists in v1. The denylist is the block list. URLs can be added later.
DEFAULT_BLOCKLISTS = ()
