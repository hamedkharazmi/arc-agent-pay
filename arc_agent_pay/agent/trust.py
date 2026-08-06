"""
agent/trust.py — reputation-gated spending.

Before paying a provider, an agent can check the provider's ERC-8004 reputation
and refuse anyone below a configurable floor. This is the production use of the
Reputation Registry: spend only with counterparties you can verify on-chain.

The gate is **off by default** and **fail-open**: with no policy set — or when a
provider advertises no on-chain identity, or reputation can't be read — payments
proceed unchanged, so enabling it never silently breaks a run.
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple, Optional

from ..models import Service
from ..spending import SpendCaps

logger = logging.getLogger(__name__)


class GateDecision(NamedTuple):
    """Outcome of a trust check for one provider."""

    allowed: bool
    score: Optional[float]  # provider reputation, if it could be read
    reason: str


class ReputationGate:
    """Decide whether to pay a provider — the agent's spending policy.

    Applies, in order: a global kill switch, a provider denylist, an allowlist,
    cross-run spending caps, an on-chain-identity requirement, and an ERC-8004
    reputation floor. Identity checks are keyed on the provider's
    `provider_agent_id`; spending caps are keyed on the service URL host.

    Args:
        reputation: a ReputationClient (or anything with `summary(agent_id) ->
            (count, score)`). Injected for tests; may be None (fail-open).
        min_reputation: minimum aggregate score to allow payment. 0 disables the
            floor.
        require_identity: if True, refuse providers that advertise no on-chain
            identity at all.
        allowlist: if non-empty, only providers whose id is listed may be paid.
        denylist: providers whose id is listed are always refused.
        disabled: kill switch — refuse every payment.
        caps: cross-run SpendCaps (daily / velocity / per-counterparty).
            Requires `spend_ledger`; ignored without one.
        spend_ledger: the SpendLedger the caps are checked against.
    """

    def __init__(
        self,
        *,
        reputation: Any = None,
        min_reputation: float = 0.0,
        require_identity: bool = False,
        allowlist: Optional[set[int]] = None,
        denylist: Optional[set[int]] = None,
        disabled: bool = False,
        caps: Optional[SpendCaps] = None,
        spend_ledger: Any = None,
    ) -> None:
        self._rep = reputation
        self.min_reputation = float(min_reputation)
        self.require_identity = bool(require_identity)
        self.allowlist = {int(a) for a in allowlist} if allowlist else set()
        self.denylist = {int(d) for d in denylist} if denylist else set()
        self.disabled = bool(disabled)
        self.caps = caps if (caps is not None and caps.active) else None
        self.spend_ledger = spend_ledger
        if self.caps is not None and self.spend_ledger is None:
            logger.warning("spend caps configured without a ledger — caps will not be enforced")
            self.caps = None
        self._cache: dict[int, float] = {}

    @property
    def active(self) -> bool:
        """True when any policy is set (otherwise evaluate() is a no-op allow)."""
        return (
            self.disabled
            or self.min_reputation > 0.0
            or self.require_identity
            or bool(self.allowlist)
            or bool(self.denylist)
            or self.caps is not None
        )

    def evaluate(self, service: Service) -> GateDecision:
        """Return whether `service`'s provider may be paid."""
        if not self.active:
            return GateDecision(True, None, "trust policy off")

        # Kill switch: refuse everything.
        if self.disabled:
            return GateDecision(False, None, "payments disabled (kill switch)")

        provider_id = None if service.provider_agent_id is None else int(service.provider_agent_id)

        # Denylist (explicit block).
        if provider_id is not None and provider_id in self.denylist:
            return GateDecision(False, None, f"provider {provider_id} is denylisted")

        # Allowlist: when set, only listed providers may be paid.
        if self.allowlist and provider_id not in self.allowlist:
            return GateDecision(False, None, "provider not in allowlist")

        # Cross-run spending caps. Checked before the identity/reputation
        # branches because those can early-allow providers without an on-chain
        # identity — caps must bound every payment regardless.
        if self.caps is not None:
            reason = self.caps.check(self.spend_ledger, service.url)
            if reason is not None:
                return GateDecision(False, None, reason)

        # Identity requirement.
        if provider_id is None:
            if self.require_identity:
                return GateDecision(False, None, "provider advertises no on-chain identity")
            return GateDecision(True, None, "provider has no identity; not gated")

        # Reputation floor.
        if self.min_reputation > 0.0:
            score = self._score(provider_id)
            if score is None:
                # No reputation source / RPC error -> don't block the run.
                return GateDecision(True, None, "reputation unavailable; allowed (fail-open)")
            if score < self.min_reputation:
                return GateDecision(
                    False, score, f"reputation {score:.1f} below floor {self.min_reputation:.1f}"
                )
            return GateDecision(
                True, score, f"reputation {score:.1f} meets floor {self.min_reputation:.1f}"
            )

        return GateDecision(True, None, "allowed")

    def _score(self, agent_id: int) -> Optional[float]:
        """Cached, best-effort reputation lookup (per gate instance / run)."""
        if agent_id in self._cache:
            return self._cache[agent_id]
        if self._rep is None:
            return None
        try:
            _count, score = self._rep.summary(agent_id)
            score = float(score)
        except Exception as e:  # noqa: BLE001 - trust check is best-effort
            logger.debug("reputation lookup for provider agent %s failed: %s", agent_id, e)
            return None
        self._cache[agent_id] = score
        return score
