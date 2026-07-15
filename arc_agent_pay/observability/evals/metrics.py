"""
observability/evals/metrics.py — evaluation metrics for agent runs.

Deterministic, offline metrics (discovery accuracy, budget adherence) plus an
optional LLM-as-judge grounding score. The deterministic metrics need no API
keys or chain access, so they run anywhere (incl. CI).
"""

from __future__ import annotations

from typing import Optional


def discovery_metrics(selected_names: list[str], expected_names: list[str]) -> dict:
    """
    Precision / recall / F1 of discovered services vs. the expected set.

    precision = correct picks / picks ; recall = correct picks / expected.
    `hit` is True if at least one expected service was discovered.
    """
    selected = set(selected_names)
    expected = set(expected_names)
    correct = len(selected & expected)

    precision = correct / len(selected) if selected else 0.0
    recall = correct / len(expected) if expected else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "hit": correct > 0,
    }


def budget_adherence(payment_summary: dict, budget_usdc: str) -> dict:
    """Check that total spend stayed within the session budget."""
    spent = float(payment_summary.get("budget", {}).get("spent_usdc", "0"))
    budget = float(budget_usdc)
    return {
        "spent_usdc": spent,
        "budget_usdc": budget,
        "within_budget": spent <= budget + 1e-9,
    }


async def grounding_score(
    report: str,
    fetched_data: dict,
    provider=None,
) -> Optional[float]:
    """
    Optional LLM-as-judge: score 1-5 how well `report` is grounded in
    `fetched_data` (faithfulness). Returns None if no LLM provider is available,
    so callers can skip it cleanly in keyless environments.
    """
    import json

    from arc_agent_pay.llm import get_provider

    provider = provider or get_provider()
    if provider is None:
        return None

    prompt = (
        "You are a strict evaluator. On a scale of 1-5, how faithfully is the "
        "REPORT grounded in the DATA (5 = every claim supported, 1 = mostly "
        "invented)? Reply with ONLY the integer.\n\n"
        f"DATA:\n{json.dumps(fetched_data)[:4000]}\n\nREPORT:\n{report[:4000]}"
    )
    raw = await provider.complete(prompt, temperature=0)
    for token in raw.split():
        if token.strip().rstrip(".").isdigit():
            return float(token.strip().rstrip("."))
    return None
