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
from pathlib import Path

GITHUB_URL = re.compile(r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


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


def clone(url: str, timeout: int = 300) -> Path:
    """Clone ``url`` into a fresh temp directory and return its path.

    ``--filter=blob:none`` keeps full history (for git_log) while only
    downloading file contents for the checked-out commit.
    """
    dest = Path(tempfile.mkdtemp(prefix="devops-audit-"))
    cmd = ["git", "clone", "--quiet", "--filter=blob:none", "--no-recurse-submodules", url, str(dest / "repo")]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout, env=git_env())
    except FileNotFoundError as e:
        cleanup(dest)
        raise RepoError("git is not installed or not on PATH") from e
    except subprocess.TimeoutExpired as e:
        cleanup(dest)
        raise RepoError(f"Cloning {url} timed out after {timeout}s") from e
    except subprocess.CalledProcessError as e:
        cleanup(dest)
        raise RepoError(f"Could not clone {url}: {e.stderr.strip() or e}") from e
    return dest / "repo"


def cleanup(path: Path) -> None:
    """Remove a temp clone. Handles read-only git objects on Windows."""

    def _onexc(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    root = path if path.name.startswith("devops-audit-") else path.parent
    if root.name.startswith("devops-audit-"):
        shutil.rmtree(root, onexc=_onexc)
