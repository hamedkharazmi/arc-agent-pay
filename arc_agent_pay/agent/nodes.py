"""
agent/nodes.py — graph node builders (planner + synthesizer).

Tool execution itself is handled by LangGraph's prebuilt ToolNode in graph.py.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from arc_agent_pay.interceptor import PaymentClient
from arc_agent_pay.llm import LLMProvider, get_provider, synthesize_report

from .linear import report_footer
from .state import ResearchState

logger = logging.getLogger(__name__)


def build_system_prompt(topic: str, budget_usdc: str) -> str:
    return (
        "You are an autonomous research agent that pays for API data with USDC.\n"
        f'Research topic: "{topic}".\n'
        f"Session budget: {budget_usdc} USDC (payments are automatic per fetch).\n\n"
        "Plan:\n"
        "1. Call discover_services to find relevant paid data sources.\n"
        "2. Call fetch_service for the sources most relevant to the topic, "
        "staying within budget.\n"
        "3. When you have enough data, STOP calling tools and reply with a brief "
        "note that you are done — a separate step writes the final report.\n\n"
        "Do not invent data. Prefer fewer, well-chosen fetches over exhaustive ones."
    )


def make_planner(llm_with_tools: Any) -> Callable:
    """Planner node: let the LLM decide the next tool call (or to stop)."""

    async def planner(state: ResearchState) -> dict:
        ai_message = await llm_with_tools.ainvoke(state["messages"])
        return {"messages": [ai_message], "steps_left": state["steps_left"] - 1}

    return planner


def make_synthesizer(
    *,
    topic: str,
    collected: dict[str, Any],
    client: PaymentClient,
    provider: Optional[LLMProvider] = None,
    on_event: Optional[Callable[[str, dict], None]] = None,
) -> Callable:
    """Synthesizer node: turn collected data into the final Markdown report."""

    async def synthesizer(state: ResearchState) -> dict:
        summary = client.summary()
        footer = report_footer(summary)

        if not collected:
            report = f"No data was fetched for topic: '{topic}'" + footer
            if on_event:
                on_event("report_completed", {"markdown": report})
            return {"report": report}

        # Resolve the provider up front so the UI can show which engine ran.
        resolved = provider or get_provider()
        provider_name = resolved.name if resolved else "template"

        if on_event:
            on_event(
                "synthesis_started",
                {"note": "Synthesizing report", "provider": provider_name},
            )
        body = await synthesize_report(topic, collected, resolved)
        report = body + footer
        if on_event:
            payload: dict[str, Any] = {"markdown": report, "provider": provider_name}
            # Surface on-chain packet quota when synthesis ran via arcapis.
            packet = getattr(resolved, "last_call", None)
            if packet:
                payload["packet"] = packet
            on_event("report_completed", payload)
        return {"report": report}

    return synthesizer
