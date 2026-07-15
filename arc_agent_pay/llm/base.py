"""
llm/base.py — the provider-agnostic LLM interface.

`LLMProvider` is a minimal Protocol so the SDK is not coupled to any single
vendor. Concrete providers (OpenAI, ArcAPIs) implement `complete()`. The
keyless template fallback is handled one level up in `synthesize_report()`
(see __init__.py) because it formats structured data rather than completing a
prompt.

This keeps the abstraction honest: `LLMProvider` describes *real* text
completion, and the factory `get_provider()` returns `None` when no provider is
configured so callers can degrade gracefully.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    """A text-completion provider. Implementations must be async."""

    #: Short, human-readable provider name (used in logs/events).
    name: str

    async def complete(self, prompt: str, **opts) -> str:
        """Return a completion for `prompt`. May raise on transport errors."""
        ...
