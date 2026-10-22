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
    agent = AuditAgent(llm, RepoTools(sample_repo), trace_path=trace)
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
    agent = AuditAgent(llm, RepoTools(sample_repo))
    assert [v["rule_id"] for v in agent.run("https://github.com/a/b")] == ["CI-001"]
    rejected = tool_results(llm.calls[1])[1]
    assert rejected["is_error"] and "already recorded at .github/workflows/ci.yml:1" in rejected["content"]


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
    AuditAgent(llm, RepoTools(sample_repo), trace_path=trace).run("https://github.com/a/b")
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
    agent = AuditAgent(llm, RepoTools(sample_repo), trace_path=tmp_path / "missing-dir" / "trace.jsonl")
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
    AuditAgent(llm, RepoTools(sample_repo), trace_path=trace).run("https://github.com/a/b")
    entries = {e["tool"] + e["input"].get("path", ""): e for e in map(json.loads, trace.read_text().splitlines())}
    assert entries["read_fileapp.py"]["output_preview"] == "app.py (lines 1-4 of 4) [contents omitted]"
    assert entries["search_code"]["output_preview"] == "app.py:3"
    assert "alice" not in trace.read_text()
