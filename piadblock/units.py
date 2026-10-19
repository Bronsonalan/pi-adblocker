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


def console_restore_command(unit: str) -> str:
    """Shell for ExecStopPost. Hand tty1 back to getty when the unit is stopping.

    ``systemctl restart`` still has a restart job queued while this runs.
    Starting ``getty@tty1`` in that window conflicts with this unit, and
    systemd cancels the restart ("Job for <unit> canceled"). A queued
    restart leaves the console with the new process.
    """
    escaped = unit.replace(".", r"\.")
    pattern = (
        f"^[[:space:]]*[0-9]+[[:space:]]+{escaped}[[:space:]]+restart([[:space:]]|$)"
    )
    return (
        "jobs=$(systemctl list-jobs --no-legend --full 2>/dev/null || true); "
        'printf "%s\\n" "$jobs" | '
        f'grep -E -q "{pattern}" '
        "|| systemctl start getty@tty1.service"
    )


def crt_service_unit(python: str, root: str, prefix: str = "") -> str:
    """Console feed. The installer writes this unit and does not start it.

    Starting it takes tty1 away from getty. Stopping it hands tty1 back.
    """
    prefix_line = f"Environment=PIADBLOCK_PREFIX={prefix}\n" if prefix else ""
    restore = console_restore_command("piadblock-crt.service")
    return f"""[Unit]
Description=Pi Adblock CRT lookup display
After=piadblock.service
Wants=piadblock.service
Conflicts=getty@tty1.service

[Service]
Type=simple
WorkingDirectory={root}
Environment=PYTHONPATH={root}
Environment=PYTHONUNBUFFERED=1
{prefix_line}ExecStart={python} -m piadblock crt --tty /dev/tty1
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
