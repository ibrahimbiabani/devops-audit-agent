"""Which tracked files look, from their contents, like they hold a committed credential.

File names catch `.env` or `id_rsa`, but most committed secrets sit in ordinary files
(`wp-config.php`, `settings.py`, `.bashrc`, `deployment-config.json`). This scan reads every
tracked text file and records, per file, the line numbers and kinds of credential-like
content: a known token format, a secret-like name assigned a literal value, credentials in
a URL, a password hash. It never records the values, and it is a hint, not a finding:
`list_files` shows it as a tag and the agent decides after reading the file.

In a run on leaky-repo (2026-10-24), names alone pointed at 15 of its 42 secret files;
names plus this scan pointed at 38, and nothing was flagged on pypa/sampleproject.
"""

from __future__ import annotations

import re

from auditor.redact import _TOKEN_PATTERNS

_SECRET_KEY = (
    r"[\w.-]*(?:password|passwd|pwd|pass|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"auth(?:[_-]?token|[_-]?key|orization)?\b|credential)[\w.-]*"
)
# (label, pattern); each pattern has `key` and `value` groups, and `quote` where a value may be bare.
_VALUE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "assigned",
        re.compile(
            rf"""(?ix)["']?\b(?P<key>{_SECRET_KEY})["']?\s*(?:=>|:=|[:=])\s*(?P<quote>["']?)(?P<value>[^\s"',;<>)]{{3,}})"""
        ),
    ),
    ("assigned", re.compile(rf"""(?i)^\s*(?P<key>{_SECRET_KEY})\s+(?P<quote>["'])(?P<value>[^"']{{3,}})["']""")),
    (
        "assigned",
        re.compile(
            rf"""(?i)define\(\s*["'](?P<key>{_SECRET_KEY})["']\s*,\s*(?P<quote>["'])(?P<value>[^"']{{3,}})["']"""
        ),
    ),
    ("assigned", re.compile(rf"(?i)<(?P<key>{_SECRET_KEY})\b[^>]*>(?P<value>[^<]{{3,}})</")),
    ("assigned", re.compile(rf"""(?i)\b(?P<key>{_SECRET_KEY})\s*=\s*["'](?P<value>[^"']{{3,}})["']""")),
]
_URL_CREDENTIALS = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:(?P<value>[^\s@/]{3,})@[^\s/]+", re.IGNORECASE)
_HASH_LINE = re.compile(r"^[\w.-]+:\$(?:1|2[abxy]?|5|6|apr1|y)\$")
# Values that name or reference a secret rather than contain one.
_NOT_A_VALUE = re.compile(
    r"""(?ix)^(?:\$|%|\{\{|<|\[|os\.|env|process\.env|getenv|settings\.|config\.|self\.|none|null|nil|true|false|
    read|write|required|optional|string|str|int|bool|password|secret|token|xxx+|\*+|\.\.\.)"""
)
_CODE = (".py", ".js", ".ts", ".rb", ".php", ".java", ".go", ".cs", ".kt", ".swift", ".rs", ".scala")
_DOCS = (".md", ".rst", ".adoc", ".txt")
_TEST_DIRS = ("test/", "tests/", "spec/", "__tests__/", "testdata/", "fixtures/")


def is_test_path(path: str) -> bool:
    p = "/" + path.lower()
    return any(f"/{d}" in p for d in _TEST_DIRS) or p.rsplit("/", 1)[-1].startswith(("test_", "conftest"))


def credential_lines(path: str, text: str) -> list[int]:
    """Line numbers in ``text`` with credential-like content (empty for docs)."""
    lower = path.lower()
    if lower.endswith(_DOCS):
        return []  # examples in documentation are not committed credentials
    is_code = lower.endswith(_CODE)
    lines = []
    for n, line in enumerate(text.splitlines(), 1):
        if any(p.search(line) for _, p in _TOKEN_PATTERNS) or _HASH_LINE.search(line):
            lines.append(n)
            continue
        m = _URL_CREDENTIALS.search(line)
        if m and not _NOT_A_VALUE.match(m["value"]):
            lines.append(n)
            continue
        for _, pattern in _VALUE_PATTERNS:
            for m in pattern.finditer(line):
                value = m["value"].strip()
                groups = m.groupdict()
                quoted = "quote" not in groups or bool(groups["quote"])
                # In code, only a quoted literal is a committed value: a bare name is a variable and
                # `name: list[bytes] = ...` is a type annotation.
                if _NOT_A_VALUE.match(value) or (is_code and not quoted):
                    continue
                lines.append(n)
                break
            if lines and lines[-1] == n:
                break
    return lines
