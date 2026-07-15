"""
agent/tools.py — LangChain tools that wrap the arc-agent-pay SDK.

The tools close over a live PaymentClient + ServiceRegistry so the LLM can
discover services and pay-and-fetch from them. Payment (402 → sign → retry) and
budget enforcement happen transparently inside PaymentClient — the model never
sees a wallet or a signature.

Side outputs (the actual fetched payloads and the names of failed services) are
written into the `collected` / `failed` containers passed in by the caller, so
the graph can build the final report without threading large blobs through the
message state.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from arc_agent_pay.interceptor import PaymentClient
from arc_agent_pay.registry import ServiceRegistry

from .linear import fetch_service_data
from .trust import ReputationGate

logger = logging.getLogger(__name__)


def build_tools(
    *,
    client: PaymentClient,
    registry: ServiceRegistry,
    topic: str,
    collected: dict[str, Any],
    failed: list[str],
    on_event: Optional[Callable[[str, dict], None]] = None,
    gate: Optional[ReputationGate] = None,
) -> list:
    """Return the list of LangChain tools bound to this run's client/registry."""
    from langchain_core.tools import tool

    def _emit(event: str, payload: dict) -> None:
        if on_event:
            on_event(event, payload)

    @tool
    async def discover_services(query: str) -> str:
        """Find paid API services relevant to a query.

        Returns a JSON list of services, each with name, url, price_usdc, tags,
        and description. Call this first to learn what data sources exist.
        """
        results = registry.search(query, max_results=3)
        if not results:
            for word in query.lower().split():
                results = registry.search(word, max_results=2)
                if results:
                    break

        payload = [
            {
                "name": s.name,
                "url": s.url,
                "price_usdc": s.price_usdc,
                "tags": s.tags,
                "description": s.description,
            }
            for s in results
        ]
        _emit("services_discovered", {"services": payload})
        return json.dumps(payload) if payload else "No services found for that query."

    @tool
    async def fetch_service(service_name: str) -> str:
        """Pay for and fetch data from a discovered service by its exact name.

        Payment is automatic (signed x402 / EIP-3009) and bounded by the session
        budget. Returns the fetched data as JSON, or an error message the agent
        can react to (e.g. budget exhausted or service unavailable).
        """
        try:
            service = registry.get(service_name)
        except Exception:
            matches = registry.search(service_name, max_results=1)
            if not matches:
                return f"Service '{service_name}' not found. Call discover_services first."
            service = matches[0]

        # Trust gate: refuse to pay a provider whose on-chain reputation is below
        # the configured floor (no-op unless a policy is set).
        if gate is not None and gate.active:
            decision = gate.evaluate(service)
            if not decision.allowed:
                _emit("provider_skipped", {
                    "service": service.name,
                    "provider_agent_id": service.provider_agent_id,
                    "reputation_score": decision.score,
                    "min_reputation": gate.min_reputation,
                    "reason": decision.reason,
                })
                logger.info("[trust] skipped %s — %s", service.name, decision.reason)
                return (
                    f"Skipped '{service.name}' without paying: {decision.reason}. "
                    "Try another provider."
                )

        _emit("fetch_started", {"service": service.name, "url": service.url})
        try:
            resp = await fetch_service_data(client, service, topic)
        except Exception as e:  # noqa: BLE001 - surface to the model, keep the loop alive
            logger.warning("[fetch] %s failed: %s", service.name, e)
            failed.append(service.name)
            return f"Could not fetch '{service.name}': {e}"

        if resp.status_code == 200:
            data = resp.json()
            collected[service.name] = data
            _emit("fetch_completed", {
                "service": service.name,
                "status": 200,
                # Surface data provenance so the UI can flag synthetic data.
                "provenance": data.get("provenance") if isinstance(data, dict) else None,
            })
            # Truncate the payload returned to the model; the full data is kept
            # in `collected` for synthesis.
            return json.dumps(data)[:1500]

        failed.append(service.name)
        _emit("fetch_completed", {"service": service.name, "status": resp.status_code})
        return f"Fetch failed for '{service.name}' with HTTP {resp.status_code}."

    return [discover_services, fetch_service]
