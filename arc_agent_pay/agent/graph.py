"""
agent/graph.py — the LangGraph research agent.

A real ReAct-style state graph:

    START → planner → (tool_calls?) ──yes──▶ tools ──▶ planner
                          │
                          └──no / budget spent──▶ synthesizer → END

The planner (an LLM bound to the tools) decides whether to discover/fetch more
or to stop. Tool failures (including budget exhaustion) come back as tool
messages the planner can react to. `steps_left` bounds the loop.

Imports langgraph/langchain — only loaded on the graph code path.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from arc_agent_pay.interceptor import PaymentClient
from arc_agent_pay.llm import LLMProvider
from arc_agent_pay.registry import ServiceRegistry

from .nodes import make_planner, make_synthesizer
from .state import ResearchState
from .tools import build_tools
from .trust import ReputationGate


def build_graph(
    *,
    client: PaymentClient,
    registry: ServiceRegistry,
    topic: str,
    llm: Any,                       # a LangChain chat model supporting bind_tools
    collected: dict[str, Any],
    failed: list[str],
    provider: Optional[LLMProvider] = None,
    on_event: Optional[Callable[[str, dict], None]] = None,
    max_steps: int = 6,
    gate: Optional[ReputationGate] = None,
):
    """Build and compile the research StateGraph for a single run."""
    from langgraph.graph import END, START, StateGraph
    from langgraph.prebuilt import ToolNode

    tools = build_tools(
        client=client,
        registry=registry,
        topic=topic,
        collected=collected,
        failed=failed,
        on_event=on_event,
        gate=gate,
    )
    llm_with_tools = llm.bind_tools(tools)

    planner = make_planner(llm_with_tools)
    synthesizer = make_synthesizer(
        topic=topic,
        collected=collected,
        client=client,
        provider=provider,
        on_event=on_event,
    )
    tool_node = ToolNode(tools)

    async def route_after_planner(state: ResearchState) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None) and state["steps_left"] > 0:
            return "tools"
        return "synthesizer"

    graph = StateGraph(ResearchState)
    graph.add_node("planner", planner)
    graph.add_node("tools", tool_node)
    graph.add_node("synthesizer", synthesizer)

    graph.add_edge(START, "planner")
    graph.add_conditional_edges(
        "planner",
        route_after_planner,
        {"tools": "tools", "synthesizer": "synthesizer"},
    )
    graph.add_edge("tools", "planner")
    graph.add_edge("synthesizer", END)

    return graph.compile()
