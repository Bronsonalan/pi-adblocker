"""CRT frame: 640x480 cells, newest first, blocked rows highlighted."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

from piadblock.crt import (
    BLOCKED_STYLE,
    CRT_HEIGHT,
    CRT_WIDTH,
    FAIL_STYLE,
    LookupFeed,
    choose_font,
    compose_lookup,
    format_local_time,
    render_frame,
    serve_display,
    visible_text,
)
from piadblock.units import crt_service_unit


def _lines(frame: str) -> list[str]:
    return frame.splitlines()


def _visible(frame: str) -> list[str]:
    return [visible_text(line) for line in _lines(frame)]


class FrameTests(unittest.TestCase):
    def test_blocked_row_is_highlighted_and_marked(self):
        frame = render_frame(
            [
                {
                    "status": "BLOCKED",
                    "client": "192.168.1.20",
                    "name": "doubleclick.net",
                    "qtype": "A",
                }
            ],
            1284,
            96,
            width=CRT_WIDTH,
            height=CRT_HEIGHT,
            clock="12:00:00",
        )
        visible = _visible(frame)
        self.assertEqual(len(visible), CRT_HEIGHT)
        self.assertTrue(all(len(line) == CRT_WIDTH for line in visible))
        self.assertIn("1284", visible[0] + visible[1])
        self.assertIn("TODAY", visible[1])
        self.assertIn("96", visible[1])
        row = next(line for line in _lines(frame) if "doubleclick.net" in visible_text(line))
        shown = visible_text(row)
        self.assertIn("BLOCKED", shown)
        self.assertIn("192.168.1.20", shown)
        self.assertIn("doubleclick.net", shown)
        self.assertIn(f"\x1b[{BLOCKED_STYLE}m", row)

    def test_ok_row_is_plain_green_and_not_marked_blocked(self):
        frame = render_frame(
            [{"status": "OK", "client": "10.1.1.1", "name": "example.com", "qtype": "A"}],
            1,
            0,
            width=CRT_WIDTH,
            height=CRT_HEIGHT,
            clock="12:00:00",
        )
        row = next(line for line in _lines(frame) if "example.com" in visible_text(line))
        shown = visible_text(row)
        self.assertIn("OK", shown)
        self.assertIn("10.1.1.1", shown)
        self.assertIn("example.com", shown)
        self.assertNotIn("BLOCKED", shown)
        self.assertNotIn(f"\x1b[{BLOCKED_STYLE}m", row)
        self.assertIn("\x1b[32m", row)

    def test_fail_row_is_not_the_blocked_highlight(self):
        frame = render_frame(
            [{"status": "FAIL", "client": "10.0.0.3", "name": "example.com", "qtype": "A"}],
            1,
            0,
            width=CRT_WIDTH,
            height=CRT_HEIGHT,
            clock="12:00:00",
        )
        row = next(line for line in _lines(frame) if "example.com" in visible_text(line))
        self.assertIn("FAIL", visible_text(row))
        self.assertIn(f"\x1b[{FAIL_STYLE}m", row)
        self.assertNotIn(f"\x1b[{BLOCKED_STYLE}m", row)

    def test_newest_lookup_is_the_first_row(self):
        frame = render_frame(
            [
                {"status": "OK", "client": "10.0.0.8", "name": "newer.example", "qtype": "A"},
                {"status": "OK", "client": "10.0.0.9", "name": "older.example", "qtype": "A"},
            ],
            2,
            0,
            width=CRT_WIDTH,
            height=CRT_HEIGHT,
            clock="12:00:00",
        )
        text = visible_text(frame)
        self.assertLess(text.index("newer.example"), text.index("older.example"))

    def test_long_client_and_domain_are_truncated_to_the_crt_width(self):
        client = "2001:db8:85a3:0000:0000:8a2e:0370:7334"
        name = "ads." + ("x" * 80) + ".example.com"
        frame = render_frame(
            [{"status": "BLOCKED", "client": client, "name": name, "qtype": "AAAA"}],
            10,
            10,
            width=CRT_WIDTH,
            height=CRT_HEIGHT,
            clock="12:00:00",
        )
        visible = _visible(frame)
        self.assertTrue(all(len(line) == CRT_WIDTH for line in visible))
        row = next(line for line in visible if line.startswith("BLOCKED") and "TODAY" not in line)
        self.assertIn("...", row)
        self.assertNotIn(client, row)
        self.assertNotIn(name, row)
        self.assertIn("2001", row)
        self.assertEqual(len(compose_lookup("BLOCKED", client, name, "AAAA", CRT_WIDTH)), CRT_WIDTH)

    def test_header_still_shows_totals_when_the_feed_is_empty(self):
        frame = render_frame([], 7, 2, width=CRT_WIDTH, height=CRT_HEIGHT, clock="08:01:02")
        visible = _visible(frame)
        self.assertEqual(len(visible), CRT_HEIGHT)
        self.assertIn("08:01:02", visible[0])
        self.assertIn("PI AD BLOCK", visible[0])
        self.assertIn("TODAY 7 Q", visible[1])
        self.assertIn("2 BLOCKED", visible[1])

    def test_rows_omit_the_raw_epoch_and_the_title_uses_local_time(self):
        previous = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(_restore_tz, previous)
        epoch = 14 * 3600 + 31 * 60 + 5
        with mock.patch("piadblock.crt.time.time", return_value=epoch):
            frame = render_frame(
                [
                    {
                        "ts": 1792099091,
                        "status": "BLOCKED",
                        "client": "192.168.1.20",
                        "name": "doubleclick.net",
                        "qtype": "A",
                    }
                ],
                4,
                1,
                width=CRT_WIDTH,
                height=CRT_HEIGHT,
            )
        visible = _visible(frame)
        self.assertIn("2:31:05 PM", visible[0])
        self.assertEqual(format_local_time(epoch), "2:31:05 PM")
        self.assertNotIn("1792099091", visible_text(frame))
        self.assertNotIn(str(epoch), visible_text(frame))

    def test_title_shows_the_effective_list_size(self):
        frame = render_frame(
            [],
            1,
            0,
            width=CRT_WIDTH,
            height=CRT_HEIGHT,
            clock="2:31:05 PM",
            blocklist_domains=91526,
        )
        title = _visible(frame)[0]
        self.assertEqual(len(title), CRT_WIDTH)
        self.assertIn("PI AD BLOCK", title)
        self.assertIn("91,526", title)
        self.assertIn("2:31:05 PM", title)

    def test_waiting_frame_does_not_invent_a_zero_list_size(self):
        frame = render_frame(
            [],
            0,
            0,
            width=CRT_WIDTH,
            height=CRT_HEIGHT,
            clock="2:31:05 PM",
            waiting=True,
            blocklist_domains=0,
        )
        title = _visible(frame)[0]
        middle = title[len("PI AD BLOCK") : title.index("2:31:05 PM")]
        self.assertEqual(middle.strip(), "")
        self.assertNotIn("91,526", title)

    def test_narrow_title_keeps_the_clock_when_the_count_does_not_fit(self):
        frame = render_frame(
            [],
            0,
            0,
            width=24,
            height=3,
            clock="2:31:05 PM",
            blocklist_domains=91526,
        )
        title = _visible(frame)[0]
        self.assertIn("2:31:05 PM", title)
        self.assertNotIn("91,526", title)


class FeedTests(unittest.TestCase):
    def test_ring_keeps_the_newest_lookups_first(self):
        feed = LookupFeed(limit=3)
        for index in range(10):
            feed.note("OK", "10.0.0.1", f"n{index}.example", "A")
        snap = feed.snapshot()
        self.assertEqual([item["name"] for item in snap], ["n9.example", "n8.example", "n7.example"])


class FontTests(unittest.TestCase):
    def test_32x16_font_wins_on_a_640x480_crt(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "Lat15-Terminus16x8.psf.gz").write_bytes(b"small")
            (root / "Lat15-Terminus32x16.psf.gz").write_bytes(b"large")
            chosen = choose_font([str(root)])
        self.assertTrue(chosen.endswith("Lat15-Terminus32x16.psf.gz"))

    def test_missing_font_is_empty(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(choose_font([raw]), "")


class DisplayLoopTests(unittest.TestCase):
    def test_one_frame_highlights_blocked_and_restores_the_cursor(self):
        stop = threading.Event()
        parts: list[str] = []

        class _Sink:
            def write(self, data: str) -> int:
                parts.append(data)
                return len(data)

            def flush(self) -> None:
                return None

        def fetch():
            stop.set()
            return {
                "queries": 2,
                "blocked": 1,
                "lookups": [
                    {"status": "BLOCKED", "client": "10.0.0.4", "name": "ads.example", "qtype": "A"}
                ],
            }

        serve_display(_Sink(), fetch, stop, width=CRT_WIDTH, height=CRT_HEIGHT, period=0.01)
        drawn = "".join(parts)
        self.assertIn(f"\x1b[{BLOCKED_STYLE}m", drawn)
        self.assertIn("ads.example", visible_text(drawn))
        self.assertIn("BLOCKED", visible_text(drawn))
        self.assertIn("\x1b[?25h", drawn)

    def test_display_puts_the_list_size_on_the_title(self):
        previous = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.addCleanup(_restore_tz, previous)
        stop = threading.Event()
        parts: list[str] = []

        class _Sink:
            def write(self, data: str) -> int:
                parts.append(data)
                return len(data)

            def flush(self) -> None:
                return None

        def fetch():
            stop.set()
            return {"queries": 1, "blocked": 0, "lookups": [], "blocklist_domains": 91526}

        with mock.patch("piadblock.crt.time.time", return_value=14 * 3600 + 31 * 60 + 5):
            serve_display(_Sink(), fetch, stop, width=CRT_WIDTH, height=CRT_HEIGHT, period=0.01)
        shown = visible_text("".join(parts))
        self.assertIn("91,526", shown)
        self.assertIn("2:31:05 PM", shown)


class UnitTextTests(unittest.TestCase):
    def test_crt_unit_takes_tty1_and_returns_it(self):
        text = crt_service_unit("/usr/bin/python3", "/opt/piadblock")
        self.assertIn("ExecStart=/usr/bin/python3 -m piadblock crt --tty /dev/tty1", text)
        self.assertIn("TTYPath=/dev/tty1", text)
        self.assertIn("Conflicts=getty@tty1.service", text)
        self.assertIn("ExecStopPost=-/bin/systemctl start getty@tty1.service", text)
        self.assertNotIn("PIADBLOCK_PREFIX", text)
        prefixed = crt_service_unit("/usr/bin/python3", "/opt/piadblock", prefix="/tmp/pi-root")
        self.assertIn("Environment=PIADBLOCK_PREFIX=/tmp/pi-root", prefixed)


def _restore_tz(previous: str | None) -> None:
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


if __name__ == "__main__":
    unittest.main()
