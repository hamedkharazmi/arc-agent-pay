"""
agent/linear.py — the dependency-light research pipeline.

A straightforward, sequential plan → fetch → synthesize pipeline with no LLM
planning and no extra dependencies beyond the core SDK. It is the fallback used
when LangGraph / a tool-calling LLM is not available, and it keeps the demo
runnable with zero API keys (the synthesis step degrades to a template report).

The real LLM-driven, tool-calling agent lives in `graph.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from arc_agent_pay import PaymentClient, ServiceRegistry
from arc_agent_pay.llm import get_provider, synthesize_report
from arc_agent_pay.models import Chain, Service

if TYPE_CHECKING:
    from .trust import ReputationGate

logger = logging.getLogger(__name__)

OnEvent = Optional[Callable[[str, dict], None]]


# ---------------------------------------------------------------------------
# Shared agent state (returned by both the linear and graph paths)
# ---------------------------------------------------------------------------

@dataclass
class AgentState:
    """Carries all data through a research run (plan → fetch → synthesize)."""

    topic: str
    budget_usdc: str = "0.10"
    selected_services: list[Service] = field(default_factory=list)
    fetched_data: dict[str, Any] = field(default_factory=dict)
    failed_services: list[str] = field(default_factory=list)
    report: str = ""
    payment_summary: dict = field(default_factory=dict)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Shared helpers (used by both the linear pipeline and the graph nodes)
# ---------------------------------------------------------------------------

def topic_to_category(topic: str) -> str:
    """Map a free-text topic to a news category the /news endpoint understands."""
    t = topic.lower()
    if any(w in t for w in ["crypto", "bitcoin", "eth", "usdc", "arc", "chain"]):
        return "crypto"
    if any(w in t for w in ["finance", "money", "market", "stock", "revenue"]):
        return "finance"
    return "tech"


def report_footer(payment_summary: dict) -> str:
    """Build the standard '... N API calls | Total spent ...' report footer."""
    spent = payment_summary.get("budget", {}).get("spent_usdc", "0")
    count = payment_summary.get("payment_count", 0)
    return (
        f"\n---\n"
        f"*Powered by arc-agent-pay | {count} API call(s) | Total spent: {spent} USDC*"
    )


async def fetch_service_data(client: PaymentClient, service: Service, topic: str):
    """
    Call a single service, routing the request shape by its URL.
    Payment (402 → sign → retry) is handled transparently by PaymentClient.
    Returns the httpx response.
    """
    if "research" in service.url:
        return await client.post(service.url, json={"topic": topic})
    if "news" in service.url:
        return await client.get(service.url, params={"category": topic_to_category(topic)})
    if "whales" in service.url:
        return await client.get(service.url, params={"token": "USDC"})
    return await client.get(service.url)


# ---------------------------------------------------------------------------
# Linear pipeline
# ---------------------------------------------------------------------------

def _plan(registry: ServiceRegistry, topic: str) -> list[Service]:
    results = registry.search(topic, max_results=3)
    if not results:
        for word in topic.lower().split():
            results = registry.search(word, max_results=2)
            if results:
                break
    return results


async def run_linear(
    topic: str,
    *,
    budget_usdc: str,
    chain: Chain,
    private_key: Optional[str],
    registry: ServiceRegistry,
    payment_signer: Any = None,
    on_event: OnEvent = None,
    gate: Optional["ReputationGate"] = None,
) -> AgentState:
    """Execute the sequential plan → fetch → synthesize pipeline."""
    state = AgentState(topic=topic, budget_usdc=budget_usdc)
    account = None
    if payment_signer is None:
        if not private_key:
            raise ValueError("private_key is required when payment_signer is not provided.")
        from eth_account import Account

        account = Account.from_key(private_key)

    # --- plan ---
    state.selected_services = _plan(registry, topic)
    if not state.selected_services:
        state.error = f"No services matched '{topic}'"
        logger.warning("[plan] %s", state.error)
    if on_event:
        on_event("services_discovered", {
            "services": [
                {"name": s.name, "url": s.url, "price_usdc": s.price_usdc, "tags": s.tags}
                for s in state.selected_services
            ]
        })

    # --- fetch ---
    async with PaymentClient(
        account=account,
        signer=payment_signer,
        budget_usdc=budget_usdc,
        chain=chain,
        on_event=on_event,
    ) as client:
        for service in state.selected_services:
            # Trust gate: skip providers below the reputation floor (no-op unless set).
            if gate is not None and gate.active:
                decision = gate.evaluate(service)
                if not decision.allowed:
                    state.failed_services.append(service.name)
                    if on_event:
                        on_event("provider_skipped", {
                            "service": service.name,
                            "provider_agent_id": service.provider_agent_id,
                            "reputation_score": decision.score,
                            "min_reputation": gate.min_reputation,
                            "reason": decision.reason,
                        })
                    logger.info("[trust] skipped %s — %s", service.name, decision.reason)
                    continue
            if on_event:
                on_event("fetch_started", {"service": service.name, "url": service.url})
            try:
                resp = await fetch_service_data(client, service, topic)
                if resp.status_code == 200:
                    data = resp.json()
                    state.fetched_data[service.name] = data
                    if on_event:
                        on_event("fetch_completed", {
                            "service": service.name,
                            "status": 200,
                            # Surface data provenance so the UI can flag synthetic data.
                            "provenance": data.get("provenance") if isinstance(data, dict) else None,
                        })
                else:
                    state.failed_services.append(service.name)
                    if on_event:
                        on_event("fetch_completed",
                                 {"service": service.name, "status": resp.status_code})
            except Exception as e:  # noqa: BLE001 - record + continue
                logger.error("[fetch] %s failed: %s", service.name, e)
                state.failed_services.append(service.name)
        state.payment_summary = client.summary()

    # --- synthesize ---
    if not state.fetched_data and not state.error:
        state.report = f"No data was fetched for topic: '{topic}'"
        return state

    resolved = get_provider()
    provider_name = resolved.name if resolved else "template"
    if on_event:
        on_event(
            "synthesis_started",
            {"note": "Synthesizing report", "provider": provider_name},
        )
    body = await synthesize_report(topic, state.fetched_data, resolved)
    state.report = body + report_footer(state.payment_summary)
    if on_event:
        payload = {"markdown": state.report, "provider": provider_name}
        packet = getattr(resolved, "last_call", None)
        if packet:
            payload["packet"] = packet
        on_event("report_completed", payload)

    return state
