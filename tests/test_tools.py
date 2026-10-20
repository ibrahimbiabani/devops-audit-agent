import pytest

from tools import RepoTools, ToolError, dispatch


def test_list_files_shows_only_tracked(sample_repo):
    out = RepoTools(sample_repo).list_files()
    assert "app.py" in out
    assert ".github/workflows/ci.yml" in out
    assert "untracked.txt" not in out


def test_list_files_pattern_and_subdir(sample_repo):
    t = RepoTools(sample_repo)
    assert "ci.yml" in t.list_files(pattern="*.yml")
    assert "app.py" not in t.list_files(subdir=".github")


def test_read_file_numbers_lines(sample_repo):
    out = RepoTools(sample_repo).read_file("app.py", start_line=3, end_line=3)
    assert "    3| DB_PATH" in out
    assert "import os" not in out


def test_read_file_redacts_known_token_formats(sample_repo):
    out = RepoTools(sample_repo).read_file("app.py")
    assert "AKIAABCDEFGHIJKLMNOP" not in out
    assert "[REDACTED:aws_access_key_id]" in out


@pytest.mark.parametrize("path", ["../outside.txt", "..\\..\\x", "/etc/passwd"])
def test_read_file_rejects_escape(sample_repo, path):
    with pytest.raises(ToolError):
        RepoTools(sample_repo).read_file(path)


def test_read_missing_file(sample_repo):
    with pytest.raises(ToolError, match="not found"):
        RepoTools(sample_repo).read_file("nope.py")


def test_search_code_finds_matches(sample_repo):
    out = RepoTools(sample_repo).search_code(r"pull_request_target")
    assert ".github/workflows/ci.yml:1:" in out


def test_search_code_glob_and_case(sample_repo):
    t = RepoTools(sample_repo)
    assert "app.py:3" in t.search_code("db_path", ignore_case=True, path_glob="*.py")
    assert t.search_code("db_path", path_glob="*.py") == "No matches."


def test_search_code_invalid_regex(sample_repo):
    with pytest.raises(ToolError, match="Invalid regular expression"):
        RepoTools(sample_repo).search_code("(")


def test_git_log(sample_repo):
    out = RepoTools(sample_repo).git_log()
    assert "Initial commit" in out
    assert "app.py" in out


def test_dispatch_rejects_unknown_tool_and_bad_args(sample_repo):
    t = RepoTools(sample_repo)
    with pytest.raises(ToolError, match="Unknown tool"):
        dispatch(t, "rm_rf", {})
    with pytest.raises(ToolError, match="Bad arguments"):
        dispatch(t, "read_file", {"wrong": 1})


def test_tools_do_not_modify_repo(sample_repo):
    import subprocess

    t = RepoTools(sample_repo)
    t.list_files()
    t.read_file("app.py")
    t.search_code("API")
    t.git_log()
    status = subprocess.run(
        ["git", "-C", str(sample_repo), "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout
    assert status.strip() == "?? untracked.txt"
