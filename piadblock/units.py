"""systemd unit text. The installer fills in the Python and install paths."""

from __future__ import annotations


def service_unit(python: str, root: str, kill: str) -> str:
    return f"""[Unit]
Description=Pi Adblock DNS server
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory={root}
Environment=PYTHONPATH={root}
Environment=PYTHONUNBUFFERED=1
ExecStart={python} -m piadblock serve
ExecReload={kill} -HUP $MAINPID
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
"""


def update_service_unit(python: str, root: str) -> str:
    return f"""[Unit]
Description=Update Pi Adblock blocklists

[Service]
Type=oneshot
WorkingDirectory={root}
Environment=PYTHONPATH={root}
Environment=PYTHONUNBUFFERED=1
ExecStart={python} -m piadblock update --if-due
"""


def update_timer_unit() -> str:
    return """[Unit]
Description=Hourly check for Pi Adblock blocklist updates

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=15min

[Install]
WantedBy=timers.target
"""
