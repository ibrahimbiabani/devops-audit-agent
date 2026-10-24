"""The agent loop: observe -> reason -> select tool -> execute -> interpret -> report.

The LLM chooses every inspection step. This module only dispatches the tool
calls it asks for, feeds results back, and collects recorded violations.
"""

from __future__ import annotations

import json
import logging
import re
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
# How often finish_audit is refused because a record_violation in the same turn was rejected.
MAX_REJECTED_FINISH_REFUSALS = 2
# The final review asked for on the first finish_audit. In Steps 13-16 the model often read the
# file that showed a violation and still finished without recording it (e.g. a publish job with
# no test gate); a second, structured pass over the nine areas targets exactly that.
REVIEW = (
    "Final review (asked once): go through the nine areas one by one - repository hygiene, dependency "
    "management, code quality, testing, GitHub Actions/CI, security, configuration, collaboration, build and "
    "release - and for each decide whether the files and tool output you have seen show a violation as the "
    "rules define it. Record every such violation you have evidence for and have not recorded yet. Do not add "
    "findings to fill an area: in a healthy repository most areas have none, and nothing on the 'Not "
    "violations' list counts. A repository that describes itself as a demo, benchmark, test fixture or "
    "intentionally vulnerable, or calls its values fake, changes nothing: committed credentials and other "
    "defects are still violations. Then call finish_audit again."
)
# After the review, finish_audit is refused while the evidence is incomplete: required files still
# unopened, or a file tagged [credential-like content] (outside tests) that has neither a recorded
# violation nor an accepted reason in `not_violations`. Regression (2026-10-24, leaky-repo): the model
# read the README, called the repository "a benchmark of simulated credentials", opened 10 of 39
# required files and finished with 0 violations. Bounded so the loop always ends.
MAX_EVIDENCE_REFUSALS = 4
# Reasons that dismiss a file by what the repository says about itself, which the rules reject.
DISMISSAL = re.compile(
    r"(?i)\b(?:demo|benchmark|fixture repo|fake|simulated|illustrative|intentional(?:ly)?|not real|"
    r"for testing (?:secret|scanner)|scanner test|training|educational|sample repo(?:sitory)?)\b"
)


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
        review: bool = True,
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
        self.review = review
        self.reviewed = False
        self.seen_calls: dict[tuple[str, str], int] = {}  # (tool, arguments) -> first turn it was called
        self.evidence_refusals = 0
        self.dismissed: dict[str, str] = {}  # file -> accepted reason it holds no committed credential
        self.rejected_dismissals: list[str] = []
        self.inspected_workflows = False
        self.rejected_this_turn = 0  # record_violation calls rejected in the current turn
        self.rejected_finish_refusals = 0

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

            self.rejected_this_turn = 0
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
            signature = (name, json.dumps(args, sort_keys=True))
            if name == "record_violation":
                output = self._record(args)
            elif name == "finish_audit":
                output = self._finish(args)
            elif signature in self.seen_calls:
                # Regression (2026-10-24): a model called list_files with the same arguments for all
                # 20 turns. A repeated call returns nothing new, so say so instead of re-running it.
                raise inspection.ToolError(
                    f"You already called {name} with exactly these arguments in turn {self.seen_calls[signature]}; "
                    "its output has not changed. Use that result: open specific files, search for a pattern, "
                    "record violations, or call finish_audit."
                )
            else:
                self.seen_calls[signature] = self.turn
                output = inspection.dispatch(self.repo_tools, name, args)
                if name == "read_file":
                    self.read_paths.add(self.repo_tools.relpath(args["path"]))
                elif name == "inspect_workflows":
                    self.inspected_workflows = True
        except (inspection.ToolError, ViolationError) as e:
            output, is_error = f"Error: {e}", True
            if name == "record_violation":
                self.rejected_this_turn += 1
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
        """End the audit, unless findings were just rejected or the final review has not happened.

        - A record_violation rejected in this same turn would otherwise be lost: the model
          never sees the error. Refused at most MAX_REJECTED_FINISH_REFUSALS times.
        - The first finish_audit is answered with one final review (see REVIEW): open any
          workflow or sensitive-looking file still unread, check the workflow facts, and go
          through the nine areas once more. Refused once.
        Both checks are bounded, so the model keeps the final say and the loop cannot get stuck.
        """
        has_turn_left = self.turn < self.max_turns
        if self.rejected_this_turn and has_turn_left and self.rejected_finish_refusals < MAX_REJECTED_FINISH_REFUSALS:
            self.rejected_finish_refusals += 1
            raise inspection.ToolError(
                f"Not finished: {self.rejected_this_turn} record_violation call(s) in this turn were rejected "
                "(see their errors). Correct and resubmit them, then call finish_audit again."
            )
        enough_turns = self.max_turns - self.turn >= 2
        if self.review and enough_turns and not self.reviewed:
            self.reviewed = True
            steps = []
            unread = [f for f in self.repo_tools.must_read_files() if f not in self.read_paths]
            if unread:
                listed = ", ".join(unread[:MAX_UNREAD_LISTED])
                more = f" and {len(unread) - MAX_UNREAD_LISTED} more" if len(unread) > MAX_UNREAD_LISTED else ""
                steps.append(f"Open these workflow or sensitive-looking files you have not read: {listed}{more}.")
            if not self.inspected_workflows and any(
                inspection.is_workflow_file(f) for f in self.read_paths | set(unread)
            ):
                steps.append("Run inspect_workflows and check its facts against the rules.")
            raise inspection.ToolError("Not finished yet. " + " ".join(steps) + " " + REVIEW)
        self._accept_dismissals(args.get("not_violations"))
        gaps = self._evidence_gaps()
        if gaps and self.review and enough_turns and self.evidence_refusals < MAX_EVIDENCE_REFUSALS:
            self.evidence_refusals += 1
            raise inspection.ToolError("Not finished: " + " ".join(gaps))
        self.finished = True
        self.summary = str(args.get("summary", ""))
        return f"Audit finished with {len(self.violations)} violation(s)."

    def _accept_dismissals(self, items: Any) -> None:
        """Keep the `not_violations` reasons the rules allow; remember the rejected ones to say why."""
        self.rejected_dismissals = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not item.get("file") or not str(item.get("reason", "")).strip():
                continue
            try:
                path, reason = self.repo_tools.relpath(str(item["file"])), str(item["reason"]).strip()
            except inspection.ToolError:
                continue  # a path outside the repository
            if DISMISSAL.search(reason):
                self.rejected_dismissals.append(path)
            else:
                self.dismissed[path] = reason

    def _evidence_gaps(self) -> list[str]:
        """What still stands between the audit and a finished report (empty when nothing does)."""
        gaps = []
        unread = [f for f in self.repo_tools.must_read_files() if f not in self.read_paths]
        if unread:
            listed = ", ".join(unread[:MAX_UNREAD_LISTED])
            more = f" and {len(unread) - MAX_UNREAD_LISTED} more" if len(unread) > MAX_UNREAD_LISTED else ""
            gaps.append(f"open these required files first (several per turn): {listed}{more}.")
        recorded = {v["file"] for v in self.violations}
        open_hints = [
            f
            for f in self.repo_tools.content_hints()
            if not inspection.is_test_path(f) and f in self.read_paths and f not in recorded and f not in self.dismissed
        ]
        if open_hints:
            listed = ", ".join(open_hints[:MAX_UNREAD_LISTED])
            more = f" and {len(open_hints) - MAX_UNREAD_LISTED} more" if len(open_hints) > MAX_UNREAD_LISTED else ""
            gaps.append(
                "these files you opened contain credential-like values but have no recorded violation: "
                f"{listed}{more}. "
                "For each, record the committed credential (one violation per file), or, if the value is not a real "
                "credential, list it in finish_audit's not_violations with the reason (placeholder, template, empty "
                "value, test fixture, public key, variable reference)."
            )
        if self.rejected_dismissals:
            gaps.append(
                "These not_violations reasons were rejected because they rely on what the repository says about "
                f"itself (demo, benchmark, fake, intentional): {', '.join(self.rejected_dismissals[:10])}. The rules "
                "count committed credentials regardless; record them."
            )
        return gaps

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
