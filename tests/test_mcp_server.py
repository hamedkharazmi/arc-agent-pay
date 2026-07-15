"""
Tests for the MCP session engine (arc_agent_pay.mcp_server.session).

A stub PaymentClient factory is injected, so no wallet, chain, or `mcp` package
is needed. We verify discovery, budget accounting across calls, the session
budget ceiling, and graceful handling of unknown services.
"""

from __future__ import annotations

import pytest

from arc_agent_pay import ServiceRegistry
from arc_agent_pay.mcp_server.session import MCPSession


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"ok": True}

    def json(self):
        return self._payload


class StubClient:
    """Async-context PaymentClient stand-in that 'spends' a fixed amount."""

    def __init__(self, spend="0.001", response=None):
        self._spend = spend
        self._response = response or FakeResponse()
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        return self._response

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        return self._response

    def summary(self):
        return {
            "budget": {"spent_usdc": self._spend},
            "payments": [{"amount_usdc": self._spend, "tx_reference": "0xtx"}],
        }


def _session(**kwargs):
    factory = kwargs.pop("client_factory", lambda remaining: StubClient())
    return MCPSession(
        registry=ServiceRegistry(include_builtins=True),
        client_factory=factory,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Discovery + introspection
# ---------------------------------------------------------------------------

def test_discover_returns_services():
    results = _session().discover("crypto prices", max_results=2)
    assert results and "name" in results[0]
    assert len(results) <= 2


def test_list_services_returns_builtins():
    names = {s["name"] for s in _session().list_services()}
    assert "Crypto Price Feed" in names


def test_budget_status_initial():
    status = _session(budget_usdc="0.50").budget_status()
    assert status == {"budget_usdc": 0.5, "spent_usdc": 0.0, "remaining_usdc": 0.5}


# ---------------------------------------------------------------------------
# pay_and_fetch
# ---------------------------------------------------------------------------

async def test_pay_and_fetch_records_spend():
    session = _session(budget_usdc="0.50")
    out = await session.pay_and_fetch("Crypto Price Feed")
    assert out["service"] == "Crypto Price Feed"
    assert out["data"] == {"ok": True}
    assert out["payment"]["amount_usdc"] == "0.001"
    assert session.budget_status()["spent_usdc"] == pytest.approx(0.001)


async def test_pay_and_fetch_unknown_service():
    out = await _session().pay_and_fetch("Nonexistent Service XYZ")
    assert "error" in out


async def test_session_budget_ceiling_enforced():
    # Each call "spends" 0.001 (the service price); a 0.0015 budget allows one.
    session = _session(budget_usdc="0.0015")
    first = await session.pay_and_fetch("Crypto Price Feed")
    assert "data" in first
    # Remaining (0.0005) < price (0.001) → the next fetch is blocked pre-flight.
    second = await session.pay_and_fetch("Crypto Price Feed")
    assert second.get("error") == "session budget exhausted"


async def test_pay_and_fetch_non_200_reports_error():
    session = _session(client_factory=lambda r: StubClient(response=FakeResponse(402)))
    out = await session.pay_and_fetch("Crypto Price Feed")
    assert out["error"] == "HTTP 402"


def test_agent_identity_none_without_resolution(monkeypatch):
    monkeypatch.delenv("AGENT_PRIVATE_KEY", raising=False)
    # No account/key configured → best-effort identity returns None, not an error.
    assert _session().agent_identity() is None
