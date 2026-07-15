"""
Tests for reputation-gated spending (arc_agent_pay.agent.trust.ReputationGate).

The gate decides whether to pay a provider based on its ERC-8004 reputation. A
fake reputation source is injected, so no chain access is needed.
"""

from __future__ import annotations

from arc_agent_pay.agent.trust import ReputationGate
from arc_agent_pay.models import Service


class _FakeRep:
    """Stand-in for ReputationClient: summary(agent_id) -> (count, score)."""

    def __init__(self, scores: dict[int, tuple[int, float]], *, raises: bool = False):
        self._scores = scores
        self.raises = raises
        self.calls = 0

    def summary(self, agent_id: int):
        self.calls += 1
        if self.raises:
            raise RuntimeError("rpc down")
        return self._scores[agent_id]  # KeyError == unknown provider


def _svc(name="Prov", provider_agent_id=None) -> Service:
    return Service(name=name, url=f"https://x/{name}", provider_agent_id=provider_agent_id)


# --- policy off / no-op ---

def test_inactive_gate_allows_everything():
    gate = ReputationGate(reputation=_FakeRep({}), min_reputation=0.0)
    assert gate.active is False
    d = gate.evaluate(_svc(provider_agent_id=5))
    assert d.allowed is True


# --- identity requirement ---

def test_no_identity_allowed_when_not_required():
    gate = ReputationGate(reputation=_FakeRep({}), min_reputation=3.0)
    d = gate.evaluate(_svc(provider_agent_id=None))
    assert d.allowed is True
    assert "no identity" in d.reason


def test_no_identity_denied_when_required():
    gate = ReputationGate(reputation=None, require_identity=True)
    assert gate.active is True
    d = gate.evaluate(_svc(provider_agent_id=None))
    assert d.allowed is False
    assert "no on-chain identity" in d.reason


# --- reputation floor ---

def test_reputation_above_floor_allowed():
    gate = ReputationGate(reputation=_FakeRep({7: (10, 4.5)}), min_reputation=3.0)
    d = gate.evaluate(_svc(provider_agent_id=7))
    assert d.allowed is True
    assert d.score == 4.5


def test_reputation_below_floor_denied():
    gate = ReputationGate(reputation=_FakeRep({7: (2, 1.0)}), min_reputation=3.0)
    d = gate.evaluate(_svc(provider_agent_id=7))
    assert d.allowed is False
    assert d.score == 1.0
    assert "below floor" in d.reason


def test_unrated_provider_scores_zero_and_is_denied():
    # An unrated provider reads (0, 0.0) from the registry -> below any positive floor.
    gate = ReputationGate(reputation=_FakeRep({7: (0, 0.0)}), min_reputation=1.0)
    d = gate.evaluate(_svc(provider_agent_id=7))
    assert d.allowed is False
    assert d.score == 0.0


# --- fail-open ---

def test_fail_open_when_reputation_source_missing():
    gate = ReputationGate(reputation=None, min_reputation=3.0)
    d = gate.evaluate(_svc(provider_agent_id=7))
    assert d.allowed is True
    assert "fail-open" in d.reason


def test_fail_open_when_lookup_raises():
    gate = ReputationGate(reputation=_FakeRep({}, raises=True), min_reputation=3.0)
    d = gate.evaluate(_svc(provider_agent_id=7))
    assert d.allowed is True


# --- kill switch ---

def test_kill_switch_refuses_everything():
    gate = ReputationGate(disabled=True)
    assert gate.active is True
    d = gate.evaluate(_svc(provider_agent_id=7))  # even a would-be-fine provider
    assert d.allowed is False
    assert "kill switch" in d.reason


# --- denylist ---

def test_denylisted_provider_refused():
    gate = ReputationGate(denylist={7})
    assert gate.active is True
    assert gate.evaluate(_svc(provider_agent_id=7)).allowed is False
    assert gate.evaluate(_svc(provider_agent_id=8)).allowed is True


# --- allowlist ---

def test_allowlist_permits_only_listed_providers():
    gate = ReputationGate(allowlist={7})
    assert gate.evaluate(_svc(provider_agent_id=7)).allowed is True
    assert gate.evaluate(_svc(provider_agent_id=8)).allowed is False
    # A provider with no identity can't be on the allowlist -> refused.
    assert gate.evaluate(_svc(provider_agent_id=None)).allowed is False


def test_denylist_beats_allowlist():
    gate = ReputationGate(allowlist={7}, denylist={7})
    d = gate.evaluate(_svc(provider_agent_id=7))
    assert d.allowed is False
    assert "denylisted" in d.reason


# --- caching ---

def test_score_is_cached_per_provider():
    rep = _FakeRep({7: (10, 4.5)})
    gate = ReputationGate(reputation=rep, min_reputation=3.0)
    gate.evaluate(_svc(provider_agent_id=7))
    gate.evaluate(_svc(name="Other", provider_agent_id=7))
    assert rep.calls == 1  # second evaluation hit the cache


# --- model field ---

def test_service_provider_agent_id_defaults_none():
    assert Service(name="x", url="https://x").provider_agent_id is None
    assert Service(name="x", url="https://x", provider_agent_id=42).provider_agent_id == 42
