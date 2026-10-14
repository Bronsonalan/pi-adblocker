"""systemd unit text. The installer fills in the Python and install paths."""

from __future__ import annotations


def service_unit(python: str, root: str, kill: str, prefix: str = "") -> str:
    # A prefixed install has to pass the same root to the service. A normal
    # install leaves this line out so the unit matches a stock Pi.
    prefix_line = f"Environment=PIADBLOCK_PREFIX={prefix}\n" if prefix else ""
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
{prefix_line}ExecStart={python} -m piadblock serve
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


def git_oneshot_unit(
    description: str,
    python: str,
    root: str,
    command: str,
    *,
    prefix: str = "",
    git_user: str = "",
    remote: str = "",
) -> str:
    """Root oneshot. Git itself runs as ``git_user`` via runuser inside the command.

    The process stays root so it can signal the DNS server. OpenSSH will not
    use techexplain's deploy key from a root ssh process.
    """
    lines = [
        f"Environment=PYTHONPATH={root}",
        "Environment=PYTHONUNBUFFERED=1",
    ]
    if prefix:
        lines.append(f"Environment=PIADBLOCK_PREFIX={prefix}")
    if git_user:
        lines.append(f"Environment=PIADBLOCK_GIT_USER={git_user}")
    if remote:
        lines.append(f"Environment=PIADBLOCK_REMOTE={remote}")
    env = "\n".join(lines)
    return f"""[Unit]
Description={description}
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory={root}
{env}
ExecStart={python} -m piadblock {command}
"""


def calendar_timer_unit(description: str, calendar: str) -> str:
    return f"""[Unit]
Description={description}

[Timer]
OnCalendar={calendar}
AccuracySec=5s
Persistent=true

[Install]
WantedBy=timers.target
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
