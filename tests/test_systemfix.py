"""Resolver decisions, marked config blocks, and systemd unit text."""

from __future__ import annotations

import subprocess
import unittest

import helpers  # noqa: F401

from piadblock.portcheck import Listener
from piadblock.systemfix import (
    classify,
    remove_marked_block,
    render_dhcpcd_block,
    render_resolv_conf,
    resolver_should_be_replaced,
    upsert_marked_block,
)
from piadblock.units import calendar_timer_unit, service_unit, update_service_unit, update_timer_unit


def _listener(process: str, cmdline: str = "") -> Listener:
    return Listener(
        proto="udp",
        ip="0.0.0.0",
        port=53,
        inode="1",
        pid=10,
        process=process,
        cmdline=cmdline,
    )


class ResolverTests(unittest.TestCase):
    def test_replaces_loopback_and_router_dns(self):
        self.assertTrue(resolver_should_be_replaced(""))
        self.assertTrue(resolver_should_be_replaced("# empty\n"))
        self.assertTrue(resolver_should_be_replaced("nameserver 127.0.0.53\n"))
        self.assertTrue(resolver_should_be_replaced("nameserver 127.0.0.1\n"))
        self.assertTrue(resolver_should_be_replaced("nameserver 192.168.1.1\n"))
        self.assertTrue(resolver_should_be_replaced("nameserver ::1\n"))

    def test_keeps_public_dns(self):
        self.assertFalse(resolver_should_be_replaced("nameserver 1.1.1.1\nnameserver 1.0.0.1\n"))
        self.assertFalse(resolver_should_be_replaced("nameserver 8.8.8.8\n"))
        text = render_resolv_conf(["1.1.1.1"])
        self.assertIn("nameserver 1.1.1.1", text)
        self.assertFalse(resolver_should_be_replaced(text))

    def test_marked_dhcpcd_block_is_idempotent(self):
        original = "hostname\nclientid\n"
        once = upsert_marked_block(original, render_dhcpcd_block(["1.1.1.1"]))
        twice = upsert_marked_block(once, render_dhcpcd_block(["1.1.1.1", "1.0.0.1"]))
        self.assertEqual(twice.count("# BEGIN piadblock"), 1)
        self.assertIn("static domain_name_servers=1.1.1.1 1.0.0.1", twice)
        self.assertNotIn("domain_name_servers=1.1.1.1\n", twice.replace("1.1.1.1 1.0.0.1", "X"))
        restored = remove_marked_block(twice)
        self.assertEqual(restored.strip(), original.strip())
        self.assertNotIn("piadblock", restored)


class ClassifyTests(unittest.TestCase):
    def test_groups(self):
        groups = classify(
            [
                _listener("systemd-resolve", "/lib/systemd/systemd-resolved"),
                _listener("dnsmasq", "/usr/sbin/dnsmasq"),
                _listener("python3", "/usr/bin/python3 -m piadblock serve"),
                _listener("named", "/usr/sbin/named"),
            ]
        )
        self.assertEqual(len(groups["resolved"]), 1)
        self.assertEqual(len(groups["dnsmasq"]), 1)
        self.assertEqual(len(groups["ours"]), 1)
        self.assertEqual(len(groups["other"]), 1)
        self.assertEqual(groups["other"][0].process, "named")


class UnitTests(unittest.TestCase):
    def test_units_point_at_the_install_root(self):
        service = service_unit("/usr/bin/python3", "/opt/piadblock", "/bin/kill")
        self.assertIn("ExecStart=/usr/bin/python3 -m piadblock serve", service)
        self.assertIn("WorkingDirectory=/opt/piadblock", service)
        self.assertIn("ExecReload=/bin/kill -HUP $MAINPID", service)
        update = update_service_unit("/usr/bin/python3", "/opt/piadblock")
        self.assertIn("piadblock update --if-due", update)
        timer = update_timer_unit()
        self.assertIn("OnCalendar=hourly", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_git_timers_use_a_real_calendar_and_install_section(self):
        expected = {"minutely": "pull", "hourly": "heartbeat", "daily": "stats"}
        for calendar, label in expected.items():
            with self.subTest(timer=label):
                text = calendar_timer_unit(f"piadblock {label}", calendar)
                self.assertEqual(timer_arming_error(text), "")
                self.assertIn(f"OnCalendar={calendar}", text)
        self.assertEqual(timer_arming_error(update_timer_unit()), "")

    def test_on_unit_active_sec_alone_never_triggers(self):
        alone = "[Timer]\nOnUnitActiveSec=1h\n\n[Install]\nWantedBy=timers.target\n"
        self.assertIn("OnUnitActiveSec", timer_arming_error(alone))
        armed = "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=1h\n\n[Install]\nWantedBy=timers.target\n"
        self.assertEqual(timer_arming_error(armed), "")
        bad_calendar = "[Timer]\nOnCalendar=not-a-calendar\n\n[Install]\nWantedBy=timers.target\n"
        self.assertIn("not-a-calendar", timer_arming_error(bad_calendar))


def timer_arming_error(text: str) -> str:
    """Return why this timer would stay at Trigger: n/a, or \"\" when it can elapse."""
    if "[Install]" not in text or "WantedBy=timers.target" not in text:
        return "timer is missing [Install] WantedBy=timers.target"
    directives: dict[str, str] = {}
    in_timer = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_timer = line == "[Timer]"
            continue
        if in_timer and "=" in line:
            key, value = line.split("=", 1)
            directives[key.strip()] = value.strip()
    calendar = directives.get("OnCalendar", "")
    unit_active = directives.get("OnUnitActiveSec", "")
    boot = directives.get("OnBootSec", "")
    active = directives.get("OnActiveSec", "")
    startup = directives.get("OnStartupSec", "")
    # OnUnitActiveSec is measured from the last run of the service. With no
    # other trigger, that run never happens, so the timer never elapses.
    if unit_active and not (calendar or boot or active or startup):
        return "OnUnitActiveSec alone never elapses until the service has already run"
    if not any((calendar, unit_active, boot, active, startup, directives.get("OnUnitInactiveSec", ""))):
        return "timer has no OnCalendar, OnUnitActiveSec, OnBootSec, or OnActiveSec"
    if calendar:
        completed = subprocess.run(
            ["systemd-analyze", "calendar", calendar],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "invalid calendar").strip()
            return f"OnCalendar={calendar} is not a valid calendar ({detail})"
    return ""


if __name__ == "__main__":
    unittest.main()
