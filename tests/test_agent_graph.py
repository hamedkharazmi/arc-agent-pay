"""
Tests for the LangGraph research agent (arc_agent_pay.agent.graph).

The graph is driven with a duck-typed fake chat model (canned tool calls) and a
stub PaymentClient, so no LLM or network/chain calls happen. We assert the ReAct
loop discovers → fetches → synthesizes, that failures are recorded, and that the
graph emits the same on_event vocabulary the SSE frontend depends on.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from arc_agent_pay import ServiceRegistry
from arc_agent_pay.agent.graph import build_graph
from arc_agent_pay.exceptions import InsufficientFundsError


# ---------------------------------------------------------------------------
# Test environment guards
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _disable_external_llm_providers(monkeypatch):
    """Force keyless synthesis path so tests never call external providers."""
    monkeypatch.delenv("ARCAPIS_TOKEN_ID", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeChatModel:
    """Returns canned AIMessages; bind_tools is a no-op (graph only needs ainvoke)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, _messages):
        msg = self._responses[self._i]
        self._i += 1
        return msg


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class StubPaymentClient:
    """Minimal stand-in: records calls, returns canned responses, no real payment."""

    def __init__(self, response=None, raise_exc=None):
        self._response = response or FakeResponse(
            200, {"data": {"BTC": {"price_usd": 100000, "change_24h_pct": 2.5}}}
        )
        self._raise_exc = raise_exc
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        if self._raise_exc:
            raise self._raise_exc
        return self._response

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        if self._raise_exc:
            raise self._raise_exc
        return self._response

    def summary(self):
        return {"budget": {"spent_usdc": "0.001"}, "payment_count": len(self.calls)}


def _event_collector():
    events = []
    return events, lambda t, p: events.append((t, p))


def _registry():
    return ServiceRegistry(include_builtins=True)


# ---------------------------------------------------------------------------
# Happy path: discover -> fetch -> synthesize
# ---------------------------------------------------------------------------

async def test_graph_discovers_fetches_synthesizes():
    responses = [
        AIMessage(content="", tool_calls=[
            {"name": "discover_services", "args": {"query": "crypto prices"}, "id": "1"},
        ]),
        AIMessage(content="", tool_calls=[
            {"name": "fetch_service", "args": {"service_name": "Crypto Price Feed"}, "id": "2"},
        ]),
        AIMessage(content="I have enough data."),
    ]
    llm = FakeChatModel(responses)
    client = StubPaymentClient()
    collected, failed = {}, []
    events, on_event = _event_collector()

    graph = build_graph(
        client=client,
        registry=_registry(),
        topic="crypto prices",
        llm=llm,
        collected=collected,
        failed=failed,
        provider=None,  # template synthesis (keyless)
        on_event=on_event,
        max_steps=6,
    )
    result = await graph.ainvoke(
        {
            "topic": "crypto prices",
            "budget_usdc": "0.10",
            "messages": [],
            "report": "",
            "steps_left": 6,
        },
        {"recursion_limit": 24},
    )

    assert collected, "fetched data should be collected"
    assert "Crypto Price Feed" in collected
    assert not failed
    assert "Research Report" in result["report"]
    assert "Total spent" in result["report"]  # footer present

    types = [t for t, _ in events]
    for expected in (
        "services_discovered",
        "fetch_started",
        "fetch_completed",
        "report_completed",
    ):
        assert expected in types, f"missing event: {expected}"


# ---------------------------------------------------------------------------
# Failure path: a budget-exhausted fetch is recorded, run still completes
# ---------------------------------------------------------------------------

async def test_graph_records_failed_fetch_on_budget_block():
    responses = [
        AIMessage(content="", tool_calls=[
            {"name": "fetch_service", "args": {"service_name": "Crypto Price Feed"}, "id": "1"},
        ]),
        AIMessage(content="Stopping."),
    ]
    llm = FakeChatModel(responses)
    client = StubPaymentClient(raise_exc=InsufficientFundsError("0.01", "0.00"))
    collected, failed = {}, []
    events, on_event = _event_collector()

    graph = build_graph(
        client=client,
        registry=_registry(),
        topic="crypto",
        llm=llm,
        collected=collected,
        failed=failed,
        provider=None,
        on_event=on_event,
        max_steps=4,
    )
    result = await graph.ainvoke(
        {
            "topic": "crypto",
            "budget_usdc": "0.10",
            "messages": [],
            "report": "",
            "steps_left": 4,
        },
        {"recursion_limit": 16},
    )

    assert "Crypto Price Feed" in failed
    assert not collected
    assert "No data was fetched" in result["report"]


# ---------------------------------------------------------------------------
# Tool returns a JSON-serializable string the model can read
# ---------------------------------------------------------------------------

async def test_fetch_tool_returns_json_string():
    from arc_agent_pay.agent.tools import build_tools

    client = StubPaymentClient(FakeResponse(200, {"data": {"ETH": {"price_usd": 3500}}}))
    collected, failed = {}, []
    tools = build_tools(
        client=client,
        registry=_registry(),
        topic="eth",
        collected=collected,
        failed=failed,
        on_event=None,
    )
    fetch = next(t for t in tools if t.name == "fetch_service")
    out = await fetch.ainvoke({"service_name": "Crypto Price Feed"})
    assert json.loads(out)["data"]["ETH"]["price_usd"] == 3500
    assert "Crypto Price Feed" in collected
