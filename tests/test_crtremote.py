"""Remote CRT client: composite edits, waiting screen, idempotent install."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

from piadblock.crt import fetch_crt, render_frame, serve_display, visible_text
from piadblock.crtremote import (
    COMPOSITE_VIDEO,
    DEFAULT_BASE_URL,
    MARK_BEGIN,
    MARK_END,
    SERVICE_NAME,
    apply_composite,
    board_kind,
    feed_url,
    install_remote,
    main,
    read_model_text,
    remote_service_unit,
    resolve_base_url,
    waiting_label_for,
)

ROOT = helpers.ROOT

KMS_CONFIG = """dtparam=audio=on

dtoverlay=vc4-kms-v3d
max_framebuffers=2
"""

KMS_CMDLINE = "console=serial0,115200 console=tty1 root=PARTUUID=abc-02 rootwait\n"

LEGACY_CONFIG = """dtparam=audio=on

#dtoverlay=vc4-kms-v3d

[all]
"""

LEGACY_CMDLINE = "console=serial0,115200 console=tty1 rootwait\n"


def _sink_parts():
    parts: list[str] = []

    class _Sink:
        def write(self, data: str) -> int:
            parts.append(data)
            return len(data)

        def flush(self) -> None:
            return None

    return _Sink(), parts


class UrlTests(unittest.TestCase):
    def test_flag_beats_env_and_file_and_the_default_is_the_main_pi(self):
        self.assertEqual(
            resolve_base_url("http://10.0.0.8:8080/", "http://10.0.0.9:8080", "http://10.1.1.1:8080"),
            "http://10.0.0.8:8080",
        )
        self.assertEqual(
            resolve_base_url("", "http://10.0.0.9:8080/api/crt", "http://10.1.1.1:8080"),
            "http://10.0.0.9:8080",
        )
        self.assertEqual(resolve_base_url("", "", "http://10.1.1.1:8080/"), "http://10.1.1.1:8080")
        self.assertEqual(resolve_base_url("", None, None), DEFAULT_BASE_URL)
        self.assertEqual(feed_url(DEFAULT_BASE_URL), "http://172.20.1.217:8080/api/crt")
        self.assertEqual(feed_url("http://172.20.1.217:8080/api/crt"), "http://172.20.1.217:8080/api/crt")
        self.assertEqual(waiting_label_for(DEFAULT_BASE_URL), "waiting for 172.20.1.217")

    def test_client_asks_the_main_pi_for_the_crt_feed(self):
        with mock.patch("piadblock.crtremote.run_display", return_value=0) as run:
            code = main(["--tty", "/dev/null", "--url", "http://172.20.1.217:8080"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[1], "http://172.20.1.217:8080/api/crt")
        self.assertEqual(run.call_args.kwargs["waiting_label"], "waiting for 172.20.1.217")
        self.assertEqual(run.call_args.kwargs["period"], 1.0)
        self.assertEqual(run.call_args.kwargs["wait_period"], 2.0)


class WaitingTests(unittest.TestCase):
    def test_unreachable_server_shows_the_host_and_retries(self):
        stop = threading.Event()
        sink, parts = _sink_parts()
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("connection refused")
            if calls["n"] == 2:
                return None
            stop.set()
            return {
                "queries": 3,
                "blocked": 1,
                "lookups": [
                    {"status": "OK", "client": "10.0.0.2", "name": "example.com", "qtype": "A"}
                ],
            }

        serve_display(
            sink,
            fetch,
            stop,
            width=40,
            height=15,
            period=0.01,
            wait_period=0.01,
            waiting_label="waiting for 172.20.1.217",
        )
        shown = visible_text("".join(parts))
        self.assertIn("waiting for 172.20.1.217", shown)
        self.assertGreaterEqual(calls["n"], 3)
        self.assertIn("example.com", shown)
        self.assertIn("\x1b[?25h", "".join(parts))

    def test_closed_port_is_a_miss_and_the_local_waiting_label_stays(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.assertIsNone(fetch_crt(f"http://127.0.0.1:{port}/api/crt", timeout=0.3))
        frame = render_frame([], 0, 0, width=40, height=15, clock="2:31:05 PM", waiting=True)
        self.assertIn("WAITING FOR PIADBLOCK", visible_text(frame))


class BoardTests(unittest.TestCase):
    def test_zero_and_zero_2_w_come_from_the_device_tree_model(self):
        self.assertEqual(board_kind("Raspberry Pi Zero 2 W Rev 1.0"), "Zero 2 W")
        self.assertEqual(board_kind("Raspberry Pi Zero W Rev 1.1"), "Zero W")
        self.assertEqual(board_kind("Raspberry Pi 4 Model B Rev 1.1"), "unknown")
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "model"
            path.write_bytes(b"Raspberry Pi Zero W Rev 1.1\x00")
            self.assertEqual(read_model_text(path), "Raspberry Pi Zero W Rev 1.1")


class CompositeTests(unittest.TestCase):
    def _layout(self, root: Path, config: str, cmdline: str, firmware: bool) -> Path:
        folder = root / "boot" / ("firmware" if firmware else "")
        folder.mkdir(parents=True)
        config_path = folder / "config.txt"
        config_path.write_text(config, encoding="utf-8")
        (folder / "cmdline.txt").write_text(cmdline, encoding="utf-8")
        return config_path

    def test_kms_bookworm_layout_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_path = self._layout(root, KMS_CONFIG, KMS_CMDLINE, firmware=True)
            first = apply_composite(config_path)
            config = config_path.read_text(encoding="utf-8")
            cmdline = (config_path.parent / "cmdline.txt").read_text(encoding="utf-8")
            self.assertIn("dtoverlay=vc4-kms-v3d,composite", config)
            self.assertNotIn("\nenable_tvout=1\n", "\n" + config)
            self.assertIn(MARK_BEGIN, config)
            self.assertIn(MARK_END, config)
            self.assertIn(COMPOSITE_VIDEO, cmdline.split())
            self.assertTrue(any("Not rebooting" in line for line in first))
            backup = config_path.with_name("config.txt.piadblock-bak")
            cmdline_backup = config_path.parent / "cmdline.txt.piadblock-bak"
            self.assertEqual(backup.read_text(encoding="utf-8"), KMS_CONFIG)
            self.assertEqual(cmdline_backup.read_text(encoding="utf-8"), KMS_CMDLINE)
            backup.write_text("SENTINEL", encoding="utf-8")
            second = apply_composite(config_path)
            self.assertEqual(config_path.read_text(encoding="utf-8"), config)
            self.assertEqual((config_path.parent / "cmdline.txt").read_text(encoding="utf-8"), cmdline)
            self.assertEqual(backup.read_text(encoding="utf-8"), "SENTINEL")
            self.assertTrue(any("already set for KMS" in line for line in second))
            self.assertTrue(any("already has" in line for line in second))
            self.assertFalse(any("Not rebooting" in line for line in second))

    def test_legacy_layout_sets_ntsc_and_leaves_cmdline(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config_path = self._layout(root, LEGACY_CONFIG, LEGACY_CMDLINE, firmware=False)
            apply_composite(config_path)
            config = config_path.read_text(encoding="utf-8")
            cmdline = (config_path.parent / "cmdline.txt").read_text(encoding="utf-8")
            self.assertIn("enable_tvout=1", config)
            self.assertIn("sdtv_mode=0", config)
            self.assertIn(MARK_BEGIN, config)
            self.assertNotIn("dtoverlay=vc4-kms-v3d,composite", config)
            self.assertEqual(cmdline, LEGACY_CMDLINE)
            self.assertFalse((config_path.parent / "cmdline.txt.piadblock-bak").exists())
            again = config
            apply_composite(config_path)
            self.assertEqual(config_path.read_text(encoding="utf-8"), again)

    def test_kms_overlay_keeps_existing_parameters(self):
        text_path_parent = None
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = KMS_CONFIG.replace("dtoverlay=vc4-kms-v3d", "dtoverlay=vc4-kms-v3d,noaudio")
            config_path = self._layout(root, config, KMS_CMDLINE, firmware=True)
            text_path_parent = config_path
            apply_composite(config_path)
            body = config_path.read_text(encoding="utf-8")
            self.assertIn("dtoverlay=vc4-kms-v3d,noaudio,composite", body)
            apply_composite(config_path)
            self.assertEqual(body, config_path.read_text(encoding="utf-8"))
        self.assertIsNotNone(text_path_parent)


class InstallTests(unittest.TestCase):
    def _calls(self):
        calls: list[list[str]] = []

        def run(args):
            calls.append(list(args))

        return calls, run

    def _install(self, prefix: Path, model: str, firmware: bool, config: str, cmdline: str):
        folder = prefix / "boot" / ("firmware" if firmware else "")
        folder.mkdir(parents=True)
        (folder / "config.txt").write_text(config, encoding="utf-8")
        (folder / "cmdline.txt").write_text(cmdline, encoding="utf-8")
        model_path = prefix / "model"
        model_path.write_bytes(model.encode("utf-8") + b"\x00")
        calls, run = self._calls()
        lines = install_remote(source=ROOT, prefix=str(prefix), model_path=model_path, systemctl=run)
        return lines, calls

    def test_install_is_idempotent_for_both_boards(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            zero = root / "zero"
            zero2 = root / "zero2"
            lines_w, calls_w = self._install(
                zero, "Raspberry Pi Zero W Rev 1.1", True, KMS_CONFIG, KMS_CMDLINE
            )
            lines_2, calls_2 = self._install(
                zero2, "Raspberry Pi Zero 2 W Rev 1.0", True, KMS_CONFIG, KMS_CMDLINE
            )
            self.assertTrue(any(line.startswith("model: Raspberry Pi Zero W Rev 1.1 (Zero W)") for line in lines_w))
            self.assertTrue(any("Zero 2 W" in line for line in lines_2))
            config_w = (zero / "boot/firmware/config.txt").read_text(encoding="utf-8")
            config_2 = (zero2 / "boot/firmware/config.txt").read_text(encoding="utf-8")
            self.assertEqual(config_w, config_2)
            self.assertEqual(
                (zero / "boot/firmware/cmdline.txt").read_text(encoding="utf-8"),
                (zero2 / "boot/firmware/cmdline.txt").read_text(encoding="utf-8"),
            )
            self.assertIn("dtoverlay=vc4-kms-v3d,composite", config_w)
            self.assertIn(COMPOSITE_VIDEO, (zero / "boot/firmware/cmdline.txt").read_text(encoding="utf-8"))
            for calls in (calls_w, calls_2):
                self.assertIn(["daemon-reload"], calls)
                self.assertIn(["enable", "--now", SERVICE_NAME], calls)
                self.assertNotIn("reboot", [arg for call in calls for arg in call])
            unit = (zero / "etc/systemd/system" / SERVICE_NAME).read_text(encoding="utf-8")
            self.assertIn("TTYPath=/dev/tty1", unit)
            self.assertIn("Conflicts=getty@tty1.service", unit)
            self.assertIn("WantedBy=multi-user.target", unit)
            self.assertIn("Nice=10", unit)
            self.assertIn("--tty /dev/tty1", unit)
            config = json.loads((zero / "etc/piadblock-crt-remote/config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["url"], DEFAULT_BASE_URL)
            config["url"] = "http://10.9.9.9:8080"
            (zero / "etc/piadblock-crt-remote/config.json").write_text(
                json.dumps(config) + "\n", encoding="utf-8"
            )
            calls, run = self._calls()
            again = install_remote(
                source=ROOT,
                prefix=str(zero),
                model_path=zero / "model",
                systemctl=run,
            )
            text = "\n".join(again)
            self.assertIn("unchanged", text)
            self.assertIn("already set for KMS", text)
            self.assertIn("config: kept", text)
            kept = json.loads((zero / "etc/piadblock-crt-remote/config.json").read_text(encoding="utf-8"))
            self.assertEqual(kept["url"], "http://10.9.9.9:8080")
            self.assertEqual(
                (zero / "boot/firmware/config.txt").read_text(encoding="utf-8"),
                config_w,
            )

    def test_legacy_install_uses_boot_config_txt(self):
        with tempfile.TemporaryDirectory() as raw:
            prefix = Path(raw)
            lines, _calls = self._install(
                prefix, "Raspberry Pi Zero W Rev 1.1", False, LEGACY_CONFIG, LEGACY_CMDLINE
            )
            self.assertTrue(any(line.endswith("/boot/config.txt") for line in lines))
            body = (prefix / "boot/config.txt").read_text(encoding="utf-8")
            self.assertIn("enable_tvout=1", body)
            self.assertIn("sdtv_mode=0", body)
            self.assertEqual((prefix / "boot/cmdline.txt").read_text(encoding="utf-8"), LEGACY_CMDLINE)
            self.assertFalse((prefix / "boot/firmware/config.txt").exists())

    def test_remote_restart_does_not_start_getty(self):
        import test_crt

        _exec_stop_post = test_crt._exec_stop_post
        _run_restore = test_crt._run_restore

        script = _exec_stop_post(remote_service_unit("/usr/bin/python3", "/opt/piadblock-crt-remote", "/etc/piadblock-crt-remote/config.json"))
        restart = _run_restore(script, "4 piadblock-crt-remote.service restart running")
        self.assertNotIn("start getty@tty1.service", restart)
        other = _run_restore(script, "4 piadblock-crt.service restart running")
        self.assertIn("start getty@tty1.service", other)
        stopped = _run_restore(script, "")
        self.assertIn("start getty@tty1.service", stopped)


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_script_installs_twice_and_ends_with_done(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            prefix = tmp / "root"
            prefix.mkdir()
            firmware = prefix / "boot/firmware"
            firmware.mkdir(parents=True)
            (firmware / "config.txt").write_text(KMS_CONFIG, encoding="utf-8")
            (firmware / "cmdline.txt").write_text(KMS_CMDLINE, encoding="utf-8")
            model = tmp / "model"
            model.write_text("Raspberry Pi Zero 2 W Rev 1.0\n", encoding="utf-8")
            stage = tmp / "pi-adblocker-main"
            stage.mkdir()
            (stage / "piadblock").mkdir()
            for rel in (
                "crt_remote_install.py",
                "crt-remote.py",
                "piadblock/__init__.py",
                "piadblock/crt.py",
                "piadblock/crtremote.py",
                "piadblock/units.py",
            ):
                target = stage / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / rel).read_bytes())
            archive = tmp / "src.tar.gz"
            with tarfile.open(archive, "w:gz") as packed:
                packed.add(stage, arcname="pi-adblocker-main")
            bindir = tmp / "bin"
            bindir.mkdir()
            log = tmp / "systemctl.log"
            systemctl = bindir / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {log}\n"
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
            env["PIADBLOCK_PREFIX"] = str(prefix)
            env["PIADBLOCK_URL"] = archive.resolve().as_uri()
            env["PIADBLOCK_CRT_MODEL"] = str(model)
            script = ROOT / "crt-remote-bootstrap.sh"
            syntax = subprocess.run(["/bin/sh", "-n", str(script)], check=False, capture_output=True, text=True)
            self.assertEqual(syntax.returncode, 0, syntax.stderr)
            first = subprocess.run([str(script)], env=env, text=True, capture_output=True, timeout=60, check=False)
            output = first.stdout + first.stderr
            self.assertEqual(first.returncode, 0, output)
            self.assertIn("crt-remote: downloading", first.stdout)
            self.assertIn("crt-remote: running installer", first.stdout)
            self.assertIn("Zero 2 W", first.stdout)
            self.assertIn("dtoverlay composite", first.stdout)
            self.assertIn("Not rebooting", first.stdout)
            self.assertTrue(first.stdout.rstrip().endswith("done"))
            self.assertIn("enable --now piadblock-crt-remote.service", log.read_text(encoding="utf-8"))
            config_before = (firmware / "config.txt").read_text(encoding="utf-8")
            second = subprocess.run([str(script)], env=env, text=True, capture_output=True, timeout=60, check=False)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("unchanged", second.stdout)
            self.assertIn("already set for KMS", second.stdout)
            self.assertTrue(second.stdout.rstrip().endswith("done"))
            self.assertEqual((firmware / "config.txt").read_text(encoding="utf-8"), config_before)
            self.assertIn(COMPOSITE_VIDEO, (firmware / "cmdline.txt").read_text(encoding="utf-8"))
