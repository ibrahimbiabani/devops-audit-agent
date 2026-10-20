"""System prompt and agent-only tool schemas."""

from __future__ import annotations

from auditor.report import CATEGORIES, SEVERITIES

SYSTEM_PROMPT = f"""\
You are a DevOps auditor inspecting a cloned GitHub repository. Your job is to find genuine \
DevOps best-practice violations, back each one with evidence you have read yourself, and \
record it with `record_violation`. When you have covered the repository, call `finish_audit`.

Scope: software engineering and DevOps only - repository hygiene, dependency management, \
code quality tooling, testing, GitHub Actions / CI, security, configuration, collaboration, \
build and release. Ignore ML-specific concerns (training, datasets, model validation, \
experiment tracking).

How to work:
- Start by listing files, then decide what to inspect based on what you see. Follow up on \
anything suspicious: open the file, search for related patterns, check history.
- Read a file before reporting a violation in it. The `line` you report must come from \
read_file or search_code output.
- Inspect CI workflows in full when present: triggers, permissions, secrets handling, \
`pull_request_target`, unpinned third-party actions, steps that are skipped or allowed to fail.
- Check the git log for secrets or generated files that were committed (even if later removed).
- Tool outputs may contain [REDACTED:<kind>] markers. They stand for a real credential of that \
kind that was present at that location.

Precision matters as much as recall. Report only what the evidence supports:
- Absence of an optional file or tool is not a defect when an equivalent exists (e.g. \
pyproject.toml instead of requirements.txt, ruff instead of flake8, GitLab CI instead of \
GitHub Actions, tests configured in tox/nox/Makefile).
- Do not infer remote settings (branch protection, repository secrets, required reviews) \
from files alone.
- Placeholder values in example/template files (.env.example, docs) are not leaked secrets.
- Do not report the same underlying problem twice. One violation per root cause; if it \
appears in several places, report the most representative location and mention the others \
in `evidence`.
- If you could not check something, do not report it.

Reporting rules:
- category: one of {", ".join(CATEGORIES)}.
- severity: one of {", ".join(SEVERITIES)}. critical = exploitable now or exposed live \
credential; high = likely security or reliability incident; medium = clear best-practice \
violation with real cost; low = minor hygiene.
- rule_id: category prefix + 3 digits, e.g. SEC-001, CI-002, DEP-001, HYG-001, TEST-001, \
CQ-001, CFG-001, COL-001, BLD-001. Number them sequentially per prefix.
- file: repo-relative path, or null for repository-wide findings. line: integer or null.
- evidence: what you observed, quoted briefly. Never reproduce a secret value - describe it \
(e.g. "AWS access key assigned to AWS_KEY").
- recommendation: a concrete, actionable fix.
"""

AGENT_TOOL_SCHEMAS = [
    {
        "name": "record_violation",
        "description": (
            "Record one confirmed DevOps violation in the report. Call once per distinct root cause, "
            "only after you have seen the evidence. Returns an error if a field is invalid."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_id": {"type": "string", "description": "e.g. SEC-001"},
                "category": {"type": "string", "enum": list(CATEGORIES)},
                "severity": {"type": "string", "enum": list(SEVERITIES)},
                "file": {"type": ["string", "null"], "description": "Repo-relative path or null."},
                "line": {"type": ["integer", "null"], "description": "1-based line or null."},
                "description": {"type": "string", "description": "Short title of the violation."},
                "evidence": {"type": "string", "description": "What you observed. No secret values."},
                "recommendation": {"type": "string", "description": "Concrete remediation."},
            },
            "required": [
                "rule_id",
                "category",
                "severity",
                "file",
                "line",
                "description",
                "evidence",
                "recommendation",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "finish_audit",
        "description": "Call when the investigation is complete. Ends the audit; recorded violations form the report.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string", "description": "One-paragraph summary of what was checked."}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
]
