import os
import subprocess
from pathlib import Path

import pytest

GIT_ID = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env={**os.environ, **GIT_ID})


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """A small git repo with a few deliberate DevOps smells."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# Demo\n")
    (root / "app.py").write_text(
        'import os\n\nDB_PATH = "C:\\\\Users\\\\alice\\\\db.sqlite"\nAPI_KEY = "AKIAABCDEFGHIJKLMNOP"\n'
    )
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("on: pull_request_target\njobs: {}\n")
    (root / "untracked.txt").write_text("not committed\n")
    _git(root, "init", "-q")
    _git(root, "add", "README.md", "app.py", ".github")
    _git(root, "commit", "-q", "-m", "Initial commit")
    return root
