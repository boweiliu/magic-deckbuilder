"""Every deck change is a git commit in the decks folder, pushed to GitHub in the background.

The decks folder (``DATA_DIR/decks``) is its own git repository whose ``origin`` is the private
repo the deck lists are copied to, reached through the workspace's latchkey gateway. A commit
happens inside the request that changed the deck; the push runs on one background thread and
retries until it succeeds, so a slow or failed push never loses or delays a save.
"""

import os
import subprocess
import threading
from pathlib import Path

from loguru import logger

PUSH_RETRY_SECONDS = 60.0
_GIT_IDENTITY = ["-c", "user.name=deck-list", "-c", "user.email=deck-list@workspace"]


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60, env=env)


def is_repo(repo: Path) -> bool:
    return (repo / ".git").exists()


def commit(repo: Path, path: Path, message: str) -> str | None:
    """Commit one deck file; returns the new commit id, or None when git could not."""
    return commit_files(repo, (path,), message)


def commit_files(repo: Path, paths: tuple[Path, ...], message: str) -> str | None:
    """Commit several deck files as one change; returns the new commit id, or None when git could not."""
    if not is_repo(repo):
        logger.warning("Deck folder {} has no history yet; change saved without a commit", repo)
        return None
    names = [p.name for p in paths]
    added = _git(repo, "add", "--", *names)
    done = _git(repo, *_GIT_IDENTITY, "commit", "--quiet", "-m", message, "--", *names) if added.returncode == 0 else added
    if done.returncode != 0 and "nothing to commit" not in done.stdout + done.stderr:
        logger.error("Could not commit {}: {}", ", ".join(names), (done.stderr or done.stdout).strip())
        return None
    return _git(repo, "rev-parse", "--short", "HEAD").stdout.strip() or None


def _push_env() -> dict[str, str]:
    """The gateway's password as git config in the environment, never on the command line."""
    env = dict(os.environ)
    password = os.environ.get("LATCHKEY_GATEWAY_PASSWORD", "")
    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
               GIT_CONFIG_VALUE_0=f"X-Latchkey-Gateway-Password: {password}", GIT_TERMINAL_PROMPT="0")
    return env


class Pusher:
    """Pushes the decks repo to origin whenever asked, one push at a time, retrying failures."""

    def __init__(self, repo: Path) -> None:
        self._repo = repo
        self._wanted = threading.Event()
        self._lock = threading.Lock()
        self.last_pushed: str | None = None
        self.last_error: str | None = None
        self._thread: threading.Thread | None = None

    def request(self) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="deck-push", daemon=True)
                self._thread.start()
        self._wanted.set()

    def _loop(self) -> None:
        # Event.wait() with no timeout returns True once set, so this runs for the life of the app.
        while self._wanted.wait():
            self._wanted.clear()
            if not is_repo(self._repo):
                continue
            head = _git(self._repo, "rev-parse", "--short", "HEAD").stdout.strip()
            pushed = _git(self._repo, "push", "--quiet", "origin", "HEAD:main", env=_push_env())
            if pushed.returncode == 0:
                self.last_pushed, self.last_error = head, None
            else:
                self.last_error = (pushed.stderr or pushed.stdout).strip()[-300:]
                logger.warning("Deck backup push failed, retrying in {}s: {}", PUSH_RETRY_SECONDS, self.last_error)
                retry = threading.Timer(PUSH_RETRY_SECONDS, self._wanted.set)
                retry.daemon = True
                retry.start()

    def is_backed_up(self) -> bool:
        if not is_repo(self._repo):
            return False
        return self.last_error is None and self.last_pushed == _git(self._repo, "rev-parse", "--short", "HEAD").stdout.strip()


def head(repo: Path) -> str | None:
    if not is_repo(repo):
        return None
    return _git(repo, "rev-parse", "--short", "HEAD").stdout.strip() or None
