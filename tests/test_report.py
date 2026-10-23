import json

import pytest

from auditor.redact import redact
from auditor.report import ViolationError, build_report, validate_violation, write_report


def make(**overrides):
    v = {
        "rule_id": "SEC-001",
        "category": "security",
        "severity": "critical",
        "file": "config.py",
        "line": 17,
        "description": "Hardcoded API credential",
        "evidence": "Credential assigned directly in source code",
        "recommendation": "Use a secrets manager or environment variable",
    }
    v.update(overrides)
    return v


def test_valid_violation_passes():
    assert validate_violation(make()) == make()


def test_null_file_and_line_allowed():
    v = validate_violation(make(file=None, line=None))
    assert v["file"] is None and v["line"] is None


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"severity": "urgent"}, "severity"),
        ({"category": "ml"}, "category"),
        ({"rule_id": "sec1"}, "rule_id"),
        ({"line": 0}, "line"),
        ({"line": "17"}, "line"),
        ({"file": None, "line": 3}, "line given without file"),
        ({"evidence": "  "}, "evidence"),
    ],
)
def test_invalid_violations_rejected(overrides, message):
    with pytest.raises(ViolationError, match=message):
        validate_violation(make(**overrides))


def test_missing_and_extra_fields_rejected():
    v = make()
    del v["evidence"]
    v["confidence"] = 0.9
    with pytest.raises(ViolationError, match="missing fields: evidence; unknown fields: confidence"):
        validate_violation(v)


def test_path_normalization_keeps_dotfiles():
    assert validate_violation(make(file="./.github/workflows/ci.yml"))["file"] == ".github/workflows/ci.yml"


def test_secret_values_redacted_from_text_fields():
    v = validate_violation(make(evidence='API_KEY = "AKIAABCDEFGHIJKLMNOP" and password="hunter22"'))
    assert "AKIA" not in v["evidence"]
    assert "hunter22" not in v["evidence"]


def test_redact_leaves_plain_text_alone():
    assert redact("Use environment variables for tokens") == "Use environment variables for tokens"


def test_report_sorted_by_severity_and_written(tmp_path):
    report = build_report("https://github.com/a/b", [make(severity="low"), make(rule_id="SEC-002")])
    assert [v["severity"] for v in report["violations"]] == ["critical", "low"]
    out = tmp_path / "nested" / "report.json"
    write_report(report, out)
    assert json.loads(out.read_text()) == report


def test_empty_report_is_valid_json(tmp_path):
    out = tmp_path / "r.json"
    write_report(build_report("https://github.com/a/b", []), out)
    assert json.loads(out.read_text()) == {"repository": "https://github.com/a/b", "violations": []}


def test_redact_obj_keeps_structure():
    from auditor.redact import redact_obj

    out = redact_obj({"evidence": 'password="hunter22"', "line": 3, "tags": ["token: abcd1234", None]})
    assert out["line"] == 3 and out["tags"][1] is None
    assert "hunter22" not in out["evidence"] and "abcd1234" not in out["tags"][0]


def test_redacts_putty_keys_and_password_hashes():
    from auditor.redact import redact_tokens

    putty = "PuTTY-User-Key-File-2: ssh-rsa\nEncryption: none\nPrivate-Lines: 1\nAAAAsecret\nPrivate-MAC: abc123\n"
    assert "AAAAsecret" not in redact_tokens(putty)
    for h in ("admin:$apr1$tp8glkbm$abcdefghijklmnopqrstuv", "root:$6$saltsalt$hashhashhashhash:19000"):
        out = redact_tokens(h)
        assert "[REDACTED:password_hash]" in out and "hash" not in out.split("]")[-1][:4]


def test_rule_id_prefix_must_match_category():
    with pytest.raises(ViolationError, match="must start with CI-"):
        validate_violation(make(category="ci_cd"))  # make() uses SEC-001
    assert validate_violation(make(category="ci_cd", rule_id="CI-001"))["rule_id"] == "CI-001"


def test_prompt_covers_exactly_the_nine_areas():
    from auditor.prompts import SYSTEM_PROMPT
    from auditor.report import CATEGORIES, RULE_PREFIXES

    assert len(CATEGORIES) == 9
    for category, prefix in RULE_PREFIXES.items():
        assert f"{category} -> {prefix}" in SYSTEM_PROMPT
    for phrase in (
        "missing README, unsuitable .gitignore, tracked temporary/generated files",
        "inconsistent specifications, absent required lockfiles, weak version constraints",
        "missing linting/formatting, obvious maintainability problems",
        "missing tests, failing tests, no CI test execution",
        "incorrect triggers, ineffective gates, skipped validation",
        "exposed credentials, unsafe workflow permissions",
        "machine-specific paths, committed local settings, poor environment separation",
        "unsafe PR automation, lack of review guidance where relevant",
        "missing build validation, risky release or deployment automation",
        "Report ONLY violations that belong to one of these nine areas".replace("Report", "report"),
        "Out of scope",
    ):
        assert " ".join(phrase.split()) in " ".join(SYSTEM_PROMPT.split()), phrase
