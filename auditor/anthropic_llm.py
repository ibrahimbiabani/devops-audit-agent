"""Optional Anthropic Claude provider.

Gemini is the default provider (see llm.py). This module is imported only when
the anthropic provider is selected, and needs requirements-anthropic.txt.
The loop's message format matches the Messages API, so no conversion is needed.
"""

from __future__ import annotations

import os
from typing import Any

import anthropic

from auditor.llm import LLMError

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
# Server-side fallback: if the primary model's safety classifier declines a
# request (security-audit content can trip it), the API retries on a
# recommended fallback model instead of returning a refusal. Haiku has no
# server-side fallback, so it is not requested there.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeLLM:
    def __init__(
        self,
        model: str | None = None,
        effort: str | None = None,
        max_tokens: int = 16_000,
        timeout: float = 300.0,
    ):
        self.model = model or os.getenv("AUDIT_MODEL") or DEFAULT_CLAUDE_MODEL
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
