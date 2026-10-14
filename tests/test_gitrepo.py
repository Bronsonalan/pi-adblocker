"""Deploy-key discovery and git pull/push without touching a real home directory."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from piadblock.gitrepo import GitAccess, Repo, discover_git_access


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "piadblock"
    env["GIT_AUTHOR_EMAIL"] = "piadblock@localhost"
    env["GIT_COMMITTER_NAME"] = "piadblock"
    env["GIT_COMMITTER_EMAIL"] = "piadblock@localhost"
    for name in ("PIADBLOCK_SSH_KEY", "PIADBLOCK_GIT_USER", "PIADBLOCK_REMOTE"):
        env.pop(name, None)
    return env


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", f"safe.directory={cwd}", *args],
        check=check,
        capture_output=True,
        text=True,
        env=_git_env(),
    )


def _init_bare(path: Path) -> None:
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self._saved = {name: os.environ.get(name) for name in ("PIADBLOCK_SSH_KEY", "PIADBLOCK_GIT_USER", "PIADBLOCK_REMOTE")}
        for name in self._saved:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_ssh_config_is_reused_and_no_key_is_generated(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "techexplain"
            ssh = home / ".ssh"
            ssh.mkdir(parents=True)
            key = ssh / "pi_adblocker_deploy"
            key.write_text("existing-private-key\n", encoding="utf-8")
            (ssh / "config").write_text(
                "Host github.com\n  IdentityFile ~/.ssh/pi_adblocker_deploy\n",
                encoding="utf-8",
            )
            before = {path.name for path in ssh.iterdir()}
            access = discover_git_access(Path(raw), generate=False)
            self.assertEqual(access.source, "ssh-config")
            self.assertEqual(access.user, "techexplain")
            self.assertEqual(access.key, str(key))
            self.assertEqual({path.name for path in ssh.iterdir()}, before)

    def test_existing_key_without_ssh_config(self):
        with tempfile.TemporaryDirectory() as raw:
            ssh = Path(raw) / "techexplain" / ".ssh"
            ssh.mkdir(parents=True)
            key = ssh / "pi_adblocker_deploy"
            key.write_text("existing-private-key\n", encoding="utf-8")
            access = discover_git_access(Path(raw), generate=False)
            self.assertEqual(access.source, "existing-key")
            self.assertEqual(access.user, "techexplain")
            self.assertEqual(access.key, str(key))

    def test_key_is_generated_only_when_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw) / "techexplain"
            home.mkdir()
            missing = discover_git_access(Path(raw), generate=False)
            self.assertEqual(missing.source, "missing")
            self.assertFalse((home / ".ssh").exists())

            created = discover_git_access(Path(raw), generate=True)
            self.assertEqual(created.source, "generated")
            self.assertTrue(created.key.endswith("/techexplain/.ssh/pi_adblocker_deploy"))
            self.assertIn("ssh-ed25519", Path(created.key + ".pub").read_text(encoding="utf-8"))

            again = discover_git_access(Path(raw), generate=True)
            self.assertEqual(again.source, "existing-key")
            self.assertEqual(again.key, created.key)
            keys = list((home / ".ssh").glob("pi_adblocker_deploy"))
            self.assertEqual(keys, [Path(created.key)])


class SyncTests(unittest.TestCase):
    def test_pull_keeps_heartbeat_updates_blocklist_and_publish_pushes(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            bare = tmp / "remote.git"
            origin = tmp / "origin"
            pi = tmp / "pi"
            _init_bare(bare)
            origin.mkdir()
            (origin / "blocklist.txt").write_text("alpha.example\n", encoding="utf-8")
            (origin / "README").write_text("hi\n", encoding="utf-8")
            _git(origin, "init", "-b", "main")
            _commit_all(origin, "seed")
            _git(origin, "remote", "add", "origin", str(bare))
            _git(origin, "push", "origin", "HEAD:main")

            _git(tmp, "clone", str(bare), str(pi))
            _git(pi, "config", "user.name", "piadblock")
            _git(pi, "config", "user.email", "piadblock@localhost")
            (pi / "heartbeat.json").write_text('{"uptime": 9}\n', encoding="utf-8")
            _commit_all(pi, "heartbeat")

            (origin / "blocklist.txt").write_text("alpha.example\nbeta.example\n", encoding="utf-8")
            _commit_all(origin, "block more")
            _git(origin, "push", "origin", "HEAD:main")

            access = GitAccess("piadblock", "", "missing", str(bare))
            repo = Repo(str(pi), access)
            changed, warning = repo.pull()
            self.assertEqual(warning, "")
            self.assertTrue(changed)
            self.assertIn("beta.example", (pi / "blocklist.txt").read_text(encoding="utf-8"))
            self.assertIn('"uptime": 9', (pi / "heartbeat.json").read_text(encoding="utf-8"))

            (pi / "heartbeat.json").write_text('{"uptime": 10}\n', encoding="utf-8")
            published = repo.publish(["heartbeat.json"], "heartbeat: later")
            self.assertEqual(published, "")

            seen = tmp / "seen"
            _git(tmp, "clone", str(bare), str(seen))
            self.assertIn("beta.example", (seen / "blocklist.txt").read_text(encoding="utf-8"))
            self.assertIn('"uptime": 10', (seen / "heartbeat.json").read_text(encoding="utf-8"))

    def test_rebase_conflict_keeps_pi_heartbeat_and_upstream_blocklist(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            bare = tmp / "remote.git"
            origin = tmp / "origin"
            pi = tmp / "pi"
            _init_bare(bare)
            origin.mkdir()
            (origin / "blocklist.txt").write_text("alpha.example\n", encoding="utf-8")
            (origin / "heartbeat.json").write_text('{"uptime": 1}\n', encoding="utf-8")
            _git(origin, "init", "-b", "main")
            _commit_all(origin, "seed")
            _git(origin, "remote", "add", "origin", str(bare))
            _git(origin, "push", "origin", "HEAD:main")

            _git(tmp, "clone", str(bare), str(pi))
            _git(pi, "config", "user.name", "piadblock")
            _git(pi, "config", "user.email", "piadblock@localhost")
            (pi / "heartbeat.json").write_text('{"uptime": 9}\n', encoding="utf-8")
            (pi / "blocklist.txt").write_text("alpha.example\nlocal.example\n", encoding="utf-8")
            _commit_all(pi, "pi edits")

            (origin / "heartbeat.json").write_text('{"uptime": 2}\n', encoding="utf-8")
            (origin / "blocklist.txt").write_text("beta.example\n", encoding="utf-8")
            _commit_all(origin, "upstream edits")
            _git(origin, "push", "origin", "HEAD:main")

            changed, warning = Repo(str(pi), GitAccess("piadblock", "", "missing", str(bare))).pull()
            self.assertEqual(warning, "", warning)
            self.assertTrue(changed)
            self.assertIn('"uptime": 9', (pi / "heartbeat.json").read_text(encoding="utf-8"))
            self.assertEqual((pi / "blocklist.txt").read_text(encoding="utf-8"), "beta.example\n")

    def test_rejected_push_is_a_warning(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            bare = tmp / "remote.git"
            origin = tmp / "origin"
            _init_bare(bare)
            origin.mkdir()
            (origin / "blocklist.txt").write_text("alpha.example\n", encoding="utf-8")
            _git(origin, "init", "-b", "main")
            _commit_all(origin, "seed")
            _git(origin, "remote", "add", "origin", str(bare))
            _git(origin, "push", "origin", "HEAD:main")
            hook = bare / "hooks" / "pre-receive"
            hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)

            _git(origin, "config", "user.name", "piadblock")
            _git(origin, "config", "user.email", "piadblock@localhost")
            (origin / "heartbeat.json").write_text('{"uptime": 4}\n', encoding="utf-8")
            warning = Repo(str(origin), GitAccess("piadblock", "", "missing", str(bare))).publish(
                ["heartbeat.json"],
                "heartbeat",
            )
            self.assertIn("git push failed", warning)
            self.assertIn("Blocking is unchanged.", warning)


if __name__ == "__main__":
    unittest.main()
