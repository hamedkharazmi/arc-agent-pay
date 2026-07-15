"""
llm/openai_provider.py — OpenAI-backed LLMProvider.

Requires the `[llm]` (or `[agent]`/`[demo]`) extra:  pip install "arc-agent-pay[llm]"
"""

from __future__ import annotations

import os
from typing import Optional


class OpenAIProvider:
    """Completes prompts with OpenAI chat models (default: gpt-4o-mini)."""

    name = "openai"

    def __init__(self, model: str = "gpt-4o-mini", api_key: Optional[str] = None) -> None:
        self.model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")

    async def complete(self, prompt: str, **opts) -> str:
        try:
            import openai
        except ImportError as e:  # pragma: no cover - import guard
            raise ImportError(
                'OpenAI provider requires the openai package.\n'
                'Install with: pip install "arc-agent-pay[llm]"'
            ) from e

        client = openai.AsyncOpenAI(api_key=self._api_key)
        response = await client.chat.completions.create(
            model=opts.get("model", self.model),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=opts.get("max_tokens", 1024),
            temperature=opts.get("temperature", 0.4),
        )
        return response.choices[0].message.content or ""
