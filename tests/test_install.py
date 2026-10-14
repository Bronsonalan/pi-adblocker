"""Catch installer import mistakes, and install from a clean checkout.

The Pi failure was ``python3 install.py`` dying at import time, before it
wrote any files. These tests import the entry points the way that command
does, and run ``bootstrap.sh`` into a temporary root with a stand-in
systemctl so nothing is written under ``/opt``, ``/etc``, or ``/var``.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

import helpers  # noqa: F401

ROOT = helpers.ROOT

DENYLIST = [
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "adnxs.com",
    "adsrvr.org",
    "criteo.com",
    "taboola.com",
    "outbrain.com",
    "scorecardresearch.com",
    "amazon-adsystem.com",
]

ENTRYPOINTS = (
    ROOT / "install.py",
    ROOT / "uninstall.py",
    ROOT / "piadblock" / "__main__.py",
)


def _piadblock_imports(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module != "piadblock" and not module.startswith("piadblock."):
            continue
        for alias in node.names:
            found.append((module, alias.name))
    return found


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _stop_service(pidfile: Path) -> None:
    if not pidfile.is_file():
        return
    text = pidfile.read_text(encoding="utf-8").strip()
    if not text:
        return
    pid = int(text)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        except OSError:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
        if sig is signal.SIGTERM:
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.1)


class ImportTests(unittest.TestCase):
    def test_entrypoint_imports_name_real_attributes(self):
        import importlib

        import install
        import uninstall  # noqa: F401  (importing it is the check)

        for path in ENTRYPOINTS:
            imports = _piadblock_imports(path)
            self.assertTrue(imports, f"{path} does not import piadblock")
            for module_name, name in imports:
                module = importlib.import_module(module_name)
                self.assertTrue(
                    hasattr(module, name),
                    f"{path.name} imports {name} from {module_name}, which does not define it",
                )

        install_imports = _piadblock_imports(ROOT / "install.py")
        self.assertIn(("piadblock.systemfix", "classify"), install_imports)
        self.assertNotIn(("piadblock.portcheck", "classify"), install_imports)
        self.assertIs(install.classify, importlib.import_module("piadblock.systemfix").classify)
        self.assertIs(install.describe, importlib.import_module("piadblock.portcheck").describe)
        self.assertIs(install.listeners_on, importlib.import_module("piadblock.portcheck").listeners_on)

    def test_stock_unit_has_no_prefix_environment(self):
        from piadblock.units import service_unit

        unit = service_unit("/usr/bin/python3", "/opt/piadblock", "/bin/kill")
        self.assertNotIn("PIADBLOCK_PREFIX", unit)
        self.assertIn("ExecStart=/usr/bin/python3 -m piadblock serve", unit)


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_installs_from_a_clean_checkout(self):
        port = _free_port()
        with tempfile.TemporaryDirectory(prefix="piadblock-e2e-") as raw:
            tmp = Path(raw)
            prefix = (tmp / "root").resolve()
            prefix.mkdir()
            clean = tmp / "checkout"
            bindir = tmp / "bin"
            bindir.mkdir()
            pidfile = tmp / "service.pid"
            systemctl_log = tmp / "systemctl.log"
            server_log = tmp / "server.log"
            tar_path = tmp / "src.tar.gz"

            shutil.copytree(
                ROOT,
                clean,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"),
            )
            config_path = clean / "config.example.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["listen"] = "127.0.0.1"
            config["port"] = port
            config["ipv6"] = False
            config["status_enabled"] = False
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

            with tarfile.open(tar_path, "w:gz") as archive:
                archive.add(clean, arcname="pi-adblocker-main")

            _write_exe(
                bindir / "python3",
                f"#!/bin/sh\nexec {sys.executable} \"$@\"\n",
            )
            _write_exe(bindir / "systemctl", _systemctl_script())

            guards = _snapshot_real_paths()
            env = os.environ.copy()
            env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
            env["PIADBLOCK_PREFIX"] = str(prefix)
            env["PIADBLOCK_URL"] = tar_path.resolve().as_uri()
            env["PIADBLOCK_SYSTEMCTL_LOG"] = str(systemctl_log)
            env["PIADBLOCK_SERVER_LOG"] = str(server_log)
            env["PIADBLOCK_SERVICE_PID"] = str(pidfile)
            # The parent process must not see the prefix. paths are snapshotted
            # at import, and this test already imported them with the real root.
            env.pop("PIADBLOCK_CONFIG", None)

            try:
                completed = subprocess.run(
                    [str(clean / "bootstrap.sh")],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=60,
                    check=False,
                )
                output = completed.stdout + completed.stderr
                self.assertEqual(completed.returncode, 0, output)
                self.assertIn("piadblock: downloading", completed.stdout)
                self.assertIn("piadblock: running installer", completed.stdout)
                self.assertIn("blocklists: none configured, download skipped", completed.stdout)
                self.assertIn(f"port {port}: free", completed.stdout)
                self.assertIn("self-check: doubleclick.net A -> 0.0.0.0", completed.stdout)
                self.assertIn("self-check: doubleclick.net AAAA -> ::", completed.stdout)
                self.assertIn("done", completed.stdout)
                self.assertNotIn("ImportError", output)
                self.assertNotIn("piadblock-update.timer", completed.stdout)

                installed = json.loads((prefix / "etc/piadblock/config.json").read_text(encoding="utf-8"))
                denylist = Path(installed["denylist"])
                self.assertTrue(str(denylist).startswith(str(prefix) + os.sep), denylist)
                self.assertEqual(
                    [line for line in denylist.read_text(encoding="utf-8").splitlines() if line.strip()],
                    DENYLIST,
                )
                self.assertEqual(installed["port"], port)
                self.assertEqual(installed["blocklists"], [])
                self.assertFalse(installed["auto_update"])

                unit = (prefix / "etc/systemd/system/piadblock.service").read_text(encoding="utf-8")
                self.assertIn("ExecStart=", unit)
                self.assertIn(f"Environment=PIADBLOCK_PREFIX={prefix}", unit)
                self.assertIn(f"WorkingDirectory={prefix}/opt/piadblock", unit)
                self.assertFalse((prefix / "etc/systemd/system/piadblock-update.timer").exists())
                self.assertFalse((prefix / "etc/systemd/system/piadblock-update.service").exists())

                calls = systemctl_log.read_text(encoding="utf-8")
                self.assertIn("daemon-reload", calls)
                self.assertIn("enable piadblock.service", calls)
                self.assertIn("restart piadblock.service", calls)
                self.assertNotIn("piadblock-update", calls)

                logged = server_log.read_text(encoding="utf-8", errors="replace")
                self.assertIn("BLOCKED 127.0.0.1 doubleclick.net A", logged)
                self.assertIn("BLOCKED 127.0.0.1 doubleclick.net AAAA", logged)
                _assert_untouched(self, guards)
            finally:
                _stop_service(pidfile)


def _systemctl_script() -> str:
    return textwrap.dedent(
        f"""\
        #!{sys.executable}
        import os
        import shlex
        import subprocess
        import sys
        import time
        from pathlib import Path

        args = sys.argv[1:]
        log = Path(os.environ["PIADBLOCK_SYSTEMCTL_LOG"])
        with log.open("a", encoding="utf-8") as handle:
            handle.write(" ".join(args) + "\\n")

        if not args:
            sys.exit(0)
        if args[0] == "is-active":
            sys.exit(3)
        if args[0] != "restart" or len(args) < 2:
            sys.exit(0)

        prefix = Path(os.environ["PIADBLOCK_PREFIX"]).expanduser().resolve()
        unit_path = prefix / "etc/systemd/system" / args[1]
        if not unit_path.is_file():
            sys.stderr.write(f"unit not found: {{unit_path}}\\n")
            sys.exit(1)

        cwd = None
        extra = {{}}
        execstart = None
        for line in unit_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("WorkingDirectory="):
                cwd = line.split("=", 1)[1]
            elif line.startswith("Environment="):
                item = line.split("=", 1)[1]
                key, value = item.split("=", 1)
                extra[key] = value
            elif line.startswith("ExecStart="):
                execstart = line.split("=", 1)[1]
        if not execstart:
            sys.stderr.write("unit has no ExecStart\\n")
            sys.exit(1)

        env = os.environ.copy()
        env.update(extra)
        server_log = Path(os.environ["PIADBLOCK_SERVER_LOG"])
        with server_log.open("ab") as handle:
            proc = subprocess.Popen(
                shlex.split(execstart),
                cwd=cwd,
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        Path(os.environ["PIADBLOCK_SERVICE_PID"]).write_text(str(proc.pid), encoding="utf-8")
        time.sleep(0.4)
        if proc.poll() is not None:
            sys.stderr.write(server_log.read_text(encoding="utf-8", errors="replace")[-4000:])
            sys.exit(proc.returncode or 1)
        sys.exit(0)
        """
    )


def _snapshot_real_paths() -> dict:
    paths = [
        Path("/etc/resolv.conf"),
        Path("/etc/piadblock"),
        Path("/opt/piadblock"),
        Path("/var/lib/piadblock"),
        Path("/var/log/piadblock"),
        Path("/etc/systemd/system/piadblock.service"),
        Path("/etc/systemd/resolved.conf.d/piadblock.conf"),
    ]
    snap = {}
    for path in paths:
        if path.is_symlink():
            snap[path] = ("symlink", os.readlink(path))
        elif path.exists():
            snap[path] = ("file", path.stat().st_mtime_ns, path.stat().st_size)
        else:
            snap[path] = ("absent",)
    return snap


def _assert_untouched(test: unittest.TestCase, before: dict) -> None:
    after = _snapshot_real_paths()
    test.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
