"""Fetching the target repository into an isolated temporary directory.

The clone is read-only from the agent's point of view: tools never write to it
and no repository code is executed.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

GITHUB_URL = re.compile(r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
# git's messages for a network that is down or flaky (e.g. "Could not resolve host: github.com",
# seen on 2026-10-23), as opposed to a repository that does not exist.
NETWORK_ERROR = re.compile(
    r"Could not resolve host|Connection reset|Connection timed out|Failed to connect|"
    r"early EOF|RPC failed|Recv failure|SSL_ERROR",
    re.IGNORECASE,
)
CLONE_RETRY_WAITS = (5, 15)  # seconds before the 2nd and 3rd attempts


class RepoError(Exception):
    """The repository could not be fetched or is invalid."""


def normalize_github_url(url: str) -> str:
    m = GITHUB_URL.match(url.strip())
    if not m:
        raise RepoError(f"Not a public GitHub repository URL: {url!r} (expected https://github.com/<owner>/<repo>)")
    return f"https://github.com/{m['owner']}/{m['name']}"


def git_env() -> dict[str, str]:
    """Environment for git subprocesses: never prompt, never fetch LFS blobs."""
    env = os.environ.copy()
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_ASKPASS": ""})
    return env


def clone(url: str, timeout: int = 300, sleep=time.sleep) -> Path:
    """Clone ``url`` into a fresh temp directory and return its path.

    ``--filter=blob:none`` keeps full history (for git_log) while only
    downloading file contents for the checked-out commit. A clone that fails
    on a network error is retried (see CLONE_RETRY_WAITS); any other failure,
    such as a repository that does not exist, is reported at once.
    """
    for attempt in range(len(CLONE_RETRY_WAITS) + 1):
        dest = Path(tempfile.mkdtemp(prefix="devops-audit-"))
        cmd = ["git", "clone", "--quiet", "--filter=blob:none", "--no-recurse-submodules", url, str(dest / "repo")]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout, env=git_env())
            return dest / "repo"
        except FileNotFoundError as e:
            cleanup(dest)
            raise RepoError("git is not installed or not on PATH") from e
        except subprocess.TimeoutExpired as e:
            cleanup(dest)
            raise RepoError(f"Cloning {url} timed out after {timeout}s") from e
        except subprocess.CalledProcessError as e:
            cleanup(dest)
            stderr = (e.stderr or "").strip()
            if attempt < len(CLONE_RETRY_WAITS) and NETWORK_ERROR.search(stderr):
                sleep(CLONE_RETRY_WAITS[attempt])
                continue
            raise RepoError(f"Could not clone {url}: {stderr or e}") from e
    raise RepoError(f"Could not clone {url}")  # not reached


def cleanup(path: Path) -> None:
    """Remove a temp clone. Handles read-only git objects on Windows."""

    def _onexc(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    root = path if path.name.startswith("devops-audit-") else path.parent
    if root.name.startswith("devops-audit-"):
        shutil.rmtree(root, onexc=_onexc)
