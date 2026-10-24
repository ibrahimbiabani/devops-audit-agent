"""Clone retries, with git replaced by a scripted stand-in (no network)."""

import stat
import subprocess

import pytest

from auditor import repo


def scripted_git(monkeypatch, *outcomes):
    """Each git call takes the next outcome: None succeeds, a string fails with that stderr."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        stderr = outcomes[len(calls) - 1]
        if stderr is not None:
            raise subprocess.CalledProcessError(128, cmd, stderr=stderr)

    monkeypatch.setattr(repo.subprocess, "run", run)
    return calls


def test_clone_retries_network_errors(monkeypatch):
    dns = "fatal: unable to access 'https://github.com/a/b/': Could not resolve host: github.com"
    calls = scripted_git(monkeypatch, dns, None)
    waits = []
    path = repo.clone("https://github.com/a/b", sleep=waits.append)
    try:
        assert len(calls) == 2 and waits == [5]
    finally:
        repo.cleanup(path)


def test_clone_does_not_retry_a_missing_repository(monkeypatch):
    calls = scripted_git(monkeypatch, "remote: Repository not found.\nfatal: repository not found")
    with pytest.raises(repo.RepoError, match="Repository not found"):
        repo.clone("https://github.com/a/b", sleep=lambda s: None)
    assert len(calls) == 1


def test_clone_gives_up_after_three_network_failures(monkeypatch):
    calls = scripted_git(monkeypatch, *["fatal: Connection reset by peer"] * 3)
    with pytest.raises(repo.RepoError, match="Connection reset"):
        repo.clone("https://github.com/a/b", sleep=lambda s: None)
    assert len(calls) == 3


@pytest.mark.parametrize("version", [(3, 11), (3, 13)])
def test_cleanup_removes_read_only_clone_on_every_supported_python(monkeypatch, tmp_path, version):
    # Regression: cleanup used rmtree(onexc=...), which only exists on Python 3.12+, so every
    # audit on 3.11 crashed after writing its report.
    monkeypatch.setattr(repo.sys, "version_info", version)
    root = tmp_path / "devops-audit-x"
    (root / "repo" / ".git").mkdir(parents=True)
    obj = root / "repo" / ".git" / "pack"
    obj.write_text("x")
    obj.chmod(stat.S_IREAD)  # git objects are read-only, which plain rmtree cannot delete on Windows
    repo.cleanup(root / "repo")
    assert not root.exists()


def test_cleanup_never_raises(monkeypatch, tmp_path, caplog):
    root = tmp_path / "devops-audit-y"
    root.mkdir()

    def broken_rmtree(*args, **kwargs):
        raise PermissionError("in use")

    monkeypatch.setattr(repo.shutil, "rmtree", broken_rmtree)
    repo.cleanup(root)  # logged, not raised: the report is already written
    assert "Could not delete the temporary clone" in caplog.text
