"""
arc_agent_pay.agent — the research agent built on the arc-agent-pay SDK.

`ResearchAgent` has two execution paths behind one stable interface:

  * graph  — a real LangGraph tool-calling agent (the headline path). Requires
             the `[agent]` extra (langgraph + langchain) and a tool-calling chat
             model (OpenAI). The LLM decides which services to discover and pay
             for.
  * linear — a dependency-light sequential pipeline (see linear.py). Runs with
             zero API keys (synthesis degrades to a template report).

By default the path is auto-selected: graph if its dependencies and an OpenAI
key are present, otherwise linear. Force it with `use_graph=True|False`.

Public surface is unchanged from earlier versions:
    agent = ResearchAgent(budget_usdc="0.10")
    report = await agent.run(topic, on_event=...)        # -> str
    state  = await agent.run_with_state(topic, on_event=...)  # -> AgentState
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Callable, Optional

from arc_agent_pay import PaymentClient, ServiceRegistry
from arc_agent_pay.models import Chain

from .linear import AgentState, run_linear

if TYPE_CHECKING:
    from .trust import ReputationGate

logger = logging.getLogger(__name__)

OnEvent = Optional[Callable[[str, dict], None]]

__all__ = ["ResearchAgent", "AgentState"]


class ResearchAgent:
    """Autonomous research agent that discovers and pays for API services."""

    def __init__(
        self,
        budget_usdc: str = "0.10",
        chain: Chain = Chain.ARC_TESTNET,
        private_key: Optional[str] = None,
        payment_signer: Any = None,
        registry: Optional[ServiceRegistry] = None,
        use_graph: Optional[bool] = None,
        max_steps: int = 6,
        min_provider_reputation: float = 0.0,
        require_provider_identity: bool = False,
        provider_allowlist: Optional[set[int]] = None,
        provider_denylist: Optional[set[int]] = None,
        payments_disabled: bool = False,
        reputation: Any = None,
        daily_cap_usdc: Optional[str] = None,
        max_payments_per_hour: Optional[int] = None,
        provider_daily_cap_usdc: Optional[str] = None,
        spend_ledger: Any = None,
    ) -> None:
        self.budget_usdc = budget_usdc
        self.chain = chain
        self.registry = registry or ServiceRegistry(include_builtins=True)
        self.use_graph = use_graph
        self.max_steps = max_steps
        self._payment_signer = payment_signer
        self._spend_ledger = self._build_ledger(
            spend_ledger,
            caps_requested=any(
                v is not None
                for v in (daily_cap_usdc, max_payments_per_hour, provider_daily_cap_usdc)
            ),
        )
        self._gate = self._build_gate(
            min_reputation=min_provider_reputation,
            require_identity=require_provider_identity,
            allowlist=provider_allowlist,
            denylist=provider_denylist,
            disabled=payments_disabled,
            reputation=reputation,
            daily_cap_usdc=daily_cap_usdc,
            max_payments_per_hour=max_payments_per_hour,
            provider_daily_cap_usdc=provider_daily_cap_usdc,
        )

        self._private_key: Optional[str] = None
        key = (private_key or os.environ.get("AGENT_PRIVATE_KEY", "")).strip()
        if self._payment_signer is None:
            if not key:
                raise EnvironmentError(
                    "AGENT_PRIVATE_KEY not set. Add it to your .env file.\n"
                    "Generate one with: python -c \"from eth_account import Account; "
                    "import secrets; a = Account.from_key('0x'+secrets.token_hex(32)); "
                    "print(a.key.hex(), a.address)\""
                )
            self._private_key = key if key.startswith("0x") else "0x" + key
        elif key:
            self._private_key = key if key.startswith("0x") else "0x" + key

    # ------------------------------------------------------------------
    # Trust policy
    # ------------------------------------------------------------------

    @staticmethod
    def _build_ledger(spend_ledger: Any, *, caps_requested: bool) -> Any:
        """Resolve the spend ledger: caller's own, else a durable sqlite one
        (only when caps are actually requested; never blocks the run)."""
        if spend_ledger is not None:
            return spend_ledger
        if not caps_requested:
            return None
        from arc_agent_pay.spending import SqliteSpendLedger, default_ledger_path

        try:
            return SqliteSpendLedger(default_ledger_path())
        except Exception as e:  # noqa: BLE001 - fail-open, caps just won't apply
            logger.warning("could not open spend ledger (caps disabled): %s", e)
            return None

    def _build_gate(
        self,
        *,
        min_reputation: float,
        require_identity: bool,
        allowlist: Optional[set[int]],
        denylist: Optional[set[int]],
        disabled: bool,
        reputation: Any,
        daily_cap_usdc: Optional[str] = None,
        max_payments_per_hour: Optional[int] = None,
        provider_daily_cap_usdc: Optional[str] = None,
    ) -> Optional["ReputationGate"]:
        """Build the spending gate if any policy is set, else None (no-op)."""
        from arc_agent_pay.spending import SpendCaps

        from .trust import ReputationGate

        rep = reputation
        # Only a reputation floor needs an on-chain client; skip the RPC setup
        # for the purely local policies (allow/deny lists, kill switch, caps).
        if rep is None and min_reputation > 0.0:
            try:
                from arc_agent_pay.identity import ReputationClient

                rep = ReputationClient()
            except Exception:  # noqa: BLE001 - onchain extra optional
                rep = None
        caps = SpendCaps(
            daily_cap_usdc=daily_cap_usdc,
            max_payments_per_hour=max_payments_per_hour,
            provider_daily_cap_usdc=provider_daily_cap_usdc,
        )
        gate = ReputationGate(
            reputation=rep,
            min_reputation=min_reputation,
            require_identity=require_identity,
            allowlist=allowlist,
            denylist=denylist,
            disabled=disabled,
            caps=caps if caps.active else None,
            spend_ledger=self._spend_ledger,
        )
        return gate if gate.active else None

    def _wrap_on_event(self, on_event: OnEvent) -> OnEvent:
        """Fold settled payments into the spend ledger, then forward the event.

        This is the single recording point for cross-run caps: both execution
        paths emit `payment_settled` through the PaymentClient they were given,
        so wrapping here covers graph and linear alike.
        """
        if self._spend_ledger is None:
            return on_event
        ledger = self._spend_ledger

        def _on_event(event: str, payload: dict) -> None:
            if event == "payment_settled":
                try:
                    ledger.record(
                        payload.get("service", ""),
                        payload.get("amount_usdc", "0"),
                        tx_reference=payload.get("tx_hash"),
                    )
                except Exception as e:  # noqa: BLE001 - recording is best-effort
                    logger.warning("spend ledger record failed: %s", e)
            if on_event:
                on_event(event, payload)

        return _on_event

    # ------------------------------------------------------------------
    # Path selection
    # ------------------------------------------------------------------

    def _graph_available(self) -> bool:
        """True if langgraph + langchain-openai are importable."""
        try:
            import langchain_openai  # noqa: F401
            import langgraph  # noqa: F401
            return True
        except ImportError:
            return False

    def _should_use_graph(self) -> bool:
        if self.use_graph is not None:
            return self.use_graph
        # Auto: the tool-calling loop needs both the libs and an OpenAI key.
        return bool(os.environ.get("OPENAI_API_KEY")) and self._graph_available()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, topic: str, on_event: OnEvent = None) -> str:
        """Run the agent and return the final Markdown report."""
        state = await self.run_with_state(topic, on_event=on_event)
        return state.report

    async def run_with_state(self, topic: str, on_event: OnEvent = None) -> AgentState:
        """Run the agent and return the full AgentState (report + audit trail)."""
        on_event = self._wrap_on_event(on_event)
        if self._should_use_graph():
            return await self._run_graph(topic, on_event)
        return await self._run_linear(topic, on_event)

    # ------------------------------------------------------------------
    # Execution paths
    # ------------------------------------------------------------------

    async def _run_linear(self, topic: str, on_event: OnEvent) -> AgentState:
        return await run_linear(
            topic,
            budget_usdc=self.budget_usdc,
            chain=self.chain,
            private_key=self._private_key,
            registry=self.registry,
            payment_signer=self._payment_signer,
            on_event=on_event,
            gate=self._gate,
        )

    async def _run_graph(self, topic: str, on_event: OnEvent) -> AgentState:
        if not self._graph_available():
            raise ImportError(
                "The graph agent requires LangGraph + LangChain.\n"
                'Install with: pip install "arc-agent-pay[agent]"'
            )

        from eth_account import Account
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI

        from arc_agent_pay.llm import get_provider
        from arc_agent_pay.observability import get_callbacks
        from .graph import build_graph
        from .nodes import build_system_prompt

        account = Account.from_key(self._private_key) if self._private_key else None
        provider = get_provider()
        llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

        collected: dict = {}
        failed: list[str] = []
        state = AgentState(topic=topic, budget_usdc=self.budget_usdc)

        async with PaymentClient(
            account=account,
            signer=self._payment_signer,
            budget_usdc=self.budget_usdc,
            chain=self.chain,
            on_event=on_event,
        ) as client:
            graph = build_graph(
                client=client,
                registry=self.registry,
                topic=topic,
                llm=llm,
                collected=collected,
                failed=failed,
                provider=provider,
                on_event=on_event,
                max_steps=self.max_steps,
                gate=self._gate,
            )
            seed = {
                "topic": topic,
                "budget_usdc": self.budget_usdc,
                "messages": [
                    SystemMessage(build_system_prompt(topic, self.budget_usdc)),
                    HumanMessage(f"Research this topic: {topic}"),
                ],
                "report": "",
                "steps_left": self.max_steps,
            }
            result = await graph.ainvoke(
                seed,
                {"recursion_limit": self.max_steps * 4, "callbacks": get_callbacks()},
            )
            state.payment_summary = client.summary()

        state.fetched_data = collected
        state.failed_services = failed
        state.report = result.get("report", "")
        return state
