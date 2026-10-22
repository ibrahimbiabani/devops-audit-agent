"""LLM provider selection.

Google Gemini is the default provider. Anthropic Claude is an optional
alternative: its SDK is only needed (and only imported) when it is selected.

Only provider SDKs are used; the agent loop itself lives in loop.py. Each
provider converts the loop's message history to its own API format.
"""

from __future__ import annotations

import os

DEFAULT_PROVIDER = "gemini"
PROVIDERS = ("gemini", "anthropic")


class LLMError(Exception):
    """The LLM call failed and the audit cannot continue."""


def detect_provider() -> str:
    """LLM_PROVIDER if set; otherwise Gemini, unless only an Anthropic key is present."""
    explicit = os.getenv("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return "gemini"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return DEFAULT_PROVIDER


def make_llm(provider: str | None = None, model: str | None = None):
    provider = (provider or detect_provider()).lower()
    if provider == "gemini":
        from auditor.gemini_llm import GeminiLLM

        return GeminiLLM(model=model)
    if provider == "anthropic":
        try:
            from auditor.anthropic_llm import ClaudeLLM
        except ImportError as e:
            raise LLMError("The anthropic provider needs its SDK: pip install -r requirements-anthropic.txt") from e
        return ClaudeLLM(model=model)
    raise LLMError(f"Unknown LLM provider {provider!r}; use one of {', '.join(PROVIDERS)}")
