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
