"""
arc_agent_pay.llm — provider-agnostic LLM layer.

Public surface:
    LLMProvider        Protocol implemented by concrete providers.
    get_provider()     Factory selecting a provider from the environment.
    synthesize_report  High-level helper: build prompt → complete, with a
                       keyless template fallback.

Provider selection order (preserves the original behaviour):
    1. ARCAPIS_TOKEN_ID  → ArcAPIsProvider  (on-chain inference, USDC-settled)
    2. OPENAI_API_KEY    → OpenAIProvider   (off-chain, API key)
    3. neither           → None             (caller uses the template fallback)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .arcapis_provider import ArcAPIsProvider
from .base import LLMProvider
from .openai_provider import OpenAIProvider
from .prompts import build_research_prompt
from .template import template_report

logger = logging.getLogger(__name__)

__all__ = [
    "LLMProvider",
    "OpenAIProvider",
    "ArcAPIsProvider",
    "get_provider",
    "synthesize_report",
    "build_research_prompt",
    "template_report",
]


def get_provider() -> Optional[LLMProvider]:
    """
    Return a configured LLM provider, or None if none is available.

    Mirrors the historical selection order: ArcAPIs first (on-chain),
    then OpenAI, then None (the caller falls back to the template report).
    """
    if os.environ.get("ARCAPIS_TOKEN_ID"):
        return ArcAPIsProvider()
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIProvider()
    return None


async def synthesize_report(
    topic: str,
    fetched_data: dict[str, Any],
    provider: Optional[LLMProvider] = None,
) -> str:
    """
    Synthesize a Markdown research report from fetched data.

    Uses `provider` if given, else `get_provider()`. If still no provider is
    available, falls back to the keyless template formatter.
    """
    provider = provider or get_provider()

    if provider is None:
        logger.info("[llm] No provider configured — using template fallback")
        return template_report(topic, fetched_data)

    prompt = build_research_prompt(topic, fetched_data)
    logger.info("[llm] Synthesizing report with provider=%s", provider.name)
    return await provider.complete(prompt)
