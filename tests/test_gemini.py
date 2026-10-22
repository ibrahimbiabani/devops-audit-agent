"""Gemini adapter (default provider) and provider selection tests: real SDK types, fake client, no network."""

from types import SimpleNamespace as NS

import pytest
from google.genai import types

from auditor.gemini_llm import GeminiLLM, normalize, to_contents, to_tools
from auditor.llm import LLMError, detect_provider, make_llm
from auditor.loop import AuditAgent
from tools import TOOL_SCHEMAS, RepoTools


def model_reply(*parts, finish=types.FinishReason.STOP):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)), finish_reason=finish)]
    )


def call(name, **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


class FakeModels:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def generate_content(self, model, contents, config):
        self.requests.append(contents)
        return self.replies.pop(0)


def test_tools_convert_to_function_declarations():
    (tool,) = to_tools(TOOL_SCHEMAS)
    names = [d.name for d in tool.function_declarations]
    assert names == ["list_files", "read_file", "search_code", "git_log"]
    assert tool.function_declarations[1].parameters_json_schema["required"] == ["path"]


def test_normalize_function_call():
    r = normalize(model_reply(types.Part.from_text(text="Looking"), call("read_file", path="a.py")))
    assert r.stop_reason == "tool_use"
    assert [b.type for b in r.content] == ["text", "tool_use"]
    assert r.content[1].name == "read_file" and r.content[1].input == {"path": "a.py"}
    assert r.content[1].id  # synthesized when Gemini omits one


def test_normalize_skips_thought_parts():
    r = normalize(model_reply(types.Part(text="secret reasoning", thought=True), types.Part.from_text(text="Done")))
    assert [b.text for b in r.content] == ["Done"]
    assert r.stop_reason == "end_turn"


@pytest.mark.parametrize("finish", [types.FinishReason.SAFETY, types.FinishReason.PROHIBITED_CONTENT])
def test_normalize_safety_is_refusal(finish):
    assert normalize(model_reply(finish=finish)).stop_reason == "refusal"


def test_normalize_blocked_prompt_is_refusal():
    resp = types.GenerateContentResponse(
        prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason=types.BlockedReason.SAFETY)
    )
    assert normalize(resp).stop_reason == "refusal"


def test_to_contents_round_trip():
    first = normalize(model_reply(call("list_files"), call("git_log", max_count=5)))
    a, b = first.content
    messages = [
        {"role": "user", "content": "Audit it"},
        {"role": "assistant", "content": first.content},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": a.id, "content": "3 files"},
                {"type": "tool_result", "tool_use_id": b.id, "content": "Error: x", "is_error": True},
                {"type": "text", "text": "2 turns left"},
            ],
        },
    ]
    contents = to_contents(messages)
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts == first.content.raw.parts  # replayed unchanged
    fr = [p.function_response for p in contents[2].parts if p.function_response]
    assert [(f.name, f.response) for f in fr] == [
        ("list_files", {"output": "3 files"}),
        ("git_log", {"error": "Error: x"}),
    ]
    assert contents[2].parts[-1].text == "2 turns left"


def test_to_contents_merges_same_role_neighbours():
    empty_turn = normalize(types.GenerateContentResponse(candidates=[])).content
    messages = [
        {"role": "user", "content": "Audit it"},
        {"role": "assistant", "content": empty_turn},
        {"role": "user", "content": "Continue"},
    ]
    contents = to_contents(messages)
    assert len(contents) == 1 and [p.text for p in contents[0].parts] == ["Audit it", "Continue"]


def test_agent_runs_on_gemini_adapter(sample_repo):
    violation = {
        "rule_id": "CI-001",
        "category": "ci_cd",
        "severity": "high",
        "file": ".github/workflows/ci.yml",
        "line": 1,
        "description": "Workflow uses pull_request_target",
        "evidence": "on: pull_request_target",
        "recommendation": "Use pull_request",
    }
    models = FakeModels(
        [
            model_reply(call("read_file", path=".github/workflows/ci.yml")),
            model_reply(call("record_violation", **violation), call("finish_audit", summary="done")),
        ]
    )
    llm = GeminiLLM(model="gemini-test", client=NS(models=models))
    assert AuditAgent(llm, RepoTools(sample_repo)).run("https://github.com/a/b") == [violation]
    sent = models.requests[1][-1].parts[0].function_response
    assert sent.name == "read_file" and "pull_request_target" in sent.response["output"]


def test_provider_detection_defaults_to_gemini(monkeypatch):
    for var in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert detect_provider() == "gemini"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert detect_provider() == "anthropic"  # only an Anthropic key present
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert detect_provider() == "gemini"  # Gemini wins when both are set
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    assert detect_provider() == "anthropic"


def test_make_llm_gemini_default(monkeypatch):
    monkeypatch.delenv("AUDIT_MODEL", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    gem = make_llm()
    assert isinstance(gem, GeminiLLM) and gem.model == "gemini-flash-latest"
    with pytest.raises(LLMError, match="Unknown LLM provider"):
        make_llm("openai")


def test_make_llm_optional_anthropic(monkeypatch):
    pytest.importorskip("anthropic")
    from auditor.anthropic_llm import ClaudeLLM

    monkeypatch.delenv("AUDIT_MODEL", raising=False)
    claude = make_llm("anthropic")
    assert isinstance(claude, ClaudeLLM) and claude.model == "claude-opus-5-5"


def test_anthropic_without_sdk_is_llm_error(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "anthropic", None)  # simulate the SDK not being installed
    monkeypatch.delitem(sys.modules, "auditor.anthropic_llm", raising=False)
    with pytest.raises(LLMError, match="requirements-anthropic.txt"):
        make_llm("anthropic")


def test_gemini_without_key_is_llm_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiLLM()
