"""Gemini adapter (default provider) and provider selection tests: real SDK types, fake client, no network."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS

import httpx
import pytest
from google.genai import errors, types

from auditor.gemini_llm import (
    STATIC_CHAIN,
    GeminiLLM,
    ModelStatus,
    discover_models,
    normalize,
    parse_fallbacks,
    quota_day,
    rank_models,
    to_contents,
    to_tools,
)
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
    monkeypatch.delenv("AUDIT_FALLBACK_MODELS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("AUDIT_MODEL_STATUS_FILE", "none")
    monkeypatch.setattr("auditor.gemini_llm.discover_models", lambda client: list(STATIC_CHAIN))  # no network
    gem = make_llm()
    assert isinstance(gem, GeminiLLM) and gem.models == list(STATIC_CHAIN)
    assert gem.model == "gemini-3.8-flash"
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


# ---------------------------------------------------------------- retries and fallback


def server_error(code=503, status="UNAVAILABLE"):
    return errors.ServerError(code, {"error": {"code": code, "message": "high demand", "status": status}})


def quota_error(*quota_ids, retry="7s", limit=None):
    violations = [{"quotaId": q, **({"quotaValue": str(limit)} if limit else {})} for q in quota_ids]
    details = [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": violations},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry},
    ]
    body = {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": details}}
    return errors.ClientError(429, body)


DAILY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
PER_MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"


def ok(version):
    return types.GenerateContentResponse(
        model_version=version,
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part.from_text(text="hi")]),
                finish_reason=types.FinishReason.STOP,
            )
        ],
    )


class ScriptedModels:
    """Per-model queues of outcomes: an exception to raise or a response to return."""

    def __init__(self, **outcomes):
        self.outcomes = {m.replace("_", "-"): list(v) for m, v in outcomes.items()}
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        outcome = self.outcomes[model].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make(models, primary="m-a", fallbacks=("m-b", "m-c")):
    sleeps = []
    llm = GeminiLLM(model=primary, fallbacks=list(fallbacks), client=NS(models=models), sleep=sleeps.append)
    return llm, sleeps


ASK = ("sys", [], [{"role": "user", "content": "hi"}])


def test_retries_transient_503_then_succeeds_on_same_model():
    models = ScriptedModels(m_a=[server_error(), ok("m-a-001")])
    llm, sleeps = make(models)
    assert llm.create(*ASK).stop_reason == "end_turn"
    assert models.calls == ["m-a", "m-a"] and len(sleeps) == 1
    assert llm.models_used == ["m-a-001"]


def test_falls_back_after_repeated_503_and_stays_there():
    models = ScriptedModels(m_a=[server_error()] * 3, m_b=[ok("m-b-001"), ok("m-b-001")])
    llm, sleeps = make(models)
    llm.create(*ASK)
    llm.create(*ASK)
    assert models.calls == ["m-a", "m-a", "m-b", "m-b"]  # two attempts per model
    assert llm.model == "m-b" and len(sleeps) == 1


def test_daily_quota_fails_over_immediately_without_waiting():
    models = ScriptedModels(m_a=[quota_error(DAILY, retry="58466s")], m_b=[ok("m-b")])
    llm, sleeps = make(models)
    llm.create(*ASK)
    assert models.calls == ["m-a", "m-b"] and sleeps == []


def test_per_minute_quota_waits_the_requested_delay():
    models = ScriptedModels(m_a=[quota_error(PER_MINUTE, retry="7s"), ok("m-a")])
    llm, sleeps = make(models)
    llm.create(*ASK)
    assert models.calls == ["m-a", "m-a"] and sleeps == [7.0]


def test_long_rate_limit_wait_fails_over_instead():
    models = ScriptedModels(m_a=[quota_error(PER_MINUTE, retry="300s")], m_b=[ok("m-b")])
    llm, sleeps = make(models)
    llm.create(*ASK)
    assert models.calls == ["m-a", "m-b"] and sleeps == []


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("stuck"),
        errors.ClientError(404, {"error": {"code": 404, "message": "retired", "status": "NOT_FOUND"}}),
    ],
)
def test_timeout_and_retired_model_fail_over(exc):
    models = ScriptedModels(m_a=[exc], m_b=[ok("m-b")])
    llm, _ = make(models)
    llm.create(*ASK)
    assert models.calls == ["m-a", "m-b"]


def test_all_models_unavailable_is_one_clear_error():
    models = ScriptedModels(
        m_a=[quota_error(DAILY, retry="58466s")], m_b=[server_error()] * 3, m_c=[httpx.ReadTimeout("x")]
    )
    llm, _ = make(models)
    with pytest.raises(LLMError) as e:
        llm.create(*ASK)
    msg = str(e.value)
    assert "m-a: daily free-tier quota used up, resets in ~16h" in msg
    assert "m-b: 503 UNAVAILABLE after 2 attempts" in msg and "m-c: request timed out" in msg


@pytest.mark.parametrize(
    "exc, message",
    [
        (errors.ClientError(403, {"error": {"code": 403, "message": "denied", "status": "PERMISSION_DENIED"}}), "auth"),
        (errors.ClientError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}}), "400"),
    ],
)
def test_auth_and_request_errors_do_not_fall_back(exc, message):
    models = ScriptedModels(m_a=[exc], m_b=[ok("m-b")])
    llm, _ = make(models)
    with pytest.raises(LLMError, match=message):
        llm.create(*ASK)
    assert models.calls == ["m-a"]


def test_parse_fallbacks_and_dedupe(monkeypatch):
    assert parse_fallbacks(None) is None  # unset: discover the chain
    assert parse_fallbacks("none") == [] and parse_fallbacks("") == []
    assert parse_fallbacks(" x , y,") == ["x", "y"]
    monkeypatch.setenv("AUDIT_FALLBACK_MODELS", "m-a, m-b")
    llm = GeminiLLM(model="m-a", client=NS(models=None))
    assert llm.models == ["m-a", "m-b"]


# ---------------------------------------------------------------- model chain


LIVE_NAMES = [  # generateContent models on the development key, 2026-10-23 (abridged)
    "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-preview-tts", "gemma-4-26b-a4b-it", "gemma-4-31b-it",
    "gemini-flash-latest", "gemini-flash-lite-latest", "gemini-pro-latest", "gemini-2.5-flash-lite",
    "gemini-2.5-flash-image", "gemini-3-flash-preview", "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview-customtools", "gemini-3.1-flash-lite-preview", "gemini-3.1-flash-lite",
    "gemini-3-pro-image", "gemini-3.1-flash-lite-image", "gemini-3.5-flash", "gemini-3.5-flash-lite",
    "gemini-omni-1.1-flash", "gemini-3.5-transcribe", "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash",
    "gemini-4-argon", "gemini-3.8-flash-lite-tts", "gemini-robotics-er-2-preview", "antigravity-preview-09-2026",
    "gemini-2.5-computer-use-preview-10-2025", "deep-research-preview-04-2026",
]  # fmt: skip


def test_rank_models_most_capable_first():
    assert rank_models(LIVE_NAMES) == [
        "gemini-3.1-pro-preview",  # pro before any flash
        "gemini-2.5-pro",
        "gemini-3.8-flash",  # newest version first
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",  # its -preview is dropped: it answers as this model
        "gemini-2.5-flash-lite",
        "gemma-4-31b-it",  # gemma last, larger first
        "gemma-4-26b-a4b-it",
    ]


def test_static_chain_is_in_rank_order():
    assert rank_models(list(STATIC_CHAIN)) == list(STATIC_CHAIN)


def test_preview_kept_when_no_stable_release():
    assert rank_models(["gemini-3-flash-preview", "gemini-3.5-flash"]) == ["gemini-3.5-flash", "gemini-3-flash-preview"]


def test_discover_models_uses_list_and_falls_back_to_static():
    listing = [
        NS(name=f"models/{n}", supported_actions=["generateContent"])
        for n in ["gemini-3.6-flash", "gemini-3.1-pro-preview"]
    ]
    listing.append(NS(name="models/gemini-3.8-flash", supported_actions=["embedContent"]))
    assert discover_models(NS(models=NS(list=lambda: listing))) == ["gemini-3.1-pro-preview", "gemini-3.6-flash"]
    assert discover_models(NS(models=NS())) == list(STATIC_CHAIN)  # listing failed


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def test_recorded_unavailable_model_is_skipped_without_a_request(tmp_path):
    clock, path = Clock(), tmp_path / "status.json"
    ModelStatus(path, clock).block("m-a", "daily free-tier quota used up", 3600)
    models = ScriptedModels(m_b=[ok("m-b")])
    llm = GeminiLLM(model="m-a", fallbacks=["m-b"], client=NS(models=models), status_path=path, clock=clock)
    llm.create(*ASK)
    assert models.calls == ["m-b"]  # m-a never contacted
    assert "m-a: daily free-tier quota used up (recorded earlier; retry in ~1h)" in llm.skipped


def test_status_survives_runs_and_expires(tmp_path):
    clock, path = Clock(), tmp_path / "status.json"
    models = ScriptedModels(m_a=[quota_error(DAILY, retry="7200s")], m_b=[ok("m-b")])
    GeminiLLM(model="m-a", fallbacks=["m-b"], client=NS(models=models), status_path=path, clock=clock).create(*ASK)
    assert ModelStatus(path, clock).blocked("m-a")  # a new run sees the recorded quota
    clock.now += 7201
    assert ModelStatus(path, clock).blocked("m-a") is None  # and forgets it after the reset


def test_status_file_from_before_request_counting_is_still_read(tmp_path):
    clock, path = Clock(), tmp_path / "status.json"
    path.write_text(json.dumps({"m-a": {"until": clock.now + 60, "reason": "old"}}), encoding="utf-8")
    assert ModelStatus(path, clock).blocked("m-a").startswith("old")


def test_first_request_is_the_check_no_extra_requests():
    models = ScriptedModels(m_a=[quota_error(DAILY, retry="3600s")], m_b=[ok("m-b"), ok("m-b")])
    llm, _ = make(models)
    llm.create(*ASK)
    llm.create(*ASK)
    assert models.calls == ["m-a", "m-b", "m-b"]  # the failed real request moved on; nothing else sent


def test_model_without_free_tier_is_recorded_for_a_week(tmp_path):
    clock, path = Clock(), tmp_path / "status.json"
    every_quota = quota_error(DAILY, PER_MINUTE, "GenerateContentInputTokensPerModelPerDay-FreeTier", retry="3600s")
    models = ScriptedModels(m_a=[every_quota], m_b=[ok("m-b")])
    llm = GeminiLLM(model="m-a", fallbacks=["m-b"], client=NS(models=models), status_path=path, clock=clock)
    llm.create(*ASK)
    assert llm.skipped == ["m-a: not available on the free tier"]
    clock.now += 6 * 86400
    assert "not available on the free tier" in ModelStatus(path, clock).blocked("m-a")


def test_daily_limit_is_learned_and_counted_locally(tmp_path):
    clock, path = Clock(), tmp_path / "status.json"
    models = ScriptedModels(m_a=[quota_error(DAILY, retry="60s", limit=20)], m_b=[ok("m-b")])
    llm = GeminiLLM(model="m-a", fallbacks=["m-b"], client=NS(models=models), status_path=path, clock=clock)
    llm.create(*ASK)
    assert llm.skipped == ["m-a: daily free-tier quota used up (20 requests a day), resets in ~1min"]
    status = ModelStatus(path, clock)
    assert status.limits == {"m-a": 20} and status.used == {"m-a": 20, "m-b": 1}
    clock.now += 120  # the recorded block has expired, but the local count still says spent
    assert ModelStatus(path, clock).blocked("m-a") == "daily free-tier quota used up (20 requests counted today)"


def test_model_low_on_quota_is_passed_over_unless_nothing_better(tmp_path):
    clock, path = Clock(), tmp_path / "status.json"
    status = ModelStatus(path, clock)
    status.limits.update({"m-a": 20, "m-b": 20})
    status.used.update({"m-a": 15, "m-b": 5})  # m-a has 5 left, below the reserve; m-b has 15
    status._save()
    models = ScriptedModels(m_b=[ok("m-b")])
    llm = GeminiLLM(model="m-a", fallbacks=["m-b"], client=NS(models=models), status_path=path, clock=clock)
    llm.create(*ASK)
    assert models.calls == ["m-b"]

    status = ModelStatus(path, clock)
    status.used["m-b"] = 19
    status._save()
    models = ScriptedModels(m_a=[ok("m-a")])  # both are low now: the more capable one is used
    llm = GeminiLLM(model="m-a", fallbacks=["m-b"], client=NS(models=models), status_path=path, clock=clock)
    llm.create(*ASK)
    assert models.calls == ["m-a"]


def test_request_counts_reset_at_midnight_pacific():
    clock = Clock()
    clock.now = datetime(2026, 10, 23, 6, 59, tzinfo=UTC).timestamp()  # 23:59 PDT on the 22nd
    assert quota_day(clock.now) == "2026-10-22"
    status = ModelStatus(None, clock)
    status.limits["m-a"] = 20
    status.count("m-a")
    assert status.remaining("m-a") == 19
    clock.now += 120  # 00:01 PDT: a new quota day
    assert quota_day(clock.now) == "2026-10-23" and status.remaining("m-a") == 20


def test_stays_on_serving_model_even_below_reserve():
    models = ScriptedModels(m_a=[ok("m-a")] * 3)
    llm, _ = make(models)
    llm.status.limits["m-a"] = 11
    for _ in range(3):
        llm.create(*ASK)  # 11 -> 8 left: no switch mid-audit while the model still serves
    assert models.calls == ["m-a"] * 3


def test_unsupported_feature_moves_on():
    no_tools = errors.ClientError(
        400, {"error": {"code": 400, "message": "Function calling is not enabled for models/x", "status": "x"}}
    )
    models = ScriptedModels(m_a=[no_tools], m_b=[ok("m-b")])
    llm, _ = make(models)
    llm.create(*ASK)
    assert models.calls == ["m-a", "m-b"] and "does not support this request" in llm.skipped[0]


def test_gemma_gets_history_without_thought_signatures():
    signed = model_reply(
        types.Part(text="plan", thought=True),
        types.Part(function_call=types.FunctionCall(name="list_files", args={}), thought_signature=b"sig"),
    )
    first = normalize(signed)
    messages = [
        {"role": "user", "content": "Audit it"},
        {"role": "assistant", "content": first.content},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": first.content[0].id, "content": "x"}]},
    ]
    seen = {}

    class Recording:
        def generate_content(self, model, contents, config):
            seen[model] = contents[1].parts
            return ok(model)

    for model in ("gemini-x", "gemma-4-26b-a4b-it"):
        GeminiLLM(model=model, fallbacks=[], client=NS(models=Recording())).create("sys", [], messages)
    assert seen["gemini-x"] == first.content.raw.parts  # Gemini: replayed unchanged
    (part,) = seen["gemma-4-26b-a4b-it"]  # Gemma: thought dropped, signature removed
    assert part.function_call.name == "list_files" and part.thought_signature is None


def test_server_timeout_504_moves_on_without_retrying():
    models = ScriptedModels(m_a=[server_error(504, "DEADLINE_EXCEEDED")], m_b=[ok("m-b")])
    llm, sleeps = make(models)
    llm.create(*ASK)
    assert models.calls == ["m-a", "m-b"] and sleeps == []
    assert "server timed out" in llm.skipped[0]
