"""
mcp_server/session.py — the engine behind the MCP tools.

MCPSession holds the wallet + a session-wide budget ceiling and exposes the
operations the MCP tools wrap: discover services, pay-and-fetch, budget status,
and (optionally) the agent's onchain identity.

Budget safety: each fetch uses a fresh PaymentClient capped at the *remaining*
session budget, and spend accumulates across calls — so an MCP host can never
exceed the configured ceiling no matter how many tools it calls.

The PaymentClient factory is injectable for testing (no wallet/chain needed).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

from arc_agent_pay import PaymentClient, ServiceRegistry
from arc_agent_pay.models import Chain

logger = logging.getLogger(__name__)


class MCPSession:
    def __init__(
        self,
        *,
        registry: Optional[Any] = None,
        budget_usdc: str = "0.50",
        account: Any = None,
        chain: Chain = Chain.ARC_TESTNET,
        client_factory: Optional[Callable[[float], Any]] = None,
    ) -> None:
        self.registry = registry or ServiceRegistry(include_builtins=True)
        self.budget_total = float(budget_usdc)
        self.spent = 0.0
        self.account = account
        self.chain = chain
        self._client_factory = client_factory or self._default_client_factory

    # ------------------------------------------------------------------
    # PaymentClient construction
    # ------------------------------------------------------------------

    def _resolve_account(self) -> Any:
        if self.account is not None:
            return self.account
        key = os.environ.get("AGENT_PRIVATE_KEY", "").strip()
        if not key:
            raise EnvironmentError("AGENT_PRIVATE_KEY is required to make payments.")
        from eth_account import Account

        return Account.from_key(key if key.startswith("0x") else "0x" + key)

    def _default_client_factory(self, remaining: float) -> Any:
        return PaymentClient(
            account=self._resolve_account(),
            budget_usdc=str(remaining),
            chain=self.chain,
        )

    # ------------------------------------------------------------------
    # Discovery + introspection
    # ------------------------------------------------------------------

    def discover(self, query: str, max_results: int = 5) -> list[dict]:
        return [
            {
                "name": s.name,
                "url": s.url,
                "price_usdc": s.price_usdc,
                "tags": s.tags,
                "description": s.description,
            }
            for s in self.registry.search(query, max_results=max_results)
        ]

    def list_services(self) -> list[dict]:
        return [
            {"name": s.name, "url": s.url, "price_usdc": s.price_usdc, "tags": s.tags}
            for s in self.registry.list_all()
        ]

    def budget_status(self) -> dict:
        remaining = max(0.0, self.budget_total - self.spent)
        return {
            "budget_usdc": self.budget_total,
            "spent_usdc": round(self.spent, 6),
            "remaining_usdc": round(remaining, 6),
        }

    # ------------------------------------------------------------------
    # Pay and fetch
    # ------------------------------------------------------------------

    async def pay_and_fetch(
        self,
        service_name: str,
        method: str = "GET",
        params: Optional[dict] = None,
    ) -> dict:
        remaining = self.budget_total - self.spent
        if remaining <= 1e-9:
            return {"error": "session budget exhausted", "budget": self.budget_status()}

        try:
            service = self.registry.get(service_name)
        except Exception:
            matches = self.registry.search(service_name, max_results=1)
            if not matches:
                return {"error": f"service '{service_name}' not found"}
            service = matches[0]

        # Pre-check: don't even attempt a fetch we can't afford.
        price = float(service.price_usdc or 0)
        if remaining < price:
            return {"error": "session budget exhausted", "budget": self.budget_status()}

        client = self._client_factory(remaining)
        async with client as c:
            if method.upper() == "POST":
                resp = await c.post(service.url, json=params or {})
            else:
                resp = await c.get(service.url, params=params or {})
            summary = c.summary()

        self.spent += float(summary.get("budget", {}).get("spent_usdc", "0"))

        if resp.status_code != 200:
            return {
                "error": f"HTTP {resp.status_code}",
                "service": service.name,
                "budget": self.budget_status(),
            }

        payments = summary.get("payments", [])
        last = payments[-1] if payments else {}
        return {
            "service": service.name,
            "data": resp.json(),
            "payment": {
                "amount_usdc": last.get("amount_usdc"),
                "tx_reference": last.get("tx_reference"),
            },
            "budget": self.budget_status(),
        }

    # ------------------------------------------------------------------
    # Onchain identity (optional)
    # ------------------------------------------------------------------

    def agent_identity(self) -> Optional[dict]:
        """Resolve this wallet's ERC-8004 agent id + reputation, or None."""
        try:
            account = self._resolve_account()
            from arc_agent_pay.identity import AgentIdentity, ReputationClient

            identity = AgentIdentity(account=account)
            agent_id = identity.resolve(account.address)
            if agent_id is None:
                return None
            profile = identity.profile(agent_id, reputation=ReputationClient())
            return profile.model_dump()
        except Exception as e:  # noqa: BLE001 - identity is best-effort
            logger.warning("agent_identity unavailable: %s", e)
            return None
