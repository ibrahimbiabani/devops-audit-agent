"""Google Gemini provider (the default).

The agent loop keeps its conversation as role/content messages with
``tool_use`` and ``tool_result`` blocks. This adapter converts that history
into Gemini ``contents`` on every call and turns Gemini's reply back into
blocks with ``.type`` of ``text`` or ``tool_use``.

Model turns are replayed to Gemini exactly as received (the raw ``Content`` is
kept on the returned block list), so thought signatures survive.
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

from auditor.llm import LLMError

# A concrete model, not a "-latest" alias, so results are reproducible: aliases
# are repointed by Google (gemini-flash-latest moved to gemini-3.8-flash, whose
# free tier allows only 20 requests/day - less than one audit).
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
# Tried in order, for the rest of the audit, when the current model is
# overloaded, out of quota or retired. Each model has its own free-tier quota.
# Checked on 2026-10-22 against the live API: a conversation started on one of
# these continues on any other when model turns are replayed unchanged.
DEFAULT_FALLBACK_MODELS = ("gemini-3.1-flash-lite", "gemini-3.6-flash", "gemini-3-flash-preview")
ATTEMPTS_PER_MODEL = 3
MAX_RETRY_WAIT = 60.0
TRANSIENT_STATUS = {500, 502, 503, 504}
REFUSAL_REASONS = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}

log = logging.getLogger(__name__)


class ModelUnavailable(Exception):
    """The current model cannot serve this request; try the next one."""


def parse_fallbacks(value: str | None) -> list[str]:
    """AUDIT_FALLBACK_MODELS: comma-separated models, or 'none' to disable fallback."""
    if value is None:
        return list(DEFAULT_FALLBACK_MODELS)
    if value.strip().lower() in ("", "none", "off"):
        return []
    return [m.strip() for m in value.split(",") if m.strip()]


def _error_details(e: errors.APIError) -> list[dict[str, Any]]:
    body = e.details if isinstance(e.details, dict) else {}
    details = (body.get("error") or {}).get("details") or []
    return [d for d in details if isinstance(d, dict)]


def is_daily_quota(e: errors.APIError) -> bool:
    """A 429 caused by a per-day quota, which waiting a minute will not fix."""
    return any(
        "PerDay" in str(v.get("quotaId", ""))
        for d in _error_details(e)
        for v in d.get("violations") or []
        if isinstance(v, dict)
    )


def retry_delay(e: errors.APIError) -> float | None:
    """Seconds the API asks us to wait (RetryInfo.retryDelay), if given."""
    for d in _error_details(e):
        if str(d.get("@type", "")).endswith("RetryInfo"):
            m = re.match(r"([\d.]+)s$", str(d.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    return None


def _backoff(attempt: int) -> float:
    return 2.0 * 2**attempt + random.uniform(0, 1)


@dataclass
class Block:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None  # Gemini's own call id, if it sent one


class Turn(list):
    """A model turn as normalized blocks, carrying the raw Gemini Content."""

    def __init__(self, blocks: list[Block], raw: types.Content | None):
        super().__init__(blocks)
        self.raw = raw


@dataclass
class Response:
    content: Turn
    stop_reason: str


def to_tools(schemas: list[dict[str, Any]]) -> list[types.Tool]:
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=s["name"], description=s["description"], parameters_json_schema=s["input_schema"]
                )
                for s in schemas
            ]
        )
    ]


def to_contents(messages: list[dict[str, Any]]) -> list[types.Content]:
    calls: dict[str, tuple[str, str | None]] = {}  # loop tool_use id -> (name, gemini call id)
    contents: list[types.Content] = []
    for m in messages:
        if m["role"] == "assistant":
            turn = m["content"]
            for b in turn:
                if b.type == "tool_use":
                    calls[b.id] = (b.name, getattr(b, "call_id", None))
            raw = getattr(turn, "raw", None)
            if raw is not None and raw.parts:
                contents.append(types.Content(role="model", parts=raw.parts))
            continue

        content = m["content"]
        if isinstance(content, str):
            parts = [types.Part.from_text(text=content)]
        else:
            parts = []
            for item in content:
                if item["type"] == "tool_result":
                    name, call_id = calls[item["tool_use_id"]]
                    key = "error" if item.get("is_error") else "output"
                    parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=call_id, name=name, response={key: item["content"]}
                            )
                        )
                    )
                elif item["type"] == "text":
                    parts.append(types.Part.from_text(text=item["text"]))
        contents.append(types.Content(role="user", parts=parts))

    # Gemini expects alternating roles; merge neighbours with the same role.
    merged: list[types.Content] = []
    for c in contents:
        if merged and merged[-1].role == c.role:
            merged[-1] = types.Content(role=c.role, parts=[*merged[-1].parts, *c.parts])
        else:
            merged.append(c)
    return merged


def normalize(resp: types.GenerateContentResponse) -> Response:
    if resp.prompt_feedback and resp.prompt_feedback.block_reason:
        return Response(Turn([], None), "refusal")
    if not resp.candidates:
        return Response(Turn([], None), "end_turn")

    cand = resp.candidates[0]
    raw = cand.content
    blocks: list[Block] = []
    for part in raw.parts if raw and raw.parts else []:
        if part.function_call:
            fc = part.function_call
            blocks.append(
                Block(
                    type="tool_use",
                    id=fc.id or f"call_{uuid.uuid4().hex[:12]}",
                    name=fc.name or "",
                    input=dict(fc.args or {}),
                    call_id=fc.id,
                )
            )
        elif part.text and not part.thought:
            blocks.append(Block(type="text", text=part.text))

    reason = getattr(cand.finish_reason, "name", str(cand.finish_reason or "STOP"))
    if reason in REFUSAL_REASONS:
        stop = "refusal"
    elif any(b.type == "tool_use" for b in blocks):
        stop = "tool_use"
    elif reason == "MAX_TOKENS":
        stop = "max_tokens"
    else:
        stop = "end_turn"
    return Response(Turn(blocks, raw), stop)


class GeminiLLM:
    """Gemini client with per-model retries and automatic fallback to other models.

    Retries are handled here rather than by the SDK, so each failure is handled
    on its own terms: brief overloads are retried, daily-quota errors and stuck
    requests fail over to the next model at once, and auth or request errors
    stop the audit with a clear message.
    """

    def __init__(
        self,
        model: str | None = None,
        fallbacks: list[str] | None = None,
        max_tokens: int = 16_000,
        timeout: float = 120.0,
        client: Any = None,
        sleep: Any = time.sleep,
    ):
        primary = model or os.getenv("AUDIT_MODEL") or DEFAULT_GEMINI_MODEL
        if fallbacks is None:
            fallbacks = parse_fallbacks(os.getenv("AUDIT_FALLBACK_MODELS"))
        self.models = list(dict.fromkeys([primary, *fallbacks]))
        self.index = 0
        self.max_tokens = max_tokens
        self.models_used: list[str] = []  # concrete versions the API reported, in order of first use
        self._sleep = sleep
        if client is not None:
            self.client = client
            return
        try:  # reads GEMINI_API_KEY / GOOGLE_API_KEY; no SDK-level retries (see class docstring)
            self.client = genai.Client(http_options=types.HttpOptions(timeout=int(timeout * 1000)))
        except ValueError as e:
            raise LLMError("No Gemini API key found; set GEMINI_API_KEY") from e

    @property
    def model(self) -> str:
        return self.models[self.index]

    def create(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Response:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=to_tools(tools),
            max_output_tokens=self.max_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = to_contents(messages)
        failures: list[str] = []
        while True:
            model = self.model
            try:
                resp = self._generate(model, contents, config)
            except ModelUnavailable as e:
                failures.append(f"{model}: {e}")
                if self.index + 1 >= len(self.models):
                    raise LLMError("No Gemini model could serve the request. " + "; ".join(failures)) from e
                self.index += 1
                log.warning("Gemini model %s unavailable (%s); switching to %s", model, e, self.model)
                continue
            self._note_version(model, resp)
            return normalize(resp)

    def _generate(self, model: str, contents: list[types.Content], config: types.GenerateContentConfig):
        """Call one model, retrying brief overloads. Raises ModelUnavailable to fail over."""
        for attempt in range(ATTEMPTS_PER_MODEL):
            final = attempt == ATTEMPTS_PER_MODEL - 1
            try:
                return self.client.models.generate_content(model=model, contents=contents, config=config)
            except errors.ClientError as e:
                if e.code in (401, 403):
                    raise LLMError("Gemini API authentication failed; check GEMINI_API_KEY") from e
                if e.code == 404:
                    raise ModelUnavailable("model not found or no longer available") from e
                if e.code != 429:
                    raise LLMError(f"Gemini API error {e.code}: {e.message}") from e
                if is_daily_quota(e):
                    wait = retry_delay(e)
                    reset = f", resets in ~{wait / 3600:.0f}h" if wait else ""
                    raise ModelUnavailable(f"daily free-tier quota used up{reset}") from e
                delay = retry_delay(e) or _backoff(attempt)
                if final or delay > MAX_RETRY_WAIT:
                    raise ModelUnavailable("rate limited") from e
                log.info("Gemini %s rate limited; retrying in %.0fs", model, delay)
                self._sleep(delay)
            except errors.APIError as e:
                if e.code not in TRANSIENT_STATUS:
                    raise LLMError(f"Gemini API error {e.code}: {e.message}") from e
                if final:
                    raise ModelUnavailable(f"{e.code} {e.status} after {ATTEMPTS_PER_MODEL} attempts") from e
                delay = _backoff(attempt)
                log.info("Gemini %s returned %s %s; retrying in %.0fs", model, e.code, e.status, delay)
                self._sleep(delay)
            except httpx.TimeoutException as e:
                # A request stuck this long means the model is congested: fail over, don't wait again.
                raise ModelUnavailable("request timed out") from e
            except httpx.HTTPError as e:
                raise LLMError(f"Could not reach the Gemini API: {e}") from e
        raise ModelUnavailable("retries exhausted")  # not reached: the final attempt raises

    def _note_version(self, model: str, resp: types.GenerateContentResponse) -> None:
        version = resp.model_version or model
        if version not in self.models_used:
            self.models_used.append(version)
            log.info("Gemini model %s is serving as %s", model, version)
