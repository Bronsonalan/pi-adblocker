""" /proc/net parsers and a live socket lookup."""

from __future__ import annotations

import os
import socket
import unittest

import helpers  # noqa: F401

from piadblock.portcheck import listeners_on, parse_ipv4, parse_ipv6, parse_proc_net

UDP_TEXT = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops
  16: 3500007F:0035 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 4242 2 0000000000000000 0
   1: 00000000:0035 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 9999 2 0000000000000000 0
   2: 0100007F:0034 00000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 1111 2 0000000000000000 0
"""

TCP_TEXT = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:0035 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 5555 1 0000000000000000 100 0 0 10 0
   1: 00000000:0035 0100007F:1234 01 00000000:00000000 00:00000000 00000000     0        0 6666 1 0000000000000000 100 0 0 10 0
"""

UDP6_TEXT = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode ref pointer drops
   0: 00000000000000000000000001000000:0035 00000000000000000000000000000000:0000 07 00000000:00000000 00:00000000 00000000     0        0 7777 2 0000000000000000 0
"""


class PortcheckTests(unittest.TestCase):
    def test_ipv4_and_ipv6_text(self):
        self.assertEqual(parse_ipv4("3500007F"), "127.0.0.53")
        self.assertEqual(parse_ipv4("0100007F"), "127.0.0.1")
        self.assertEqual(parse_ipv6("00000000000000000000000001000000"), "::1")

    def test_proc_net_filters_port_and_tcp_state(self):
        udp = parse_proc_net(UDP_TEXT, 53, proto="udp")
        self.assertEqual(udp, [("127.0.0.53", "4242"), ("0.0.0.0", "9999")])
        tcp = parse_proc_net(TCP_TEXT, 53, proto="tcp")
        self.assertEqual(tcp, [("0.0.0.0", "5555")])
        udp6 = parse_proc_net(UDP6_TEXT, 53, proto="udp")
        self.assertEqual(udp6, [("::1", "7777")])

    def test_live_socket_is_attributed_to_this_process(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        try:
            found = listeners_on(port)
        finally:
            sock.close()
        self.assertTrue(
            any(item.pid == os.getpid() and item.proto == "udp" for item in found),
            found,
        )


if __name__ == "__main__":
    unittest.main()
