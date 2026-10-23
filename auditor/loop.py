"""The agent loop: observe -> reason -> select tool -> execute -> interpret -> report.

The LLM chooses every inspection step. This module only dispatches the tool
calls it asks for, feeds results back, and collects recorded violations.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Protocol

import tools as inspection
from auditor.prompts import AGENT_TOOL_SCHEMAS, SYSTEM_PROMPT
from auditor.redact import redact, redact_obj
from auditor.report import ViolationError, dedupe_key, next_rule_id, same_problem, validate_violation

log = logging.getLogger(__name__)

MAX_NUDGES = 2
MAX_UNREAD_LISTED = 25


def trace_preview(tool: str, output: str, is_error: bool) -> str:
    """What the trace keeps of a tool's output. File contents are never stored.

    Pattern redaction cannot recognise every secret format, so read_file keeps
    only its header line and search_code only its file:line locations.
    """
    if not is_error and tool == "read_file":
        return redact(output.split("\n", 1)[0]) + " [contents omitted]"
    if not is_error and tool == "search_code":
        locations = [":".join(line.split(":", 2)[:2]) for line in output.splitlines() if line.count(":") >= 2]
        return redact(", ".join(locations)[:500] if locations else output.split("\n", 1)[0])
    return redact(output[:500])


class LLM(Protocol):
    def create(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Any: ...


class AgentError(Exception):
    """The agent could not complete the audit."""


class AuditAgent:
    def __init__(
        self,
        llm: LLM,
        repo_tools: inspection.RepoTools,
        max_turns: int = 40,
        trace_path: Path | None = None,
    ):
        self.llm = llm
        self.repo_tools = repo_tools
        self.max_turns = max_turns
        self.trace_path = Path(trace_path) if trace_path else None
        self.tool_schemas = inspection.TOOL_SCHEMAS + AGENT_TOOL_SCHEMAS
        self.violations: list[dict[str, Any]] = []
        self.finished = False
        self.summary: str | None = None
        self.turn = 0
        self.read_paths: set[str] = set()
        self.coverage_checked = False

    # ------------------------------------------------------------------ run

    def run(self, repository: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"Audit the repository {repository} against the nine DevOps inspection areas. "
                    "It is cloned locally and available through your tools. "
                    f"You have at most {self.max_turns} turns."
                ),
            }
        ]
        nudges = 0
        for turn in range(1, self.max_turns + 1):
            self.turn = turn
            response = self.llm.create(SYSTEM_PROMPT, self.tool_schemas, messages)
            messages.append({"role": "assistant", "content": response.content})
            self._log_reasoning(response)

            if response.stop_reason == "refusal":
                raise AgentError("The model declined to continue the audit (refusal)")

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                if self.finished or nudges >= MAX_NUDGES:
                    break
                nudges += 1
                messages.append(
                    {"role": "user", "content": "Continue the investigation, or call finish_audit if you are done."}
                )
                continue

            results = [self._execute(block) for block in tool_uses]
            remaining = self.max_turns - self.turn
            if not self.finished and remaining <= 3:
                results.append(
                    {
                        "type": "text",
                        "text": (f"{remaining} turn(s) left. Record remaining violations and call finish_audit."),
                    }
                )
            messages.append({"role": "user", "content": results})
            if self.finished:
                break
        else:
            log.warning("Turn limit (%d) reached before finish_audit; reporting what was recorded", self.max_turns)

        return self.violations

    # ------------------------------------------------------------ dispatch

    def _execute(self, block: Any) -> dict[str, Any]:
        name, args = block.name, dict(block.input or {})
        started = time.monotonic()
        is_error = False
        try:
            if name == "record_violation":
                output = self._record(args)
            elif name == "finish_audit":
                output = self._finish(args)
            else:
                output = inspection.dispatch(self.repo_tools, name, args)
                if name == "read_file":
                    self.read_paths.add(self.repo_tools.relpath(args["path"]))
        except (inspection.ToolError, ViolationError) as e:
            output, is_error = f"Error: {e}", True
        except Exception as e:  # a tool bug must not kill the audit
            log.exception("Tool %s crashed", name)
            output, is_error = f"Error: tool {name} failed unexpectedly ({type(e).__name__})", True

        try:
            self._trace(name, args, output, is_error, time.monotonic() - started)
        except Exception:  # logging must never end an audit
            log.exception("Could not write trace entry for %s", name)
        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": output}
        if is_error:
            result["is_error"] = True
        return result

    def _finish(self, args: dict[str, Any]) -> str:
        """End the audit, unless workflows or sensitive-looking files were never opened.

        The check fires once: a second finish_audit always ends the run, so the
        model keeps the final say and the loop cannot get stuck.
        """
        unread = [f for f in self.repo_tools.must_read_files() if f not in self.read_paths]
        enough_turns = self.max_turns - self.turn >= 2
        if unread and enough_turns and not self.coverage_checked:
            self.coverage_checked = True
            listed = ", ".join(unread[:MAX_UNREAD_LISTED])
            more = f" and {len(unread) - MAX_UNREAD_LISTED} more" if len(unread) > MAX_UNREAD_LISTED else ""
            raise inspection.ToolError(
                f"Not finished: you have not opened these workflow or sensitive-looking files: {listed}{more}. "
                "Read them (several per turn), record any violations they contain, then call finish_audit again."
            )
        self.finished = True
        self.summary = str(args.get("summary", ""))
        return f"Audit finished with {len(self.violations)} violation(s)."

    def _record(self, args: dict[str, Any]) -> str:
        v = validate_violation(args)
        key = dedupe_key(v)
        if any(dedupe_key(existing) == key for existing in self.violations):
            raise ViolationError(f"duplicate of an already recorded violation {key}")
        if v["file"] is not None and v["line"] is not None:
            for existing in self.violations:
                if (existing["file"], existing["line"]) == (v["file"], v["line"]):
                    raise ViolationError(
                        f"{existing['rule_id']} is already recorded at {v['file']}:{v['line']}. If this is the same "
                        "problem it is already covered; if it is a different problem, record it at the line where "
                        "that problem occurs."
                    )
        for existing in self.violations:
            if same_problem(existing, v):
                raise ViolationError(
                    f"the same problem is already recorded as {existing['rule_id']} ({existing['description']!r}); "
                    "do not record it again."
                )
        if any(existing["rule_id"] == v["rule_id"] for existing in self.violations):
            prefix = v["rule_id"].split("-")[0]
            raise ViolationError(
                f"rule_id {v['rule_id']} is already used by another violation; give this one "
                f"{next_rule_id(prefix, self.violations)}."
            )
        self.violations.append(v)
        return f"Recorded violation #{len(self.violations)} ({v['rule_id']})."

    # ------------------------------------------------------------- logging

    def _log_reasoning(self, response: Any) -> None:
        for b in response.content:
            if b.type == "text" and b.text.strip():
                log.info("[turn %d] model: %s", self.turn, redact(b.text.strip())[:500])

    def _trace(self, name: str, args: dict[str, Any], output: str, is_error: bool, seconds: float) -> None:
        safe_args = redact_obj(args)
        log.info("[turn %d] %s(%s)%s", self.turn, name, json.dumps(safe_args)[:200], " -> error" if is_error else "")
        if not self.trace_path:
            return
        entry = {
            "turn": self.turn,
            "tool": name,
            "input": safe_args,
            "is_error": is_error,
            "output_chars": len(output),
            "output_preview": trace_preview(name, output, is_error),
            "seconds": round(seconds, 3),
        }
        with self.trace_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
