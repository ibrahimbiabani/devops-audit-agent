import pytest

from tools import RepoTools, ToolError, dispatch, is_sensitive_name, is_workflow_file


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


@pytest.mark.parametrize(
    "path, expected",
    [
        (".env", True),
        ("web/.env.production", True),
        (".ssh/id_rsa", True),
        ("certs/server.pem", True),
        (".docker/config.json", True),
        ("db/.pgpass", True),
        (".env.example", False),
        ("config/credentials.sample.json", False),
        ("docs/.env", False),
        (".ssh/id_rsa.pub", False),
        ("src/keys.py", False),
    ],
)
def test_is_sensitive_name(path, expected):
    assert is_sensitive_name(path) is expected


def test_is_workflow_file_only_direct_children():
    assert is_workflow_file(".github/workflows/ci.yml")
    assert is_workflow_file(".github/workflows/release.yaml")
    assert not is_workflow_file(".github/workflows/controls/nested.yml")
    assert not is_workflow_file(".github/dependabot.yml")


def test_list_files_tags_sensitive_names_and_must_read(sample_repo):
    import subprocess

    (sample_repo / ".env").write_text("DB_PASSWORD=hunter22\n")
    (sample_repo / ".env.example").write_text("DB_PASSWORD=\n")
    subprocess.run(["git", "-C", str(sample_repo), "add", ".env", ".env.example"], check=True)
    t = RepoTools(sample_repo)
    lines = {line.split(" (")[0]: line for line in t.list_files().splitlines()[1:]}
    assert "[sensitive name]" in lines[".env"]
    assert "[sensitive name]" not in lines[".env.example"]
    assert "[sensitive name]" not in lines["app.py"]
    assert lines["app.py"].endswith("[credential-like content: line 4]")  # found by content, not name
    assert "credential-like" not in lines[".env.example"]  # templates are never tagged
    assert t.must_read_files() == [".env", ".github/workflows/ci.yml", "app.py"]


def test_relpath_canonicalizes(sample_repo):
    assert RepoTools(sample_repo).relpath("./.github//workflows/../workflows/ci.yml") == ".github/workflows/ci.yml"
