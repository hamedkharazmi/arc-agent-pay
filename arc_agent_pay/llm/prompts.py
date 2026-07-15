"""
llm/prompts.py — prompt construction shared by all LLM providers.
"""

from __future__ import annotations

import json
from typing import Any


def build_research_prompt(topic: str, fetched_data: dict[str, Any]) -> str:
    """
    Build the research-synthesis prompt from data fetched off paid APIs.

    The model is instructed to ground its report strictly in the provided data.
    """
    context_parts = [
        f"--- {name} ---\n{json.dumps(data, indent=2)}"
        for name, data in fetched_data.items()
    ]
    return (
        f'You are a research analyst. Write a concise, well-structured Markdown '
        f'research report on the topic: "{topic}".\n\n'
        f"Use only the data provided below — do not invent facts. "
        f"Include sections for any of: market prices, news headlines, whale activity, "
        f"or research briefs that appear in the data. Keep it under 400 words.\n\n"
        f"DATA:\n" + "\n\n".join(context_parts)
    )
