"""Content-based credential hints: which files to open, never the values, no false tags on code or docs."""

import pytest

from auditor.content_hints import credential_lines, is_test_path
from tools import RepoTools


@pytest.mark.parametrize(
    "path, text, expected",
    [
        ("wp-config.php", "define( 'DB_PASSWORD', 'hunter22pw' );", [1]),
        (".esmtprc", 'identity me\npassword "s3cretvalue"', [2]),
        (".bash_profile", "export GITHUB_TOKEN=abc123def456", [1]),
        ("deployment-config.json", '{"host": "x", "password": "Pa55word!"}', [1]),
        ("filezilla.xml", '<Pass encoding="plain">hunter22</Pass>', [1]),
        ("app.ini", "url = postgres://admin:topsecret@db.example.com/app", [1]),
        ("etc/shadow", "root:$6$saltsalt$hashhashhashhash:19000", [1]),
        ("settings.py", 'SECRET_KEY = "django-insecure-abc123"', [1]),
    ],
)
def test_credential_like_content_is_found(path, text, expected):
    assert credential_lines(path, text) == expected


@pytest.mark.parametrize(
    "path, text",
    [
        ("signer.py", "self.secret_key = secret_key"),  # a variable, not a committed value
        ("app.py", 'password = os.environ["DB_PASSWORD"]'),
        ("app.js", "const token = process.env.TOKEN;"),
        ("ci.yml", "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}"),
        ("conf.py", 'author = "Pallets"'),  # "author" is not "auth"
        ("README.md", "s = Serializer(secret_key='my-secret')"),  # documentation example
        ("config.yml", "password: null"),
    ],
)
def test_references_and_docs_are_not_tagged(path, text):
    assert credential_lines(path, text) == []


def test_test_paths():
    assert (
        is_test_path("tests/test_x.py")
        and is_test_path("src/pkg/test_utils.py")
        and is_test_path("spec/fixtures/a.yml")
    )
    assert not is_test_path("src/latest.py") and not is_test_path("contest/app.py")


def test_tags_show_lines_not_values_and_test_files_are_not_required(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "prod.json").write_text('{"db_password": "Sup3rSecretValue"}\n')
    (tmp_path / "config" / "prod.json.example").write_text('{"db_password": "Sup3rSecretValue"}\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_auth.py").write_text('API_KEY = "fixture-key-123"\n')
    t = RepoTools(tmp_path)
    listing = t.list_files()
    assert "config/prod.json (" in listing and "B) [credential-like content: line 1]" in listing
    assert "tests/test_auth.py" in listing and "[credential-like content in a test file: line 1]" in listing
    assert "Sup3rSecretValue" not in listing and "fixture-key-123" not in listing
    assert t.must_read_files() == ["config/prod.json"]  # not the template, not the test fixture


def test_type_annotations_are_not_values():
    # Regression (2026-10-24, pallets/itsdangerous): `secret_keys: list[bytes]` read as a value.
    assert credential_lines("signer.py", "        self.secret_keys: list[bytes] = _make_keys_list(secret_key)") == []
