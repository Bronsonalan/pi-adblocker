"""Deploy-key discovery and git pull/push without touching a real home directory."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

import helpers  # noqa: F401

from piadblock.gitrepo import RESOLVER_OPTIONS, SSH_CONNECT_OPTIONS, GitAccess, Repo, discover_git_access


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
            self.assertIn("exit ", warning)


def _shim(directory: Path, body: str) -> None:
    path = directory / "git"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


_SHIM = """#!/bin/sh
real=/usr/bin/git
mode=${PIADBLOCK_GIT_FAIL:-}
count=${PIADBLOCK_GIT_COUNT:-}
repo=
prev=
sub=
for arg in "$@"; do
  if [ "$prev" = "-C" ]; then
    repo=$arg
    prev=
    continue
  fi
  if [ "$prev" = "-c" ]; then
    prev=
    continue
  fi
  case "$arg" in
    -C|-c) prev=$arg; continue ;;
    -*) continue ;;
  esac
  sub=$arg
  break
done
case "$sub" in
  fetch|pull|push|add|commit) ;;
  *) exec "$real" "$@" ;;
esac
if [ "$mode" = "access" ]; then
  n=0
  if [ -n "$count" ] && [ -f "$count" ]; then
    n=$(cat "$count")
  fi
  n=$((n + 1))
  if [ -n "$count" ]; then
    echo "$n" > "$count"
  fi
  echo "fatal: Could not read from remote repository." >&2
  echo "Please make sure you have the correct access rights" >&2
  echo "and the repository exists." >&2
  exit 128
fi
if [ "$mode" = "index" ] && [ -n "$repo" ] && [ -f "$repo/.git/index.lock" ]; then
  echo "fatal: Unable to create '$repo/.git/index.lock': File exists." >&2
  echo "Another git process seems to be running in this repository." >&2
  exit 128
fi
if [ "$mode" = "slow" ] && [ "$sub" = "fetch" ]; then
  sleep 0.4
fi
exec "$real" "$@"
"""


class GitErrorTests(unittest.TestCase):
    def _repo(self, tmp: Path) -> tuple[Path, Path, Repo]:
        bare = tmp / "remote.git"
        pi = tmp / "pi"
        _init_bare(bare)
        pi.mkdir()
        (pi / "blocklist.txt").write_text("alpha.example\n", encoding="utf-8")
        _git(pi, "init", "-b", "main")
        _git(pi, "config", "user.name", "piadblock")
        _git(pi, "config", "user.email", "piadblock@localhost")
        _commit_all(pi, "seed")
        _git(pi, "remote", "add", "origin", str(bare))
        _git(pi, "push", "origin", "HEAD:main")
        repo = Repo(str(pi), GitAccess("piadblock", "", "missing", str(bare)))
        repo.retry_delays = (0, 0)
        return bare, pi, repo

    def _use_shim(self, tmp: Path) -> dict[str, str]:
        bindir = tmp / "bin"
        bindir.mkdir()
        _shim(bindir, _SHIM)
        saved_path = os.environ.get("PATH", "")
        saved_mode = os.environ.get("PIADBLOCK_GIT_FAIL")
        saved_count = os.environ.get("PIADBLOCK_GIT_COUNT")
        os.environ["PATH"] = str(bindir) + os.pathsep + saved_path

        def restore() -> None:
            os.environ["PATH"] = saved_path
            if saved_mode is None:
                os.environ.pop("PIADBLOCK_GIT_FAIL", None)
            else:
                os.environ["PIADBLOCK_GIT_FAIL"] = saved_mode
            if saved_count is None:
                os.environ.pop("PIADBLOCK_GIT_COUNT", None)
            else:
                os.environ["PIADBLOCK_GIT_COUNT"] = saved_count

        self.addCleanup(restore)
        return {"PATH": saved_path}

    def test_pull_logs_exit_code_and_the_full_remote_error(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            _bare, _pi, repo = self._repo(tmp)
            count = tmp / "count"
            self._use_shim(tmp)
            os.environ["PIADBLOCK_GIT_FAIL"] = "access"
            os.environ["PIADBLOCK_GIT_COUNT"] = str(count)
            _changed, warning = repo.pull()
            self.assertIn("exit 128", warning)
            self.assertIn("Could not read from remote repository", warning)
            self.assertIn("Please make sure you have the correct access rights", warning)
            self.assertIn("and the repository exists.", warning)
            self.assertIn("Blocking is unchanged.", warning)
            # One try plus each backoff delay.
            self.assertEqual(int(count.read_text(encoding="utf-8")), 1 + len(repo.retry_delays))

    def test_stale_index_lock_is_removed_and_the_pull_retries(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            bare, pi, repo = self._repo(tmp)
            origin = tmp / "origin"
            _git(tmp, "clone", str(bare), str(origin))
            _git(origin, "config", "user.name", "piadblock")
            _git(origin, "config", "user.email", "piadblock@localhost")
            (origin / "blocklist.txt").write_text("alpha.example\nbeta.example\n", encoding="utf-8")
            _commit_all(origin, "block more")
            _git(origin, "push", "origin", "HEAD:main")
            lock = pi / ".git" / "index.lock"
            lock.write_text("stale\n", encoding="utf-8")
            os.utime(lock, (time.time() - 30, time.time() - 30))
            self._use_shim(tmp)
            os.environ["PIADBLOCK_GIT_FAIL"] = "index"
            changed, warning = repo.pull()
            self.assertEqual(warning, "", warning)
            self.assertTrue(changed)
            self.assertFalse(lock.exists())
            self.assertIn("beta.example", (pi / "blocklist.txt").read_text(encoding="utf-8"))

    def test_pull_and_publish_share_one_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            bare, pi, repo = self._repo(tmp)
            self._use_shim(tmp)
            os.environ["PIADBLOCK_GIT_FAIL"] = "slow"
            (pi / "heartbeat.json").write_text('{"uptime": 3}\n', encoding="utf-8")
            errors: list[str] = []

            def _pull() -> None:
                _changed, warning = repo.pull()
                if warning:
                    errors.append(warning)

            def _publish() -> None:
                warning = repo.publish(["heartbeat.json"], "heartbeat: locked")
                if warning:
                    errors.append(warning)

            first = threading.Thread(target=_pull)
            second = threading.Thread(target=_publish)
            first.start()
            time.sleep(0.05)
            second.start()
            first.join(timeout=10)
            second.join(timeout=10)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [], errors)
            seen = tmp / "seen"
            _git(tmp, "clone", str(bare), str(seen))
            self.assertIn('"uptime": 3', (seen / "heartbeat.json").read_text(encoding="utf-8"))


class ResolverLimitTests(unittest.TestCase):
    def test_ssh_config_keeps_the_host_block_and_bounds_the_connect(self):
        access = GitAccess(
            "techexplain",
            "/home/techexplain/.ssh/pi_adblocker_deploy",
            "ssh-config",
            "git@github.com:Bronsonalan/pi-adblocker.git",
        )
        command = Repo(".", access)._ssh_env()["GIT_SSH_COMMAND"]
        self.assertEqual(command, f"ssh {SSH_CONNECT_OPTIONS}")
        self.assertNotIn("-i ", command)
        self.assertNotIn("IdentitiesOnly", command)
        self.assertIn("ConnectTimeout=5", command)

    def test_generated_key_sets_the_same_connect_limit(self):
        access = GitAccess(
            "techexplain",
            "/tmp/pi_adblocker_deploy",
            "generated",
            "git@github.com:Bronsonalan/pi-adblocker.git",
        )
        command = Repo(".", access)._ssh_env()["GIT_SSH_COMMAND"]
        self.assertIn("-i /tmp/pi_adblocker_deploy", command)
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertIn("ConnectTimeout=5", command)
        self.assertIn("ConnectionAttempts=1", command)

    def test_a_local_remote_does_not_override_ssh(self):
        access = GitAccess("piadblock", "", "missing", "/tmp/remote.git")
        self.assertEqual(Repo(".", access)._ssh_env(), {})

    def test_git_process_uses_a_one_second_resolver_timeout(self):
        access = GitAccess("piadblock", "", "missing", "/tmp/remote.git")
        repo = Repo(".", access)
        captured: dict = {}

        def fake_run(command, **kwargs):
            captured["env"] = kwargs["env"]
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with unittest.mock.patch("piadblock.gitrepo.subprocess.run", fake_run):
            repo.git("status", check=False)
        self.assertEqual(captured["env"]["RES_OPTIONS"], RESOLVER_OPTIONS)
        self.assertEqual(RESOLVER_OPTIONS, "timeout:1 attempts:1")


if __name__ == "__main__":
    unittest.main()
