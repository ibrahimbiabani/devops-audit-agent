import pytest

import agent
from auditor.repo import RepoError, normalize_github_url


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/psf/requests", "https://github.com/psf/requests"),
        ("https://github.com/psf/requests.git", "https://github.com/psf/requests"),
        ("https://github.com/psf/requests/", "https://github.com/psf/requests"),
    ],
)
def test_normalize_github_url(url, expected):
    assert normalize_github_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://gitlab.com/a/b",
        "https://github.com/a",
        "git@github.com:a/b.git",
        "file:///etc",
    ],
)
def test_rejects_non_github_urls(url):
    with pytest.raises(RepoError):
        normalize_github_url(url)


def test_cli_invalid_repo_exits_nonzero(tmp_path, capsys):
    code = agent.main(["--repo", "not-a-url", "--output", str(tmp_path / "r.json")])
    assert code != 0
    assert "error:" in capsys.readouterr().err
    assert not (tmp_path / "r.json").exists()


def test_cli_requires_arguments():
    with pytest.raises(SystemExit) as e:
        agent.main([])
    assert e.value.code != 0
