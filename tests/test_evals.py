"""
Tests for the evaluation harness + observability tracing.
Deterministic and offline (keyword registry; no keys, no chain).
"""

from __future__ import annotations

from arc_agent_pay import ServiceRegistry
from arc_agent_pay.observability import get_callbacks, is_tracing_enabled
from arc_agent_pay.observability.evals import budget_adherence, discovery_metrics, grounding_score
from arc_agent_pay.observability.evals.run_evals import evaluate, load_dataset


# ---------------------------------------------------------------------------
# discovery_metrics
# ---------------------------------------------------------------------------

def test_discovery_metrics_perfect():
    m = discovery_metrics(["A"], ["A"])
    assert m["precision"] == 1.0
    assert m["recall"] == 1.0
    assert m["f1"] == 1.0
    assert m["hit"] is True


def test_discovery_metrics_partial():
    # picked 2, one correct, expected 1 → precision .5, recall 1.0
    m = discovery_metrics(["A", "B"], ["A"])
    assert m["precision"] == 0.5
    assert m["recall"] == 1.0
    assert m["hit"] is True


def test_discovery_metrics_miss():
    m = discovery_metrics(["X"], ["A"])
    assert m["hit"] is False
    assert m["f1"] == 0.0


# ---------------------------------------------------------------------------
# budget_adherence
# ---------------------------------------------------------------------------

def test_budget_adherence_within():
    summary = {"budget": {"spent_usdc": "0.04"}}
    r = budget_adherence(summary, "0.10")
    assert r["within_budget"] is True
    assert r["spent_usdc"] == 0.04


def test_budget_adherence_exceeded():
    summary = {"budget": {"spent_usdc": "0.20"}}
    assert budget_adherence(summary, "0.10")["within_budget"] is False


# ---------------------------------------------------------------------------
# grounding_score (no provider -> None)
# ---------------------------------------------------------------------------

async def test_grounding_score_none_without_provider(monkeypatch):
    monkeypatch.delenv("ARCAPIS_TOKEN_ID", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert await grounding_score("report", {"a": 1}) is None


async def test_grounding_score_with_stub_provider():
    class _Stub:
        name = "stub"

        async def complete(self, prompt, **opts):
            return "5"

    assert await grounding_score("r", {"a": 1}, provider=_Stub()) == 5.0


# ---------------------------------------------------------------------------
# end-to-end harness over the dataset (keyword registry)
# ---------------------------------------------------------------------------

def test_evaluate_over_dataset():
    dataset = load_dataset()
    assert len(dataset) >= 10
    result = evaluate(ServiceRegistry(include_builtins=True), dataset)
    assert result["n"] == len(dataset)
    # The keyword registry should find at least one expected service most of the time.
    assert result["hit_rate"] >= 0.8
    assert 0.0 <= result["precision"] <= 1.0


# ---------------------------------------------------------------------------
# tracing fallback
# ---------------------------------------------------------------------------

def test_tracing_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert is_tracing_enabled() is False
    assert get_callbacks() == []
