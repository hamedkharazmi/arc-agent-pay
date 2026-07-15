"""
agent/state.py — LangGraph state schema for the research agent.

Imports langgraph, so this module is only loaded on the graph code path
(via graph.py), never at package import time.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class ResearchState(TypedDict):
    """
    State threaded through the graph.

    `messages` accumulates the ReAct conversation (system → human → AI tool
    calls → tool results → ...). `steps_left` bounds the planner/tool loop so a
    misbehaving model can't spin forever. Fetched API payloads are collected via
    a closure shared with the tools (see graph.py) rather than the state, to keep
    the schema small.
    """

    topic: str
    budget_usdc: str
    messages: Annotated[list, add_messages]
    report: str
    steps_left: int
