"""Google Gemini provider.

The agent loop keeps its conversation in the Anthropic message shape. This
adapter converts that history into Gemini ``contents`` on every call and turns
Gemini's reply back into blocks with ``.type`` of ``text`` or ``tool_use``.

Model turns are replayed to Gemini exactly as received (the raw ``Content`` is
kept on the returned block list), so thought signatures survive.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

from auditor.llm import LLMError

DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
REFUSAL_REASONS = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
RETRY_STATUS = [429, 500, 502, 503, 504]


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
    def __init__(
        self,
        model: str | None = None,
        max_tokens: int = 16_000,
        timeout: float = 300.0,
        client: Any = None,
    ):
        self.model = model or os.getenv("AUDIT_MODEL") or DEFAULT_GEMINI_MODEL
        self.max_tokens = max_tokens
        if client is not None:
            self.client = client
            return
        # Retries with exponential backoff absorb free-tier per-minute limits.
        http_options = types.HttpOptions(
            timeout=int(timeout * 1000),
            retry_options=types.HttpRetryOptions(
                attempts=6, initial_delay=2.0, max_delay=60.0, http_status_codes=RETRY_STATUS
            ),
        )
        try:
            self.client = genai.Client(http_options=http_options)  # reads GEMINI_API_KEY / GOOGLE_API_KEY
        except ValueError as e:
            raise LLMError("No Gemini API key found; set GEMINI_API_KEY") from e

    def create(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Response:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=to_tools(tools),
            max_output_tokens=self.max_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        try:
            resp = self.client.models.generate_content(model=self.model, contents=to_contents(messages), config=config)
        except errors.ClientError as e:
            if e.code in (401, 403):
                raise LLMError("Gemini API authentication failed; check GEMINI_API_KEY") from e
            if e.code == 404:
                raise LLMError(f"Gemini model not found: {self.model}") from e
            if e.code == 429:
                raise LLMError("Gemini rate limit or daily quota exhausted after retries") from e
            raise LLMError(f"Gemini API error {e.code}: {e.message}") from e
        except errors.APIError as e:
            raise LLMError(f"Gemini API error {e.code}: {e.message}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Could not reach the Gemini API: {e}") from e
        return normalize(resp)
