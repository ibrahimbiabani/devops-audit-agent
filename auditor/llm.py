"""LLM providers and provider selection.

Only provider SDKs are used here; the agent loop itself lives in loop.py. The
loop speaks the Anthropic message shape; other providers (gemini_llm.py)
translate to and from it.
"""

from __future__ import annotations

import os
from typing import Any

import anthropic

PROVIDERS = ("anthropic", "gemini")
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
# Server-side fallback: if the primary model's safety classifier declines a
# request (security-audit content can trip it), the API retries on a
# recommended fallback model instead of returning a refusal. Haiku has no
# server-side fallback, so it is not requested there.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """The LLM call failed and the audit cannot continue."""


def detect_provider() -> str:
    """Pick a provider from LLM_PROVIDER, else from whichever API key is set."""
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return "gemini"
    return "anthropic"


def make_llm(provider: str | None = None, model: str | None = None):
    provider = (provider or detect_provider()).lower()
    if provider == "anthropic":
        return ClaudeLLM(model=model)
    if provider == "gemini":
        from auditor.gemini_llm import GeminiLLM

        return GeminiLLM(model=model)
    raise LLMError(f"Unknown LLM provider {provider!r}; use one of {', '.join(PROVIDERS)}")


class ClaudeLLM:
    def __init__(
        self,
        model: str | None = None,
        effort: str | None = None,
        max_tokens: int = 16_000,
        timeout: float = 300.0,
    ):
        self.model = model or os.getenv("AUDIT_MODEL") or DEFAULT_MODEL
        self.effort = effort or os.getenv("AUDIT_EFFORT") or DEFAULT_EFFORT
        self.max_tokens = max_tokens
        # The SDK retries 408/409/429/5xx and connection errors with backoff.
        self.client = anthropic.Anthropic(timeout=timeout, max_retries=4)

    def create(self, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]):
        extra: dict[str, Any] = {}
        if not self.model.startswith("claude-haiku"):
            extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        try:
            return self.client.beta.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                tools=tools,
                messages=messages,
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                **extra,
            )
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic API authentication failed; check ANTHROPIC_API_KEY") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError(f"API key lacks permission for model {self.model}") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Model not found: {self.model}") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"Bad request to the Anthropic API: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Rate limited by the Anthropic API after retries") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"Could not reach the Anthropic API: {e}") from e
