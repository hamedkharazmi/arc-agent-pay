"""
observability/evals/run_evals.py — offline evaluation of service discovery.

Runs the discovery layer over a labelled dataset and reports precision / recall /
F1 / hit-rate, comparing the keyword registry against the semantic (RAG) one.
Deterministic and offline — no API keys, no chain, no USDC spent.

Usage:
    python -m arc_agent_pay.observability.evals.run_evals
    python -m arc_agent_pay.observability.evals.run_evals --semantic
    python -m arc_agent_pay.observability.evals.run_evals --json out.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean

from arc_agent_pay import SemanticServiceRegistry, ServiceRegistry

from .metrics import discovery_metrics

DATASET = Path(__file__).parent / "dataset.jsonl"


def load_dataset(path: Path = DATASET) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def evaluate(registry, dataset: list[dict], max_results: int = 3) -> dict:
    """Run discovery over the dataset and aggregate metrics."""
    rows = []
    for case in dataset:
        selected = [s.name for s in registry.search(case["topic"], max_results=max_results)]
        m = discovery_metrics(selected, case["expected_services"])
        rows.append({"topic": case["topic"], "selected": selected, **m})

    return {
        "n": len(rows),
        "precision": mean(r["precision"] for r in rows) if rows else 0.0,
        "recall": mean(r["recall"] for r in rows) if rows else 0.0,
        "f1": mean(r["f1"] for r in rows) if rows else 0.0,
        "hit_rate": (sum(r["hit"] for r in rows) / len(rows)) if rows else 0.0,
        "rows": rows,
    }


def _print_summary(label: str, result: dict) -> None:
    print(f"\n{label}  (n={result['n']})")
    print(f"  precision : {result['precision']:.3f}")
    print(f"  recall    : {result['recall']:.3f}")
    print(f"  f1        : {result['f1']:.3f}")
    print(f"  hit_rate  : {result['hit_rate']:.3f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate arc-agent-pay service discovery")
    parser.add_argument("--semantic", action="store_true", help="Use SemanticServiceRegistry")
    parser.add_argument("--json", dest="json_out", help="Write full results to this JSON file")
    args = parser.parse_args()

    dataset = load_dataset()
    base = ServiceRegistry(include_builtins=True)
    registry = SemanticServiceRegistry(base) if args.semantic else base
    label = "semantic" if args.semantic else "keyword"

    result = evaluate(registry, dataset)
    _print_summary(f"discovery [{label}]", result)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({label: result}, indent=2))
        print(f"\nWrote {args.json_out}")


if __name__ == "__main__":
    main()
