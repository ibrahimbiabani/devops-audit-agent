"""Secret redaction.

Two strengths:
- ``redact_tokens`` masks high-confidence credential formats (cloud keys, tokens,
  private keys). It is applied to every tool output before the LLM sees it, so
  real credentials never leave the machine. The mask names the kind of secret,
  so the agent can still report it.
- ``redact`` additionally masks values in generic ``password = "..."``-style
  assignments. It is applied to everything we write (report, trace, logs).
"""

from __future__ import annotations

import re

_TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")),
    ("putty_private_key", re.compile(r"PuTTY-User-Key-File-\d+:[\s\S]*?(?:Private-MAC:[^\n]*|\Z)")),
    ("password_hash", re.compile(r"\$(?:1|2[abxy]?|5|6|apr1|y)\$[./A-Za-z0-9$]{8,}")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("api_secret_key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]

_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<key>[\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|auth|credential)[\w.-]*)
    (?P<sep>\s*[:=]\s*)
    (?P<quote>['"]?)
    (?P<value>[^'"\s,;]{4,})
    (?P=quote)
    """
)


def redact_tokens(text: str) -> str:
    for name, pattern in _TOKEN_PATTERNS:
        text = pattern.sub(f"[REDACTED:{name}]", text)
    return text


def redact(text: str) -> str:
    text = redact_tokens(text)

    def _mask(m: re.Match[str]) -> str:
        if m.group("value").startswith("[REDACTED"):
            return m.group(0)
        q = m.group("quote")
        return f"{m.group('key')}{m.group('sep')}{q}[REDACTED]{q}"

    return _ASSIGNMENT.sub(_mask, text)


def redact_obj(obj):
    """Apply ``redact`` to every string inside a JSON-like structure.

    Redacting a serialized JSON string instead can break its quoting.
    """
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact_obj(v) for v in obj]
    return obj
