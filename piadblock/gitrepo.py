"""Git checkout sync. Pulls blocklist.txt and pushes only stats and heartbeat.

The DNS server stays root so it can bind port 53 and reload itself. Git
runs as the Unix user who owns the deploy key (on this Pi, techexplain),
because OpenSSH refuses a private key owned by someone else. The timer
process is root and uses ``runuser`` for every git command. When
``~/.ssh/config`` already has a ``Host github.com`` entry, that config is
left in charge. A key is generated only when neither the config nor
``~/.ssh/pi_adblocker_deploy`` exists.

A failed fetch or push is a warning. Callers return success so systemd
does not treat it as a crash, and the DNS server keeps blocking.
"""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from piadblock.paths import CONFIG_DIR, DEFAULT_REMOTE, PREFIX, STATE_DIR

# OpenSSH's ConnectTimeout defaults to unlimited and does not cover DNS.
# glibc's resolver defaults to 5 seconds and 2 attempts, about 10 seconds
# when github.com gets no answer. Both limits are applied to the git process.
RESOLVER_OPTIONS = "timeout:1 attempts:1"
SSH_CONNECT_OPTIONS = "-o ConnectTimeout=5 -o ConnectionAttempts=1"

PREFERRED_USER = "techexplain"
KEY_NAME = "pi_adblocker_deploy"
ACCESS_PATH = str(Path(STATE_DIR) / "git-access.json")
OWNED_PREFIXES = ("heartbeat.json", "stats/")
GITHUB_KNOWN_HOST = (
    "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
)


@dataclass
class GitAccess:
    user: str
    key: str
    source: str
    remote: str

    def to_json(self) -> str:
        return json.dumps(
            {"user": self.user, "key": self.key, "source": self.source, "remote": self.remote},
            indent=2,
        ) + "\n"


def remote_url() -> str:
    return os.environ.get("PIADBLOCK_REMOTE", "").strip() or DEFAULT_REMOTE


def identity_file_from_ssh_config(text: str, home: Path) -> str | None:
    """Return the IdentityFile from a ``Host github.com`` block, if any."""
    active = False
    found = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        bits = line.split(None, 1)
        if len(bits) != 2:
            continue
        key, value = bits[0].lower(), bits[1].strip().strip('"').strip("'")
        if key == "host":
            active = "github.com" in value.split()
            continue
        if active and key == "identityfile" and found is None:
            found = value
    if not found:
        return None
    if found.startswith("~/"):
        return str(home / found[2:])
    return found


def _current_user() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return os.environ.get("USER") or "root"


def _home(user: str, home_root: Path) -> Path:
    candidate = home_root / user
    if candidate.is_dir():
        return candidate
    if home_root == Path("/home"):
        try:
            return Path(pwd.getpwnam(user).pw_dir)
        except KeyError:
            return candidate
    return candidate


def _public_key(private_key: str) -> str:
    pub = Path(str(private_key) + ".pub")
    if pub.is_file():
        return pub.read_text(encoding="utf-8").strip()
    return ""


def _user_exists(user: str) -> bool:
    try:
        pwd.getpwnam(user)
    except KeyError:
        return False
    return True


def discover_git_access(home_root: Path | None = None, *, generate: bool = False) -> GitAccess:
    """Find techexplain's key or SSH config. Generate a key only when asked and none exists."""
    homes = Path(home_root) if home_root is not None else Path("/home")
    env_user = os.environ.get("PIADBLOCK_GIT_USER", "").strip()
    env_key = os.environ.get("PIADBLOCK_SSH_KEY", "").strip()
    remote = remote_url()
    if env_key and Path(env_key).is_file():
        return GitAccess(env_user or _current_user(), env_key, "environment", remote)

    users: list[str] = []
    if env_user:
        users.append(env_user)
    users.append(PREFERRED_USER)
    if homes.is_dir():
        for entry in sorted(homes.iterdir()):
            if entry.is_dir() and entry.name not in users:
                users.append(entry.name)

    seen: set[str] = set()
    for user in users:
        if user in seen:
            continue
        seen.add(user)
        home = _home(user, homes)
        config = home / ".ssh" / "config"
        if config.is_file():
            identity = identity_file_from_ssh_config(config.read_text(encoding="utf-8", errors="replace"), home)
            if identity and Path(identity).is_file():
                return GitAccess(user, identity, "ssh-config", remote)
        key = home / ".ssh" / KEY_NAME
        if key.is_file():
            return GitAccess(user, str(key), "existing-key", remote)

    user = env_user or (PREFERRED_USER if _home(PREFERRED_USER, homes).is_dir() else _current_user())
    if not generate:
        return GitAccess(user, "", "missing", remote)
    key_path = _generate_key(user, homes)
    return GitAccess(user, str(key_path), "generated", remote)


def _generate_key(user: str, homes: Path) -> Path:
    home = _home(user, homes)
    # A prefixed install (the test suite) must not write into a real home directory.
    if not PREFIX and (home.is_dir() or (homes == Path("/home") and _user_exists(user))):
        target = home / ".ssh" / KEY_NAME
    else:
        target = Path(CONFIG_DIR) / "github_deploy_key"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.is_file():
        ssh_keygen = shutil.which("ssh-keygen")
        if ssh_keygen is None:
            raise OSError("ssh-keygen is not installed, so a deploy key could not be created")
        subprocess.run(
            [ssh_keygen, "-t", "ed25519", "-f", str(target), "-N", "", "-C", "piadblock"],
            check=True,
            capture_output=True,
            text=True,
        )
        os.chmod(target, 0o600)
        if os.geteuid() == 0 and _user_exists(user) and user != "root":
            shutil.chown(target, user)
            if Path(str(target) + ".pub").exists():
                shutil.chown(str(target) + ".pub", user)
            shutil.chown(target.parent, user)
    known = Path(CONFIG_DIR) / "github_known_hosts"
    try:
        if not known.is_file():
            known.parent.mkdir(parents=True, exist_ok=True)
            known.write_text(GITHUB_KNOWN_HOST, encoding="utf-8")
            os.chmod(known, 0o644)
    except OSError:
        # A non-root test can generate a key without writing under /etc.
        # The installer runs as root, where this file is created.
        pass
    return target


def load_access() -> GitAccess:
    path = Path(ACCESS_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict) and data.get("user"):
        remote = os.environ.get("PIADBLOCK_REMOTE", "").strip() or str(data.get("remote") or remote_url())
        return GitAccess(
            str(data.get("user") or ""),
            str(data.get("key") or ""),
            str(data.get("source") or "missing"),
            remote,
        )
    return discover_git_access(generate=False)


def save_access(access: GitAccess) -> None:
    from piadblock.files import atomic_write

    atomic_write(ACCESS_PATH, access.to_json())


def _failure_text(result: subprocess.CompletedProcess) -> str:
    """Exit code plus the full stderr and stdout. The last line alone hides the cause."""
    err = (result.stderr or "").strip()
    out = (result.stdout or "").strip()
    if err and out:
        detail = err + "\n" + out
    else:
        detail = err or out or "git failed"
    return f"exit {result.returncode}: {detail}"


def _combined(result: subprocess.CompletedProcess) -> str:
    return ((result.stderr or "") + "\n" + (result.stdout or "")).lower()


def _retryable(result: subprocess.CompletedProcess) -> bool:
    text = _combined(result)
    needles = (
        "index.lock",
        "file exists",
        "could not read from remote repository",
        "the repository exists",
        "connection reset",
        "connection timed out",
        "timed out",
        "network is unreachable",
        "could not resolve host",
        "temporary failure",
        "kex_exchange_identification",
        "connection refused",
        "broken pipe",
        "early eof",
        "the remote end hung up",
        "service not known",
    )
    return any(needle in text for needle in needles)


class Repo:
    """One checkout. Git commands impersonate ``access.user`` when we are root."""

    def __init__(self, root: str, access: GitAccess):
        self.root = str(Path(root))
        self.access = access
        # Sleeps between retries of a remote or index.lock failure. Tests set this to zeros.
        self.retry_delays = (0.25, 0.5, 1.0)

    def git(self, *args: str, check: bool = True, retry: bool = False) -> subprocess.CompletedProcess:
        result = self._git_once(*args)
        if retry and result.returncode != 0:
            for delay in self.retry_delays:
                if result.returncode == 0 or not _retryable(result):
                    break
                if "index.lock" in _combined(result):
                    self._clear_stale_index_lock()
                if delay:
                    time.sleep(delay)
                result = self._git_once(*args)
        if check and result.returncode != 0:
            raise OSError(_failure_text(result))
        return result

    def _git_once(self, *args: str) -> subprocess.CompletedProcess:
        command = ["git", "-C", self.root, "-c", f"safe.directory={self.root}", *args]
        env = os.environ.copy()
        env["RES_OPTIONS"] = RESOLVER_OPTIONS
        env.update(self._ssh_env())
        if self._impersonate():
            command = ["runuser", "-u", self.access.user, "--", *command]
        return subprocess.run(command, check=False, capture_output=True, text=True, env=env)

    def _clear_stale_index_lock(self) -> None:
        """Remove a leftover index.lock. Caller already holds piadblock.lock.

        A lock newer than five seconds may belong to a git process this
        program does not own, so it is left in place and the retry waits.
        """
        lock = Path(self.root) / ".git" / "index.lock"
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            return
        if age < 5:
            return
        try:
            lock.unlink()
        except OSError:
            return

    def _impersonate(self) -> bool:
        user = self.access.user
        if os.geteuid() != 0 or not user or user in {"root", _current_user()}:
            return False
        return _user_exists(user) and shutil.which("runuser") is not None

    def _ssh_env(self) -> dict[str, str]:
        # HTTPS and local remotes do not use ssh. A git@ remote always gets
        # a connect limit. When ~/.ssh/config already selects the key, the
        # command must not pass -i or IdentitiesOnly, or that Host block is
        # ignored. OpenSSH still reads ~/.ssh/config unless -F is set.
        if not self.access.remote.startswith("git@"):
            return {}
        if self.access.source == "ssh-config" or not self.access.key:
            return {"GIT_SSH_COMMAND": f"ssh {SSH_CONNECT_OPTIONS}"}
        known = Path(CONFIG_DIR) / "github_known_hosts"
        extra = f" -o UserKnownHostsFile={known}" if known.is_file() else ""
        command = (
            f"ssh -i {self.access.key} -o IdentitiesOnly=yes "
            f"-o StrictHostKeyChecking=yes {SSH_CONNECT_OPTIONS}{extra}"
        )
        return {"GIT_SSH_COMMAND": command}

    @contextmanager
    def lock(self):
        git_dir = Path(self.root) / ".git"
        if not git_dir.is_dir():
            raise OSError(f"{self.root} is not a git checkout")
        handle = (git_dir / "piadblock.lock").open("a", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def has_head(self) -> bool:
        return self.git("rev-parse", "--verify", "HEAD", check=False).returncode == 0

    def ensure(self) -> str:
        """Create the checkout and fast-forward it. A fetch failure is a warning string."""
        try:
            root = Path(self.root)
            root.mkdir(parents=True, exist_ok=True)
            # Init before taking the lock. The lock file lives inside .git, and
            # creating that directory early would make git init think a repo exists.
            if not (root / ".git").is_dir():
                self.git("init", "-b", "main")
            with self.lock():
                self.git("config", "user.name", "piadblock")
                self.git("config", "user.email", "piadblock@localhost")
                self.git("config", "pull.rebase", "true")
                self._set_remote()
                return self._pull_rebase()
        except Exception as exc:
            return f"git setup failed ({exc}). Blocking is unchanged."

    def _set_remote(self) -> None:
        listed = self.git("remote", check=False).stdout.split()
        if "origin" in listed:
            self.git("remote", "set-url", "origin", self.access.remote)
        else:
            self.git("remote", "add", "origin", self.access.remote)

    def pull(self) -> tuple[bool, str]:
        """Fetch and rebase. Return (blocklist file changed, warning)."""
        blocklist = Path(self.root) / "blocklist.txt"
        before = _digest(blocklist)
        try:
            with self.lock():
                self._commit_owned("sync stats and heartbeat")
                if self.has_head():
                    # Drop edits to anything except the commit we just made.
                    # blocklist.txt is edited on GitHub, not on the Pi.
                    self.git("checkout", "--", ".", check=False)
                warning = self._pull_rebase()
                if not warning and self._ahead():
                    warning = self._push()
        except Exception as exc:
            warning = f"git pull failed ({exc}). Blocking is unchanged."
        after = _digest(blocklist)
        return before != after, warning

    def publish(self, paths: list[str], message: str) -> str:
        """Commit ``paths`` and push. A rejected push is a warning, not an exception."""
        try:
            with self.lock():
                return self.publish_holding_lock(paths, message)
        except Exception as exc:
            return f"git publish failed ({exc}). Blocking is unchanged."

    def publish_holding_lock(self, paths: list[str], message: str) -> str:
        """Commit and push. The caller already holds :meth:`lock`."""
        self._commit_paths(paths, message)
        warning = self._pull_rebase()
        if not self._ahead():
            return warning
        pushed = self._push()
        if pushed:
            return pushed
        return warning

    def _commit_owned(self, message: str) -> None:
        paths = []
        if (Path(self.root) / "heartbeat.json").is_file():
            paths.append("heartbeat.json")
        if (Path(self.root) / "stats").is_dir():
            paths.append("stats")
        if paths:
            self._commit_paths(paths, message)

    def _commit_paths(self, paths: list[str], message: str) -> bool:
        existing = [path for path in paths if (Path(self.root) / path).exists()]
        if not existing:
            return False
        self.git("add", "--", *existing, retry=True)
        if self.git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self.git("commit", "-m", message, retry=True)
        return True

    def _ahead(self) -> bool:
        if not self.has_head():
            return False
        result = self.git("rev-parse", "--verify", "origin/main", check=False)
        if result.returncode != 0:
            return True
        count = self.git("rev-list", "--count", "origin/main..HEAD", check=False)
        try:
            return int((count.stdout or "0").strip() or "0") > 0
        except ValueError:
            return False

    def _push(self) -> str:
        result = self.git("push", "origin", "HEAD:main", check=False, retry=True)
        if result.returncode == 0:
            return ""
        self._pull_rebase()
        result = self.git("push", "origin", "HEAD:main", check=False, retry=True)
        if result.returncode == 0:
            return ""
        return f"git push failed ({_failure_text(result)}). Blocking is unchanged."

    def _pull_rebase(self) -> str:
        fetched = self.git("fetch", "origin", "main", check=False, retry=True)
        if fetched.returncode != 0:
            return f"git pull failed ({_failure_text(fetched)}). Blocking is unchanged."
        if not self.has_head():
            reset = self.git("reset", "--mixed", "origin/main", check=False, retry=True)
            if reset.returncode != 0:
                return f"git pull failed ({_failure_text(reset)}). Blocking is unchanged."
            self.git("checkout", "--", ".", check=False)
            return ""
        result = self.git("pull", "--rebase", "origin", "main", check=False, retry=True)
        if result.returncode == 0:
            return ""
        if self._resolve_rebase():
            return ""
        self.git("rebase", "--abort", check=False)
        return f"git pull failed ({_failure_text(result)}). Blocking is unchanged."

    def _resolve_rebase(self) -> bool:
        for _ in range(30):
            unmerged = self.git("diff", "--name-only", "--diff-filter=U", check=False)
            names = [line.strip() for line in unmerged.stdout.splitlines() if line.strip()]
            if names:
                for name in names:
                    side = "--theirs" if _ours(name) else "--ours"
                    self.git("checkout", side, "--", name, check=False)
                    self.git("add", "--", name, check=False)
            continued = self.git(
                "-c",
                "core.editor=true",
                "-c",
                "sequence.editor=true",
                "rebase",
                "--continue",
                check=False,
            )
            if continued.returncode == 0:
                return True
            text = (continued.stdout or "") + (continued.stderr or "")
            if "No changes" in text or "no changes" in text:
                skipped = self.git("rebase", "--skip", check=False)
                if skipped.returncode == 0:
                    return True
                continue
            if self.git("rev-parse", "-q", "--verify", "REBASE_HEAD", check=False).returncode != 0:
                return False
        return False


def _ours(path: str) -> bool:
    return path == "heartbeat.json" or path.startswith("stats/")


def _digest(path: Path) -> str | None:
    try:
        return str(path.read_bytes())
    except OSError:
        return None


def blocklist_changed_message(changed: bool) -> str:
    if changed:
        return "blocklist changed; reloaded"
    return "blocklist unchanged"
