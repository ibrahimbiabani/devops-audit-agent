"""Read-only inspection tools the agent can call on a cloned repository.

Every tool returns plain text for the LLM. Failures raise ``ToolError`` so the
agent loop can hand them back as ``is_error`` tool results instead of crashing.
No tool writes to the repository or executes code from it.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Any

from auditor.content_hints import credential_lines, is_test_path
from auditor.redact import redact_tokens
from auditor.repo import git_env
from auditor.workflows import summarize

MAX_OUTPUT_CHARS = 20_000
MAX_FILE_BYTES = 1_000_000
MAX_LINE_CHARS = 2_000

# File names that usually hold credentials or private keys. A match is only a
# hint for the agent to open the file; it is never reported on its own.
_SENSITIVE_NAMES = {
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".htpasswd",
    ".dockercfg",
    ".s3cfg",
    ".boto",
    "credentials",
    "credentials.json",
    "credentials.yml",
    "credentials.yaml",
    "secrets.json",
    "secrets.yml",
    "secrets.yaml",
    "shadow",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".ppk", ".jks", ".keystore", ".kdbx", ".tfstate")
_SENSITIVE_PATHS = (".docker/config.json", ".aws/credentials", ".kube/config")
_TEMPLATE_MARKERS = (".example", ".sample", ".template", ".dist", ".tmpl")
WORKFLOW_DIR = ".github/workflows/"


def is_sensitive_name(path: str) -> bool:
    """True for paths whose name suggests credentials or keys (templates excluded)."""
    path = path.replace("\\", "/")
    lower = path.lower()
    name = lower.rsplit("/", 1)[-1]
    if any(marker in name for marker in _TEMPLATE_MARKERS) or lower.startswith("docs/"):
        return False
    return (
        name in _SENSITIVE_NAMES
        or name.startswith(".env.")
        or name.endswith(_SENSITIVE_SUFFIXES)
        or any(lower == p or lower.endswith("/" + p) for p in _SENSITIVE_PATHS)
    )


def is_workflow_file(path: str) -> bool:
    """True for workflow files GitHub actually loads (direct children of .github/workflows)."""
    if not path.startswith(WORKFLOW_DIR):
        return False
    rest = path[len(WORKFLOW_DIR) :]
    return "/" not in rest and rest.endswith((".yml", ".yaml"))


class ToolError(Exception):
    """A tool call failed in a way the agent should be told about."""


class RepoTools:
    def __init__(self, root: Path, max_output_chars: int = MAX_OUTPUT_CHARS):
        self.root = Path(root).resolve()
        self.max_output_chars = max_output_chars
        self._content_hints: dict[str, list[int]] | None = None
        if not self.root.is_dir():
            raise ToolError(f"Repository root does not exist: {self.root}")

    # ---------------------------------------------------------------- helpers

    def _git(self, *args: str, timeout: int = 60) -> str:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=git_env(),
            )
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"git {args[0]} timed out") from e
        if proc.returncode != 0:
            raise ToolError(f"git {args[0]} failed: {proc.stderr.strip()}")
        return proc.stdout

    def _resolve(self, path: str) -> Path:
        """Resolve a repo-relative path, refusing anything outside the repo."""
        candidate = (self.root / path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ToolError(f"Path escapes the repository: {path}")
        return candidate

    def _tracked_files(self) -> list[str]:
        try:
            out = self._git("ls-files", "-z")
            return sorted(p for p in out.split("\0") if p)
        except ToolError:
            # Not a git checkout: fall back to walking the tree.
            return sorted(
                p.relative_to(self.root).as_posix()
                for p in self.root.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(self.root).parts
            )

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_output_chars:
            return text
        return text[: self.max_output_chars] + f"\n... [truncated, {len(text) - self.max_output_chars} more chars]"

    def _finish(self, text: str) -> str:
        return self._truncate(redact_tokens(text))

    def relpath(self, path: str) -> str:
        """Canonical repo-relative POSIX form of ``path`` (for tracking what was read)."""
        return self._resolve(path).relative_to(self.root).as_posix()

    def must_read_files(self) -> list[str]:
        """Tracked files the agent should open before finishing: workflows, sensitive names, and files
        with credential-like content (test files excepted: their values are usually fixtures)."""
        hints = self.content_hints()
        return [
            f
            for f in self._tracked_files()
            if is_workflow_file(f) or is_sensitive_name(f) or (f in hints and not is_test_path(f))
        ]

    def content_hints(self) -> dict[str, list[int]]:
        """Tracked files with credential-like content -> line numbers. Scanned once per audit."""
        if self._content_hints is None:
            hints: dict[str, list[int]] = {}
            for rel in self._tracked_files():
                lower = rel.lower()
                if any(marker in lower.rsplit("/", 1)[-1] for marker in _TEMPLATE_MARKERS) or lower.startswith("docs/"):
                    continue  # templates and docs hold placeholders by design
                p = self.root / rel
                try:
                    if not p.is_file() or p.is_symlink() or p.stat().st_size > MAX_FILE_BYTES:
                        continue
                    data = p.read_bytes()
                except OSError:
                    continue
                if b"\0" in data[:8192]:
                    continue
                lines = credential_lines(rel, data.decode("utf-8", errors="replace"))
                if lines:
                    hints[rel] = lines
            self._content_hints = hints
        return self._content_hints

    def _content_tag(self, path: str) -> str:
        lines = self.content_hints().get(path)
        if not lines:
            return ""
        shown = ", ".join(map(str, lines[:5])) + (f" +{len(lines) - 5} more" if len(lines) > 5 else "")
        where = " in a test file" if is_test_path(path) else ""
        return f" [credential-like content{where}: line{'s' if len(lines) > 1 else ''} {shown}]"

    # ------------------------------------------------------------------ tools

    def list_files(self, subdir: str = "", pattern: str | None = None) -> str:
        """List git-tracked files, optionally under ``subdir`` and/or matching a glob."""
        prefix = subdir.strip("/").replace("\\", "/")
        if prefix:
            self._resolve(prefix)
        files = self._tracked_files()
        if prefix:
            files = [f for f in files if f == prefix or f.startswith(prefix + "/")]
        if pattern:
            files = [f for f in files if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(Path(f).name, pattern)]
        if not files:
            return "No tracked files match."
        lines = []
        for f in files:
            p = self.root / f
            size = p.stat().st_size if p.is_file() else 0
            link = " -> symlink" if p.is_symlink() else ""
            tag = (" [sensitive name]" if is_sensitive_name(f) else "") + self._content_tag(f)
            lines.append(f"{f} ({size} B){link}{tag}")
        return self._finish(f"{len(files)} tracked file(s):\n" + "\n".join(lines))

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """Return a file's contents with 1-based line numbers."""
        p = self._resolve(path)
        if not p.is_file():
            raise ToolError(f"File not found: {path}")
        if p.stat().st_size > MAX_FILE_BYTES:
            raise ToolError(f"File too large to read ({p.stat().st_size} B); use search_code instead")
        data = p.read_bytes()
        if b"\0" in data[:8192]:
            return f"{path} looks like a binary file ({len(data)} B); contents not shown."
        lines = data.decode("utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        end = len(lines) if end_line is None else min(end_line, len(lines))
        body = "\n".join(f"{i:>5}| {lines[i - 1][:MAX_LINE_CHARS]}" for i in range(start, end + 1))
        header = f"{path} (lines {start}-{end} of {len(lines)})"
        return self._finish(f"{header}\n{body}" if body else f"{header}\n[empty]")

    def search_code(
        self, pattern: str, path_glob: str | None = None, ignore_case: bool = False, max_results: int = 100
    ) -> str:
        """Regex search across tracked text files. Returns ``file:line: text`` matches."""
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            raise ToolError(f"Invalid regular expression: {e}") from e
        max_results = max(1, min(int(max_results), 500))
        hits: list[str] = []
        for rel in self._tracked_files():
            if path_glob and not (fnmatch.fnmatch(rel, path_glob) or fnmatch.fnmatch(Path(rel).name, path_glob)):
                continue
            p = self.root / rel
            try:
                if not p.is_file() or p.is_symlink() or p.stat().st_size > MAX_FILE_BYTES:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if regex.search(line[:MAX_LINE_CHARS]):
                    hits.append(f"{rel}:{n}: {line.strip()[:300]}")
                    if len(hits) >= max_results:
                        return self._finish("\n".join(hits) + f"\n[stopped at {max_results} matches]")
        return self._finish("\n".join(hits) if hits else "No matches.")

    def git_log(self, max_count: int = 30, path: str | None = None) -> str:
        """Recent commits with changed files (name-status)."""
        max_count = max(1, min(int(max_count), 200))
        args = ["log", f"-n{max_count}", "--no-renames", "--date=short", "--name-status"]
        args.append("--pretty=format:%n%h %ad %an%n    %s")
        if path:
            self._resolve(path)
            args += ["--", path]
        out = self._git(*args).strip()
        return self._finish(out or "No commits.")

    def inspect_workflows(self, path: str | None = None) -> str:
        """Structured facts about GitHub Actions workflows (all, or the one at ``path``)."""
        tracked = self._tracked_files()
        workflows = [f for f in tracked if is_workflow_file(f)]
        if path:
            rel = self.relpath(path)
            if rel not in workflows:
                raise ToolError(f"Not a workflow file GitHub runs: {path} (only files directly in {WORKFLOW_DIR})")
            workflows = [rel]
        if not workflows:
            return "No workflow files directly in .github/workflows/."
        parts = []
        for rel in workflows:
            p = self.root / rel
            if p.stat().st_size > MAX_FILE_BYTES:
                parts.append(f"{rel}\n  too large to inspect")
                continue
            parts.append(summarize(rel, p.read_bytes().decode("utf-8", errors="replace")))
        ignored = [
            f
            for f in tracked
            if f.startswith(WORKFLOW_DIR) and f.endswith((".yml", ".yaml")) and not is_workflow_file(f)
        ]
        if ignored and not path:
            parts.append(
                f"Not run by GitHub (in subfolders of {WORKFLOW_DIR}, which GitHub ignores): {', '.join(ignored)}"
            )
        return self._finish("\n\n".join(parts))


# Tool schemas exposed to the LLM. Descriptions say what the tool is good for,
# so the model can choose between them.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "list_files",
        "description": (
            "List files tracked by git in the repository, with sizes. Use it first to learn the layout, "
            "and later to check whether specific files (lockfiles, workflows, tests, .env files) are tracked. "
            "Files whose names suggest credentials or keys are tagged [sensitive name], and files whose "
            "contents look like a committed credential are tagged [credential-like content: lines ...] "
            "(values never shown). Tags are hints: open the file with read_file and judge it yourself. "
            "Optional `subdir` limits to a directory; optional `pattern` is a glob "
            "like '*.yml' or '.github/workflows/*'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subdir": {"type": "string", "description": "Repo-relative directory, e.g. 'src'."},
                "pattern": {"type": "string", "description": "Glob matched against path or file name."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a tracked file with 1-based line numbers. Use line numbers from this output as evidence. "
            "For long files, pass start_line/end_line."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repo-relative file path."},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_code",
        "description": (
            "Search tracked text files with a Python regular expression. Returns 'file:line: text'. "
            "Use for suspicious patterns: credentials, machine-specific paths, risky workflow settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regex."},
                "path_glob": {"type": "string", "description": "Only search files matching this glob."},
                "ignore_case": {"type": "boolean"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "git_log",
        "description": (
            "Show recent commits (hash, date, author, subject) and the files each one changed. "
            "Use to spot committed-then-deleted secrets or generated files, and to judge history hygiene."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_count": {"type": "integer", "minimum": 1, "maximum": 200},
                "path": {"type": "string", "description": "Limit history to this path."},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "inspect_workflows",
        "description": (
            "Structured facts about the GitHub Actions workflows, with line numbers: triggers, workflow- and "
            "job-level permissions, each job's runs-on and needs, which steps publish/deploy and which run tests, "
            "and signals such as untrusted ${{ github.event.* }} or inputs.* expressions, secrets used, set -x, "
            "verbose curl, remote scripts piped to a shell, literal values under secret-like names, contains() "
            "actor checks and artifact uploads. It reports facts only; you decide what is a violation. Use it "
            "alongside read_file on each workflow. Optional `path` inspects one workflow."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "One workflow file, repo-relative."}},
            "additionalProperties": False,
        },
    },
]


def dispatch(tools: RepoTools, name: str, args: dict[str, Any]) -> str:
    """Run the named inspection tool. Raises ToolError for unknown tools or bad arguments."""
    allowed = {s["name"] for s in TOOL_SCHEMAS}
    if name not in allowed:
        raise ToolError(f"Unknown tool: {name}")
    try:
        return getattr(tools, name)(**args)
    except TypeError as e:
        raise ToolError(f"Bad arguments for {name}: {e}") from e
