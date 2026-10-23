"""Violation validation and the JSON report contract."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from auditor.redact import redact

SEVERITIES = ("critical", "high", "medium", "low")
# The nine DevOps inspection areas from the assignment (section 4), each with its rule_id prefix.
RULE_PREFIXES = {
    "repository_hygiene": "HYG",
    "dependency_management": "DEP",
    "code_quality": "CQ",
    "testing": "TEST",
    "ci_cd": "CI",
    "security": "SEC",
    "configuration": "CFG",
    "collaboration": "COL",
    "build_release": "BLD",
}
CATEGORIES = tuple(RULE_PREFIXES)
FIELDS = ("rule_id", "category", "severity", "file", "line", "description", "evidence", "recommendation")
RULE_ID = re.compile(r"^[A-Z]{2,5}-\d{3}$")
TEXT_FIELDS = ("description", "evidence", "recommendation")


class ViolationError(ValueError):
    """A proposed violation does not satisfy the report contract."""


def validate_violation(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized, redacted violation or raise ViolationError listing every problem."""
    problems = []
    missing = [f for f in FIELDS if f not in raw]
    if missing:
        problems.append(f"missing fields: {', '.join(missing)}")
    extra = [f for f in raw if f not in FIELDS]
    if extra:
        problems.append(f"unknown fields: {', '.join(extra)}")
    if problems:
        raise ViolationError("; ".join(problems))

    v = {f: raw[f] for f in FIELDS}
    v["severity"] = str(v["severity"]).lower()
    v["category"] = str(v["category"]).lower()
    v["rule_id"] = str(v["rule_id"]).upper()

    if v["severity"] not in SEVERITIES:
        problems.append(f"severity must be one of {SEVERITIES}")
    if v["category"] not in CATEGORIES:
        problems.append(f"category must be one of {CATEGORIES}")
    if not RULE_ID.match(v["rule_id"]):
        problems.append("rule_id must look like 'SEC-001'")
    elif v["category"] in RULE_PREFIXES and v["rule_id"].split("-")[0] != RULE_PREFIXES[v["category"]]:
        problems.append(f"rule_id for category {v['category']} must start with {RULE_PREFIXES[v['category']]}-")
    if v["file"] in ("", "null"):
        v["file"] = None
    if v["file"] is not None and not isinstance(v["file"], str):
        problems.append("file must be a repo-relative path string or null")
    if v["line"] is not None:
        if isinstance(v["line"], bool) or not isinstance(v["line"], int) or v["line"] < 1:
            problems.append("line must be a positive integer or null")
        elif v["file"] is None:
            problems.append("line given without file")
    for f in TEXT_FIELDS:
        if not isinstance(v[f], str) or not v[f].strip():
            problems.append(f"{f} must be a non-empty string")
    if problems:
        raise ViolationError("; ".join(problems))

    if v["file"]:
        v["file"] = v["file"].replace("\\", "/").removeprefix("./").lstrip("/") or None
    for f in TEXT_FIELDS:
        v[f] = redact(v[f].strip())
    return v


def dedupe_key(v: dict[str, Any]) -> tuple:
    return (v["rule_id"], v["file"], v["line"])


def build_report(repository: str, violations: list[dict[str, Any]]) -> dict[str, Any]:
    order = {s: i for i, s in enumerate(SEVERITIES)}
    ordered = sorted(violations, key=lambda v: (order[v["severity"]], v["file"] or "", v["line"] or 0))
    return {"repository": repository, "violations": ordered}


def write_report(report: dict[str, Any], output: Path) -> None:
    """Write atomically so a crash never leaves half-written JSON behind."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=output.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, output)
