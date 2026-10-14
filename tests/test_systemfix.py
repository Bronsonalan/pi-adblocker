"""Resolver decisions, marked config blocks, and systemd unit text."""

from __future__ import annotations

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
from piadblock.units import service_unit, update_service_unit, update_timer_unit


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


if __name__ == "__main__":
    unittest.main()
