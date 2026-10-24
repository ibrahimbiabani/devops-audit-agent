"""Clone retries, with git replaced by a scripted stand-in (no network)."""

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
