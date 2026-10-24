"""GitHub Actions workflow inspection: the facts of a workflow, with line numbers.

The output states what each workflow does - triggers, permissions, jobs, what each
job depends on, which steps publish or run tests, where untrusted input and secrets
appear - and never whether that is a violation. The agent decides that, as with
every other tool's output. Facts are collected from the parsed YAML (structure) and
a line scan of the raw file (exact line numbers for each signal).
"""

from __future__ import annotations

import re
from typing import Any

import yaml

# Event payload fields that an outside contributor controls, and other untrusted inputs.
UNTRUSTED = re.compile(
    r"\$\{\{[^}]*\b(?:github\.event\.(?:issue|comment|review|review_comment|pull_request|discussion|"
    r"pages|head_commit|commits|workflow_run|client_payload)\b[\w.\[\]'\"*-]*|github\.head_ref|inputs\.[\w-]+)[^}]*\}\}"
)
SECRET_REF = re.compile(r"\$\{\{\s*secrets\.([\w-]+)\s*\}\}")
RISKY_TRIGGERS = ("pull_request_target", "issue_comment", "issues", "workflow_run", "discussion", "discussion_comment")
PUBLISH = re.compile(
    r"\b(?:npm publish|twine upload|gh-action-pypi-publish|docker push|docker/build-push-action|gh release|"
    r"deploy|kubectl apply|helm upgrade|terraform apply|cargo publish|gem push|mvn deploy|git push)\b",
    re.IGNORECASE,
)
TESTS = re.compile(
    r"\b(?:pytest|py\.test|tox|nox|unittest|npm (?:run )?test|yarn test|pnpm test|jest|go test|cargo test|"
    r"mvn (?:test|verify)|gradle(?:w)? test|make test|rspec|phpunit)\b",
    re.IGNORECASE,
)
# Line-level signals: (label, pattern). Each match is listed with its line number.
LINE_SIGNALS: list[tuple[str, re.Pattern[str]]] = [
    ("bash debug mode (set -x)", re.compile(r"\bset -[a-z]*x")),
    ("verbose HTTP client", re.compile(r"\bcurl\b[^\n]*\s(?:-v\b|--verbose\b)|\bwget\b[^\n]*\s(?:-d\b|--debug\b)")),
    ("environment dump", re.compile(r"\b(?:printenv|env\s*\|)|\bset\s*$")),
    ("remote script piped to a shell", re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b")),
    ("value passed as a command-line credential", re.compile(r"--(?:token|password|api-key|apikey|secret)[= ]")),
    ("step failure ignored", re.compile(r"continue-on-error:\s*true|\|\|\s*true\b")),
    ("actor/name check with contains()", re.compile(r"contains\([^)]*github\.(?:actor|triggering_actor)")),
    ("pull request auto-merge or approval", re.compile(r"\bgh pr (?:merge|review --approve)|auto-merge|automerge")),
    ("artifact upload", re.compile(r"actions/upload-artifact")),
    ("checkout of a pull request ref", re.compile(r"ref:\s*\$\{\{[^}]*(?:head\.ref|head\.sha|head_ref|pull/)")),
]
SECRETISH_NAME = re.compile(r"(?i)(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)")
# Printing a secret: `echo ... ${{ secrets.X }}` or `echo ... $SOME_TOKEN`.
SECRET_PRINTED = re.compile(
    r"\b(?:echo|printf|cat)\b[^\n]*(?:\$\{\{\s*secrets\.|\$\{?\w*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY)\w*)",
    re.IGNORECASE,
)
INSTALL_ONLY = re.compile(r"^(?:python\d?(?:\.\d+)? -m )?pip3? install\b[^\n;&|]*$|^npm (?:ci|install)\b[^\n;&|]*$")


class _LineLoader(yaml.SafeLoader):
    """SafeLoader that records the 1-based line of every mapping as '__line__'."""


def _construct_mapping(loader: _LineLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    mapping = loader.construct_mapping(node, deep=True)
    mapping["__line__"] = node.start_mark.line + 1  # where the mapping's first key is
    # Where each key itself is, e.g. the `build:` line of a job rather than its first setting.
    mapping["__keylines__"] = {
        loader.construct_object(k, deep=True): k.start_mark.line + 1
        for k, _ in node.value
        if k.tag != "tag:yaml.org,2002:merge"
    }
    return mapping


_LineLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in ("__line__", "__keylines__")}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _triggers(on: Any) -> list[str]:
    """`on:` as readable parts, e.g. ['push (branches: main)', 'pull_request']."""
    if on is None:
        return []
    if isinstance(on, str):
        return [on]
    if isinstance(on, list):
        return [str(t) for t in on]
    parts = []
    for event, spec in _clean(on).items():
        filters = []
        if isinstance(spec, dict):
            for key in ("branches", "tags", "types", "paths"):
                if key in spec:
                    vals = spec[key] if isinstance(spec[key], list) else [spec[key]]
                    filters.append(f"{key}: {', '.join(map(str, vals))}")
            if event == "workflow_dispatch" and spec.get("inputs"):
                filters.append(f"inputs: {', '.join(spec['inputs'])}")
        parts.append(f"{event} ({'; '.join(filters)})" if filters else str(event))
    return parts


def _permissions(value: Any) -> str:
    if value is None:
        return "not declared (the repository's default token permissions apply)"
    if isinstance(value, str):
        return value
    perms = _clean(value)
    return ", ".join(f"{k}: {v}" for k, v in perms.items()) if perms else "{} (none)"


def _short(text: str, limit: int = 110) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def summarize(path: str, text: str) -> str:
    """The facts of one workflow file, or a note that it could not be parsed."""
    try:
        doc = yaml.load(text, Loader=_LineLoader)  # noqa: S506 - _LineLoader is a SafeLoader
    except yaml.YAMLError as e:
        return f"{path}\n  could not be parsed as YAML: {_short(e, 200)}"
    if not isinstance(doc, dict):
        return f"{path}\n  not a workflow (top level is not a mapping)"

    on = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on:` key as the boolean True
    triggers = _triggers(on)
    risky = [t.split(" ")[0] for t in triggers if t.split(" ")[0] in RISKY_TRIGGERS]
    out = [path, f"  on: {', '.join(triggers) or 'none found'}"]
    if risky:
        out.append(f"  triggers that carry outside input or run with repository privileges: {', '.join(risky)}")
    out.append(f"  permissions (workflow level): {_permissions(doc.get('permissions'))}")

    lines = text.splitlines()
    jobs = doc.get("jobs") if isinstance(doc.get("jobs"), dict) else {}
    job_ranges: list[tuple[int, str]] = []
    keylines = jobs.get("__keylines__", {})
    for name, job in jobs.items():
        if name in ("__line__", "__keylines__") or not isinstance(job, dict):
            continue
        job_line = keylines.get(name, job.get("__line__", 0))
        job_ranges.append((job_line, str(name)))
        needs = job.get("needs")
        needs = ", ".join(needs) if isinstance(needs, list) else (needs or "none")
        out.append(f"  job {name} (line {job_line})")
        out.append(f"    runs-on: {_short(_clean(job.get('runs-on', 'not set')))}")
        out.append(f"    needs: {needs} (jobs in this workflow only)")
        if "permissions" in job:
            out.append(f"    permissions (job level): {_permissions(job['permissions'])}")
        if job.get("if") is not None:
            out.append(f"    if: {_short(job['if'])}")
        if job.get("environment") is not None:
            out.append(f"    environment: {_short(_clean(job['environment']))}")
        if job.get("uses"):
            out.append(f"    calls reusable workflow: {job['uses']}")
        steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
        publishes, tests = [], []
        for step in steps:
            body = f"{step.get('uses', '')} {step.get('run', '')} {step.get('name', '')}"
            label = step.get("name") or step.get("uses") or _short(step.get("run", ""), 50)
            if PUBLISH.search(body):
                publishes.append(f"line {step.get('__line__')} ({_short(label, 50)})")
            # A step that only installs a test tool (`pip install nox`) does not run tests.
            command = f"{step.get('uses', '')} {step.get('run', '')}"
            if TESTS.search(command) and not INSTALL_ONLY.match(str(step.get("run", "")).strip()):
                tests.append(f"line {step.get('__line__')} ({_short(label, 50)})")
        out.append(f"    steps: {len(steps)}")
        out.append(f"    steps that publish, deploy or push: {'; '.join(publishes) or 'none'}")
        out.append(f"    steps that run tests: {'; '.join(tests) or 'none'}")

    job_ranges.sort()

    def job_at(n: int) -> str:
        name = "(workflow level)"
        for start, job_name in job_ranges:
            if n >= start:
                name = job_name
        return name

    signals = []
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        where = job_at(n)
        for m in UNTRUSTED.finditer(line):
            field = "runs-on" if "runs-on" in line else "if condition" if stripped.startswith("if:") else "run/with/env"
            signals.append(f"line {n} [{where}] untrusted input in {field}: {m.group(0)}")
        for m in SECRET_REF.finditer(line):
            signals.append(f"line {n} [{where}] secret used: secrets.{m.group(1)}")
        for label, pattern in LINE_SIGNALS:
            if pattern.search(line):
                signals.append(f"line {n} [{where}] {label}: {_short(stripped, 90)}")
        if SECRET_PRINTED.search(line):
            signals.append(f"line {n} [{where}] secret-like value printed: {_short(stripped, 90)}")
        key, sep, value = stripped.partition(":")
        value = value.strip().strip("'\"")
        if sep and SECRETISH_NAME.search(key) and value and not value.startswith(("${{", "$", "|", ">")):
            if value.lower() not in ("true", "false", "read", "write", "none"):
                signals.append(f"line {n} [{where}] literal value assigned to {key.strip()} (value not shown)")
        if "self-hosted" in line and "runs-on" in line:
            signals.append(f"line {n} [{where}] self-hosted runner: {_short(stripped, 90)}")
    out.append("  signals:" if signals else "  signals: none")
    out.extend(f"    {s}" for s in signals)
    return "\n".join(out)
