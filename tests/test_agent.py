"""Agent-loop tests with a scripted fake LLM (no network)."""

import json
from types import SimpleNamespace as NS

import pytest

from auditor.loop import AgentError, AuditAgent
from tools import RepoTools


def tool_use(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def text(t):
    return NS(type="text", text=t)


def resp(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop)


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, system, tools, messages):
        self.calls.append([dict(m) for m in messages])
        return self.script.pop(0)


VIOLATION = {
    "rule_id": "CI-001",
    "category": "ci_cd",
    "severity": "high",
    "file": ".github/workflows/ci.yml",
    "line": 1,
    "description": "Workflow uses pull_request_target",
    "evidence": "on: pull_request_target",
    "recommendation": "Use pull_request unless privileged context is required",
}


def tool_results(messages):
    return [b for b in messages[-1]["content"] if b.get("type") == "tool_result"]


def test_agent_follows_llm_tool_choices(sample_repo, tmp_path):
    llm = FakeLLM(
        [
            resp(text("Look around first."), tool_use("t1", "list_files")),
            resp(tool_use("t2", "read_file", path=".github/workflows/ci.yml")),
            resp(tool_use("t3", "record_violation", **VIOLATION)),
            resp(tool_use("t4", "finish_audit", summary="done")),
        ]
    )
    trace = tmp_path / "trace.jsonl"
    agent = AuditAgent(llm, RepoTools(sample_repo), review=False, trace_path=trace)
    violations = agent.run("https://github.com/a/b")

    assert violations == [VIOLATION]
    assert agent.finished and agent.summary == "done"
    # Each turn's tool output is fed back with the matching tool_use_id.
    second_call_results = tool_results(llm.calls[1])
    assert second_call_results[0]["tool_use_id"] == "t1"
    assert "ci.yml" in second_call_results[0]["content"]
    assert "pull_request_target" in tool_results(llm.calls[2])[0]["content"]
    traced = [json.loads(line)["tool"] for line in trace.read_text().splitlines()]
    assert traced == ["list_files", "read_file", "record_violation", "finish_audit"]


def test_parallel_tool_calls_return_in_one_message(sample_repo):
    llm = FakeLLM(
        [
            resp(tool_use("a", "list_files"), tool_use("b", "git_log", max_count=5)),
            resp(tool_use("c", "finish_audit", summary="x")),
            resp(tool_use("d", "finish_audit", summary="x")),
        ]
    )
    AuditAgent(llm, RepoTools(sample_repo)).run("https://github.com/a/b")
    assert [r["tool_use_id"] for r in tool_results(llm.calls[1])] == ["a", "b"]


def test_tool_errors_are_reported_back_not_raised(sample_repo):
    llm = FakeLLM(
        [
            resp(tool_use("t1", "read_file", path="../../etc/passwd"), tool_use("t2", "search_code", pattern="(")),
            resp(tool_use("t3", "record_violation", **{**VIOLATION, "severity": "urgent"})),
            resp(tool_use("t4", "finish_audit", summary="x")),
            resp(tool_use("t5", "finish_audit", summary="x")),
        ]
    )
    agent = AuditAgent(llm, RepoTools(sample_repo))
    assert agent.run("https://github.com/a/b") == []
    assert all(r["is_error"] for r in tool_results(llm.calls[1]))
    assert tool_results(llm.calls[2])[0]["is_error"]


def test_duplicate_violation_rejected(sample_repo):
    llm = FakeLLM(
        [
            resp(tool_use("t1", "record_violation", **VIOLATION), tool_use("t2", "record_violation", **VIOLATION)),
            resp(tool_use("t3", "finish_audit", summary="x")),
            resp(tool_use("t4", "finish_audit", summary="x")),
        ]
    )
    agent = AuditAgent(llm, RepoTools(sample_repo))
    assert len(agent.run("https://github.com/a/b")) == 1
    assert "duplicate" in tool_results(llm.calls[1])[1]["content"]


def test_nudges_then_stops_when_model_never_finishes(sample_repo):
    llm = FakeLLM([resp(text("thinking"), stop="end_turn")] * 3)
    agent = AuditAgent(llm, RepoTools(sample_repo))
    assert agent.run("https://github.com/a/b") == []
    assert len(llm.calls) == 3


def test_turn_limit_keeps_recorded_violations(sample_repo):
    llm = FakeLLM(
        [resp(tool_use("t1", "record_violation", **VIOLATION))]
        + [resp(tool_use(f"x{i}", "list_files")) for i in range(5)]
    )
    agent = AuditAgent(llm, RepoTools(sample_repo), max_turns=3)
    assert agent.run("https://github.com/a/b") == [VIOLATION]
    assert len(llm.calls) == 3
    # Low-budget warning is appended after the tool results.
    assert llm.calls[2][-1]["content"][-1]["type"] == "text"


def test_refusal_raises(sample_repo):
    llm = FakeLLM([resp(stop="refusal")])
    with pytest.raises(AgentError):
        AuditAgent(llm, RepoTools(sample_repo)).run("https://github.com/a/b")


def test_same_location_rejected_even_with_new_rule_id(sample_repo):
    other = {**VIOLATION, "rule_id": "SEC-009", "category": "security", "description": "Another view of it"}
    llm = FakeLLM(
        [
            resp(tool_use("t1", "record_violation", **VIOLATION), tool_use("t2", "record_violation", **other)),
            resp(tool_use("t3", "read_file", path=".github/workflows/ci.yml")),
            resp(tool_use("t4", "finish_audit", summary="x")),
        ]
    )
    agent = AuditAgent(llm, RepoTools(sample_repo), review=False)
    assert [v["rule_id"] for v in agent.run("https://github.com/a/b")] == ["CI-001"]
    rejected = tool_results(llm.calls[1])[1]
    assert rejected["is_error"] and "already recorded at .github/workflows/ci.yml:1" in rejected["content"]


def record_both(sample_repo, first, second):
    """Record two violations in one turn; return (violations kept, the second call's result)."""
    llm = FakeLLM(
        [
            resp(tool_use("t1", "record_violation", **first), tool_use("t2", "record_violation", **second)),
            resp(tool_use("t3", "read_file", path=".github/workflows/ci.yml")),
            resp(tool_use("t4", "finish_audit", summary="x")),
        ]
    )
    kept = AuditAgent(llm, RepoTools(sample_repo), review=False).run("https://github.com/a/b")
    return kept, tool_results(llm.calls[1])[1]


def test_same_problem_at_repository_level_rejected(sample_repo):
    # Regression (Step 11, paastha1): "Missing .gitignore" recorded once with file null, once at .gitignore:1.
    hyg = {**VIOLATION, "rule_id": "HYG-001", "category": "repository_hygiene", "description": "Missing .gitignore."}
    kept, rejected = record_both(sample_repo, {**hyg, "file": None, "line": None}, {**hyg, "file": ".gitignore"})
    assert len(kept) == 1
    assert rejected["is_error"] and "same problem is already recorded as HYG-001" in rejected["content"]


def test_same_description_in_another_file_is_allowed(sample_repo):
    # Each committed credential is its own violation, even when described identically.
    key = {**VIOLATION, "category": "security", "description": "Committed private key", "line": None}
    kept, _ = record_both(
        sample_repo,
        {**key, "rule_id": "SEC-001", "file": ".ssh/id_rsa"},
        {**key, "rule_id": "SEC-002", "file": "k.pem"},
    )
    assert [v["file"] for v in kept] == [".ssh/id_rsa", "k.pem"]


def test_reused_rule_id_rejected_with_next_free_id(sample_repo):
    other = {**VIOLATION, "file": "app.py", "line": 3, "description": "Something else"}
    kept, rejected = record_both(sample_repo, VIOLATION, other)
    assert len(kept) == 1
    assert rejected["is_error"] and "rule_id CI-001 is already used" in rejected["content"]
    assert "give this one CI-002" in rejected["content"]


def test_finish_blocked_once_until_must_read_files_opened(sample_repo):
    llm = FakeLLM(
        [
            resp(tool_use("t1", "finish_audit", summary="early")),
            resp(tool_use("t2", "read_file", path="./.github/workflows/ci.yml")),
            resp(tool_use("t3", "finish_audit", summary="done")),
        ]
    )
    agent = AuditAgent(llm, RepoTools(sample_repo))
    agent.run("https://github.com/a/b")
    blocked = tool_results(llm.calls[1])[0]
    assert blocked["is_error"] and ".github/workflows/ci.yml" in blocked["content"]
    assert agent.finished and agent.summary == "done"
    assert len(llm.calls) == 3


def test_second_finish_always_ends_the_run(sample_repo):
    llm = FakeLLM([resp(tool_use(f"f{i}", "finish_audit", summary=str(i))) for i in range(2)])
    agent = AuditAgent(llm, RepoTools(sample_repo))
    agent.run("https://github.com/a/b")
    assert agent.finished and agent.summary == "1"


def test_coverage_check_skipped_on_last_turns(sample_repo):
    llm = FakeLLM([resp(tool_use("t1", "list_files")), resp(tool_use("t2", "finish_audit", summary="x"))])
    agent = AuditAgent(llm, RepoTools(sample_repo), max_turns=2)
    agent.run("https://github.com/a/b")
    assert agent.finished


def test_trace_survives_quoted_secrets_in_arguments(sample_repo, tmp_path):
    # Regression: redacting the serialized JSON broke its quoting and crashed the audit.
    tricky = {**VIOLATION, "evidence": 'env sets password="hunter22" and token: "abcd1234"'}
    llm = FakeLLM(
        [
            resp(
                tool_use("t1", "record_violation", **tricky),
                tool_use("t2", "read_file", path=".github/workflows/ci.yml"),
            ),
            resp(tool_use("t3", "finish_audit", summary="x")),
        ]
    )
    trace = tmp_path / "trace.jsonl"
    AuditAgent(llm, RepoTools(sample_repo), review=False, trace_path=trace).run("https://github.com/a/b")
    entries = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [e["tool"] for e in entries] == ["record_violation", "read_file", "finish_audit"]
    assert "hunter22" not in trace.read_text() and "abcd1234" not in trace.read_text()


def test_trace_failure_does_not_end_audit(sample_repo, tmp_path):
    llm = FakeLLM(
        [
            resp(
                tool_use("t1", "record_violation", **VIOLATION),
                tool_use("t1b", "read_file", path=".github/workflows/ci.yml"),
            ),
            resp(tool_use("t2", "finish_audit", summary="x")),
        ]
    )
    agent = AuditAgent(llm, RepoTools(sample_repo), review=False, trace_path=tmp_path / "missing-dir" / "trace.jsonl")
    assert agent.run("https://github.com/a/b") == [VIOLATION]


def test_trace_never_stores_file_contents(sample_repo, tmp_path):
    llm = FakeLLM(
        [
            resp(tool_use("t1", "read_file", path="app.py"), tool_use("t2", "search_code", pattern="DB_PATH")),
            resp(tool_use("t3", "read_file", path=".github/workflows/ci.yml")),
            resp(tool_use("t4", "finish_audit", summary="x")),
        ]
    )
    trace = tmp_path / "trace.jsonl"
    AuditAgent(llm, RepoTools(sample_repo), review=False, trace_path=trace).run("https://github.com/a/b")
    entries = {e["tool"] + e["input"].get("path", ""): e for e in map(json.loads, trace.read_text().splitlines())}
    assert entries["read_fileapp.py"]["output_preview"] == "app.py (lines 1-4 of 4) [contents omitted]"
    assert entries["search_code"]["output_preview"] == "app.py:3"
    assert "alice" not in trace.read_text()


def test_finish_refused_when_a_finding_was_rejected_in_the_same_turn(sample_repo):
    # Regression (Step 13, Grafter): garbled record_violation calls were rejected, and the
    # finish_audit sent alongside them ended the audit before they could be corrected.
    garbled = {**VIOLATION, "rule_id": "CI-001,severity:"}
    llm = FakeLLM(
        [
            resp(
                tool_use("t0", "read_file", path=".github/workflows/ci.yml"),
                tool_use("t1", "record_violation", **garbled),
                tool_use("t2", "finish_audit", summary="x"),
            ),
            resp(tool_use("t3", "record_violation", **VIOLATION), tool_use("t4", "finish_audit", summary="done")),
        ]
    )
    agent = AuditAgent(llm, RepoTools(sample_repo), review=False)
    assert agent.run("https://github.com/a/b") == [VIOLATION]
    refused = tool_results(llm.calls[1])[2]
    assert refused["is_error"] and "1 record_violation call(s) in this turn were rejected" in refused["content"]
    assert agent.finished and agent.summary == "done"


def test_rejected_finding_refusal_is_bounded(sample_repo):
    garbled = {**VIOLATION, "rule_id": "bad"}
    turn = [tool_use("t0", "read_file", path=".github/workflows/ci.yml"), tool_use("t1", "record_violation", **garbled)]
    llm = FakeLLM([resp(*turn, tool_use(f"f{i}", "finish_audit", summary=str(i))) for i in range(3)])
    agent = AuditAgent(llm, RepoTools(sample_repo), review=False)
    agent.run("https://github.com/a/b")
    assert agent.finished and agent.summary == "2"  # refused twice, then the model has the final say


def test_first_finish_asks_for_one_final_review(sample_repo):
    llm = FakeLLM(
        [
            resp(tool_use("t1", "finish_audit", summary="early")),
            resp(tool_use("t2", "read_file", path=".github/workflows/ci.yml"), tool_use("t3", "inspect_workflows")),
            resp(tool_use("t4", "finish_audit", summary="done")),
        ]
    )
    agent = AuditAgent(llm, RepoTools(sample_repo))
    agent.run("https://github.com/a/b")
    review = tool_results(llm.calls[1])[0]
    assert review["is_error"] and "Final review" in review["content"]
    assert ".github/workflows/ci.yml" in review["content"]  # unread workflow listed
    assert "Run inspect_workflows" in review["content"]
    assert agent.finished and agent.summary == "done" and len(llm.calls) == 3  # asked once only


def test_review_skips_reminders_already_done(sample_repo):
    llm = FakeLLM(
        [
            resp(tool_use("t1", "read_file", path=".github/workflows/ci.yml"), tool_use("t2", "inspect_workflows")),
            resp(tool_use("t3", "finish_audit", summary="x")),
            resp(tool_use("t4", "finish_audit", summary="y")),
        ]
    )
    agent = AuditAgent(llm, RepoTools(sample_repo))
    agent.run("https://github.com/a/b")
    review = tool_results(llm.calls[2])[0]["content"]
    assert "Final review" in review and "not read" not in review and "Run inspect_workflows" not in review
    assert agent.summary == "y"


def test_repeated_identical_call_is_not_rerun(sample_repo):
    # Regression (2026-10-24): list_files with the same arguments for all 20 turns.
    llm = FakeLLM([resp(tool_use(f"t{i}", "list_files", subdir=".")) for i in range(3)])
    agent = AuditAgent(llm, RepoTools(sample_repo), max_turns=3, review=False)
    agent.run("https://github.com/a/b")
    first, repeat = tool_results(llm.calls[1])[0], tool_results(llm.calls[2])[0]
    assert not first.get("is_error") and "tracked file" in first["content"]
    assert (
        repeat["is_error"] and "already called list_files with exactly these arguments in turn 1" in repeat["content"]
    )
