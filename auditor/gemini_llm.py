"""Google Gemini provider (the default).

The agent loop keeps its conversation as role/content messages with
``tool_use`` and ``tool_result`` blocks. This adapter converts that history
into Gemini ``contents`` on every call and turns Gemini's reply back into
blocks with ``.type`` of ``text`` or ``tool_use``.

Model turns are replayed to Gemini exactly as received (the raw ``Content`` is
kept on the returned block list), so thought signatures survive.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from google import genai
from google.genai import errors, types

from auditor.llm import LLMError

# The model chain is every general-purpose text model the API key can use, ordered from the
# most to the least capable: tier first (pro > flash > flash-lite > gemma), then newest version,
# then stable before preview (Gemma: larger before smaller). Aliases (-latest) and specialised
# variants (image, tts, omni, ...) are excluded, so every entry is a concrete model.
TIER_RANK = {"pro": 0, "flash": 1, "flash-lite": 2, "gemma": 3}
MODEL_NAME = re.compile(r"^gemini-(?P<version>\d+(?:\.\d+)?)-(?P<tier>pro|flash-lite|flash)(?P<preview>-preview)?$")
# Google's open-weight models, served on the same key with their own quota (e.g. gemma-4-26b-a4b-it).
GEMMA_NAME = re.compile(r"^gemma-(?P<version>\d+(?:\.\d+)?)-(?P<size>\d+)b(?:-a\d+b)?-it$")
# Used only if the model list cannot be fetched: the models that served a tool call on the
# free tier in the 2026-10-23 check (evals/model-checks/2026-10-23.json), in rank order.
STATIC_CHAIN = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
)
ATTEMPTS_PER_MODEL = 2
MAX_RETRY_WAIT = 60.0
TRANSIENT_STATUS = {500, 502, 503, 504}
REFUSAL_REASONS = {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
# How long a model is skipped after each kind of failure (seconds). A daily quota uses the
# reset time the API reports, falling back to DAILY_COOLDOWN.
DAILY_COOLDOWN = 6 * 3600
COOLDOWN = {
    "retired": 7 * 86400,
    "not_free": 7 * 86400,
    "unsupported": 7 * 86400,
    "overloaded": 600,
    "timeout": 600,
    "rate": 120,
}
# A model is only chosen if it has at least this many requests of today's quota left (when its
# limit is known), so an audit does not start on a model about to run out. Audits in the
# 2026-10-22 evaluation made 5-20 successful requests each, 6 at the median.
RESERVE_REQUESTS = 10
# Daily quotas reset at midnight Pacific time. tzdata supplies the zone on Windows; without it,
# Pacific standard time is close enough (off by an hour during daylight saving).
try:
    PACIFIC: tzinfo = ZoneInfo("America/Los_Angeles")
except Exception:
    PACIFIC = timezone(timedelta(hours=-8))

log = logging.getLogger(__name__)


class ModelUnavailable(Exception):
    """The current model cannot serve this request; try the next one."""

    def __init__(self, reason: str, cooldown: float):
        super().__init__(reason)
        self.cooldown = cooldown


def is_gemma(model: str) -> bool:
    return model.startswith("gemma-")


def _rank_key(name: str) -> tuple:
    m = MODEL_NAME.match(name) or GEMMA_NAME.match(name)
    major, _, minor = m["version"].partition(".")
    if is_gemma(name):
        return (TIER_RANK["gemma"], -int(major), -int(minor or 0), False, -int(m["size"]))
    return (TIER_RANK[m["tier"]], -int(major), -int(minor or 0), bool(m["preview"]), 0)


def rank_models(names: list[str]) -> list[str]:
    """Concrete text models from ``names``, most capable first."""
    found = {n for n in names if MODEL_NAME.match(n) or GEMMA_NAME.match(n)}
    # A preview whose stable release is also listed is answered by that release (e.g.
    # gemini-3.1-flash-lite-preview reports model_version gemini-3.1-flash-lite), so as a
    # fallback it would only repeat the same model and quota.
    found = {n for n in found if not (n.endswith("-preview") and n.removesuffix("-preview") in found)}
    return sorted(found, key=_rank_key)


def discover_models(client: Any) -> list[str]:
    """The ranked chain from the API's model list (a metadata call, no generation quota)."""
    try:
        names = [
            m.name.removeprefix("models/")
            for m in client.models.list()
            if "generateContent" in (m.supported_actions or [])
        ]
        ranked = rank_models(names)
        if ranked:
            return ranked
    except errors.ClientError as e:
        if e.code in (401, 403):
            raise LLMError("Gemini API authentication failed; check GEMINI_API_KEY") from e
        log.warning("Could not list Gemini models (%s); using the built-in model chain", e)
    except Exception as e:  # network trouble, unexpected client: the static chain still works
        log.warning("Could not list Gemini models (%s); using the built-in model chain", type(e).__name__)
    return list(STATIC_CHAIN)


def parse_fallbacks(value: str | None) -> list[str] | None:
    """AUDIT_FALLBACK_MODELS: comma-separated models, 'none' to disable, unset to discover."""
    if value is None:
        return None
    if value.strip().lower() in ("", "none", "off"):
        return []
    return [m.strip() for m in value.split(",") if m.strip()]


def default_status_path() -> Path | None:
    configured = os.getenv("AUDIT_MODEL_STATUS_FILE")
    if configured is not None:
        return None if configured.strip().lower() in ("", "none", "off") else Path(configured)
    return Path.home() / ".cache" / "devops-audit-agent" / "gemini-model-status.json"


def _duration(seconds: float) -> str:
    return f"{seconds / 3600:.0f}h" if seconds >= 3600 else f"{max(1, round(seconds / 60))}min"


def quota_day(ts: float) -> str:
    """The quota day containing ``ts`` (daily quotas reset at midnight Pacific)."""
    return datetime.fromtimestamp(ts, PACIFIC).date().isoformat()


class ModelStatus:
    """Remembers, across runs, which models cannot serve and how much daily quota each has used.

    - A model that is out of quota, retired or overloaded is recorded with the time it may
      work again, and skipped until then.
    - Successful requests are counted per model per quota day, and a model's daily limit is
      learned from the quota error that states it. Together they tell how many requests a
      model has left today without spending one. The count misses requests made elsewhere
      with the same key, so a real quota error always has the last word.

    Stored as JSON; any read or write problem just means nothing is remembered.
    """

    def __init__(self, path: Path | None, clock: Any = time.time):
        self.path = path
        self.clock = clock
        self.entries: dict[str, dict[str, Any]] = {}  # model -> {"until": timestamp, "reason": str}
        self.limits: dict[str, int] = {}  # model -> requests per day
        self.day = quota_day(clock())
        self.used: dict[str, int] = {}  # model -> successful requests on self.day
        if path is None:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        blocked = data.get("blocked", data)  # files written before request counting held only blocks
        self.entries = {k: v for k, v in blocked.items() if isinstance(v, dict) and "until" in v}
        self.limits = {k: v for k, v in (data.get("daily_limits") or {}).items() if isinstance(v, int)}
        usage = data.get("usage") or {}
        if usage.get("day") == self.day:
            self.used = {k: v for k, v in (usage.get("requests") or {}).items() if isinstance(v, int)}

    def blocked(self, model: str) -> str | None:
        """Why ``model`` should be skipped right now, or None."""
        entry = self.entries.get(model)
        if entry and entry["until"] > self.clock():
            return f"{entry['reason']} (recorded earlier; retry in ~{_duration(entry['until'] - self.clock())})"
        if self.remaining(model) == 0:
            return f"daily free-tier quota used up ({self.limits[model]} requests counted today)"
        return None

    def remaining(self, model: str) -> int | None:
        """Requests ``model`` has left today, or None if its limit is not known yet."""
        limit = self.limits.get(model)
        if limit is None:
            return None
        self._roll()
        return max(0, limit - self.used.get(model, 0))

    def count(self, model: str) -> None:
        """Record one successful request."""
        self._roll()
        self.used[model] = self.used.get(model, 0) + 1
        self._save()

    def learn_limit(self, model: str, limit: int) -> None:
        """A daily-quota error stated ``limit``: remember it, and that today's quota is spent."""
        self._roll()
        self.limits[model] = limit
        self.used[model] = max(self.used.get(model, 0), limit)
        self._save()

    def block(self, model: str, reason: str, seconds: float) -> None:
        self.entries[model] = {"until": self.clock() + seconds, "reason": reason}
        self._save()

    def _roll(self) -> None:
        today = quota_day(self.clock())
        if today != self.day:
            self.day, self.used = today, {}

    def _save(self) -> None:
        if self.path is None:
            return
        now = self.clock()
        data = {
            "blocked": {k: v for k, v in self.entries.items() if v["until"] > now},
            "daily_limits": self.limits,
            "usage": {"day": self.day, "requests": self.used},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as e:
            log.info("Could not save model status (%s)", e)


def _error_details(e: errors.APIError) -> list[dict[str, Any]]:
    body = e.details if isinstance(e.details, dict) else {}
    details = (body.get("error") or {}).get("details") or []
    return [d for d in details if isinstance(d, dict)]


def _violations(e: errors.APIError) -> list[dict[str, Any]]:
    return [v for d in _error_details(e) for v in d.get("violations") or [] if isinstance(v, dict)]


def is_daily_quota(e: errors.APIError) -> bool:
    """A 429 caused by a per-day quota, which waiting a minute will not fix."""
    return any("PerDay" in str(v.get("quotaId", "")) for v in _violations(e))


def daily_limit(e: errors.APIError) -> int | None:
    """The requests-per-day limit a daily-quota error states, if it states one."""
    for v in _violations(e):
        if "RequestsPerDay" in str(v.get("quotaId", "")) and str(v.get("quotaValue", "")).isdigit():
            return int(v["quotaValue"])
    return None


def not_on_free_tier(e: errors.APIError) -> bool:
    """A 429 from a model with no free-tier quota at all.

    Such a model reports every free-tier quota (per minute and per day) as exceeded on its very
    first request, with no limit stated. Seen for the Pro, Argon and Omni models in the
    2026-10-23 check; a model that merely ran out reports only the quota it used up, with its limit.
    """
    violations = _violations(e)
    ids = " ".join(str(v.get("quotaId", "")) for v in violations)
    stated = any(str(v.get("quotaValue", "0")) not in ("", "0") for v in violations)
    return "FreeTier" in ids and "PerMinute" in ids and "PerDay" in ids and not stated


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


def to_contents(messages: list[dict[str, Any]], signatures: bool = True) -> list[types.Content]:
    """Gemini ``contents`` for the conversation.

    With ``signatures=False`` (for Gemma) model turns are replayed without their thought parts
    and thought signatures: Gemma rejects a conversation containing Gemini's signatures with a
    400, and accepts the same turns without them (checked 2026-10-23).
    """
    calls: dict[str, tuple[str, str | None]] = {}  # loop tool_use id -> (name, gemini call id)
    contents: list[types.Content] = []
    for m in messages:
        if m["role"] == "assistant":
            turn = m["content"]
            for b in turn:
                if b.type == "tool_use":
                    calls[b.id] = (b.name, getattr(b, "call_id", None))
            raw = getattr(turn, "raw", None)
            parts = list(raw.parts or []) if raw is not None else []
            if not signatures:
                parts = [p.model_copy(update={"thought_signature": None}) for p in parts if not p.thought]
            if parts:
                contents.append(types.Content(role="model", parts=parts))
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


_DEFAULT = object()


class GeminiLLM:
    """Gemini client that works down a chain of models, most capable first.

    A model is chosen without spending a request on it: models recorded as out of quota,
    retired or not on the free tier are skipped, as are models with fewer than
    RESERVE_REQUESTS of today's quota left (unless nothing better remains). The first real
    request then shows whether the model serves. Each model gets ATTEMPTS_PER_MODEL tries;
    then the audit moves to the next model and stays there. Retries are handled here rather
    than by the SDK, so each failure gets its own treatment: brief overloads are retried,
    daily-quota errors and stuck requests move on at once, and auth or request errors stop
    the audit with a clear message.
    """

    def __init__(
        self,
        model: str | None = None,
        fallbacks: list[str] | None = None,
        max_tokens: int = 16_000,
        timeout: float = 120.0,
        client: Any = None,
        sleep: Any = time.sleep,
        status_path: Any = _DEFAULT,
        clock: Any = time.time,
    ):
        injected = client is not None
        if injected:
            self.client = client
        else:
            try:  # reads GEMINI_API_KEY / GOOGLE_API_KEY; no SDK-level retries (see class docstring)
                self.client = genai.Client(http_options=types.HttpOptions(timeout=int(timeout * 1000)))
            except ValueError as e:
                raise LLMError("No Gemini API key found; set GEMINI_API_KEY") from e

        primary = model or os.getenv("AUDIT_MODEL")
        if fallbacks is None:
            fallbacks = parse_fallbacks(os.getenv("AUDIT_FALLBACK_MODELS"))
        if fallbacks is None:
            fallbacks = discover_models(self.client)
        self.models = list(dict.fromkeys([m for m in [primary, *fallbacks] if m]))
        if not self.models:
            self.models = discover_models(self.client)[:1]
        self.current: str | None = None  # the model serving this audit
        self.dropped: set[str] = set()  # models given up on during this audit
        self.max_tokens = max_tokens
        self.models_used: list[str] = []  # concrete versions the API reported, in order of first use
        self.skipped: list[str] = []  # why models were passed over, for the final error message
        self._sleep = sleep
        # Injected clients are test doubles: no status file unless one is given.
        if status_path is _DEFAULT:
            status_path = None if injected else default_status_path()
        self.status = ModelStatus(status_path, clock)

    @property
    def model(self) -> str:
        return self.current or self.models[0]

    def create(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]) -> Response:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=to_tools(tools),
            max_output_tokens=self.max_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        while True:
            model = self._select()
            contents = to_contents(messages, signatures=not is_gemma(model))
            try:
                resp = self._generate(model, contents, config)
            except ModelUnavailable as e:
                self._give_up(model, e)
                continue
            self.status.count(model)
            self._note_version(model, resp)
            return normalize(resp)

    def _select(self) -> str:
        """The model for the next request, chosen without sending one.

        Stay on the current model while it works. Otherwise take the most capable model that
        is not known to be unavailable and has at least RESERVE_REQUESTS of today's quota left
        (or an unknown amount); if none has that much, the most capable one with any left.
        """
        current = self.current
        if current and current not in self.dropped and not self.status.blocked(current):
            return current
        low: list[str] = []
        for model in self.models:
            if model in self.dropped:
                continue
            reason = self.status.blocked(model)
            if reason:
                self._drop(model, reason)
                log.info("Skipping Gemini model %s: %s", model, reason)
                continue
            left = self.status.remaining(model)
            if left is not None and left < RESERVE_REQUESTS:
                log.info("Passing over Gemini model %s for now: %d request(s) of today's quota left", model, left)
                low.append(model)
                continue
            return self._use(model)
        if low:
            return self._use(low[0])
        raise LLMError("No Gemini model could serve the request. " + "; ".join(self.skipped))

    def _use(self, model: str) -> str:
        if model != self.current:
            log.info("Using Gemini model %s", model)
        self.current = model
        return model

    def _drop(self, model: str, reason: str) -> None:
        self.dropped.add(model)
        self.skipped.append(f"{model}: {reason}")

    def _give_up(self, model: str, e: ModelUnavailable) -> None:
        self._drop(model, str(e))
        self.status.block(model, str(e), e.cooldown)
        log.warning("Gemini model %s unavailable (%s); switching to the next model", model, e)

    def _generate(self, model: str, contents: Any, config: types.GenerateContentConfig):
        """Call one model, retrying brief overloads. Raises ModelUnavailable to move on."""
        for attempt in range(ATTEMPTS_PER_MODEL):
            final = attempt == ATTEMPTS_PER_MODEL - 1
            try:
                return self.client.models.generate_content(model=model, contents=contents, config=config)
            except errors.ClientError as e:
                if e.code in (401, 403):
                    raise LLMError("Gemini API authentication failed; check GEMINI_API_KEY") from e
                if e.code == 404:
                    raise ModelUnavailable("model not found or no longer available", COOLDOWN["retired"]) from e
                if e.code == 429 and not_on_free_tier(e):
                    raise ModelUnavailable("not available on the free tier", COOLDOWN["not_free"]) from e
                if e.code == 429 and is_daily_quota(e):
                    wait = retry_delay(e) or DAILY_COOLDOWN
                    limit = daily_limit(e)
                    if limit:
                        self.status.learn_limit(model, limit)
                    stated = f" ({limit} requests a day)" if limit else ""
                    reason = f"daily free-tier quota used up{stated}, resets in ~{_duration(wait)}"
                    raise ModelUnavailable(reason, wait) from e
                if e.code == 429:
                    delay = retry_delay(e) or _backoff(attempt)
                    if final or delay > MAX_RETRY_WAIT:
                        raise ModelUnavailable("rate limited", COOLDOWN["rate"]) from e
                    log.info("Gemini %s rate limited; retrying in %.0fs", model, delay)
                    self._sleep(delay)
                    continue
                message = e.message or ""
                if e.code == 400 and re.search(r"not (enabled|supported)", message, re.IGNORECASE):
                    # A capability this model lacks (e.g. "Function calling is not enabled for
                    # models/..."): the next model may have it.
                    reason = f"does not support this request ({message[:80]})"
                    raise ModelUnavailable(reason, COOLDOWN["unsupported"]) from e
                raise LLMError(f"Gemini API error {e.code}: {message}") from e
            except errors.APIError as e:
                if e.code not in TRANSIENT_STATUS:
                    raise LLMError(f"Gemini API error {e.code}: {e.message}") from e
                if e.code == 504:
                    # The server's own deadline expired after minutes of waiting, the same as a
                    # client timeout: retrying would wait that long again, so move on now.
                    raise ModelUnavailable("504 DEADLINE_EXCEEDED (server timed out)", COOLDOWN["timeout"]) from e
                if final:
                    reason = f"{e.code} {e.status} after {ATTEMPTS_PER_MODEL} attempts"
                    raise ModelUnavailable(reason, COOLDOWN["overloaded"]) from e
                delay = _backoff(attempt)
                log.info("Gemini %s returned %s %s; retrying in %.0fs", model, e.code, e.status, delay)
                self._sleep(delay)
            except httpx.TimeoutException as e:
                # A request stuck this long means the model is congested: move on, don't wait again.
                raise ModelUnavailable("request timed out", COOLDOWN["timeout"]) from e
            except httpx.HTTPError as e:
                raise LLMError(f"Could not reach the Gemini API: {e}") from e
        raise ModelUnavailable("retries exhausted", COOLDOWN["overloaded"])  # not reached

    def _note_version(self, model: str, resp: types.GenerateContentResponse) -> None:
        version = resp.model_version or model
        if version not in self.models_used:
            self.models_used.append(version)
            log.info("Gemini model %s is serving as %s", model, version)
