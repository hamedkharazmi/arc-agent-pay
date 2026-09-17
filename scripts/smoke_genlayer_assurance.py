#!/usr/bin/env python3
"""Run one GOOD and one BAD synthetic Assurance adjudication on Studio-dev."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from arc_agent_pay import AssuranceTerms, Chain, PaymentExchange, Service
from arc_agent_pay.assurance import AssuranceEvidence
from arc_agent_pay.assurance.genlayer import (
    AdjudicationPayload,
    GenLayerAdjudicationClient,
    GenLayerSubmission,
    adjudication_input_hash,
)
from arc_agent_pay.assurance.genlayer_live import (
    DEFAULT_FINALITY_TIMEOUT_SECONDS,
    STUDIO_DEV_NETWORK,
    create_studio_dev_client,
    estimate_transaction_fees,
    load_deployment_artifact,
    load_operator_account,
    query_native_balance,
    require_operator_balance,
    verify_studio_dev_network,
)
from arc_agent_pay.exceptions import (
    GenLayerAdjudicationError,
    GenLayerDeploymentError,
    GenLayerFinalityTimeout,
    GenLayerSubmissionOutcomeUnknown,
    GenLayerTransportRetryExhausted,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "genlayer" / "contracts" / "AgentPayAssurance.py"
ARTIFACT_PATH = PROJECT_ROOT / "genlayer" / "deployments" / "studio-dev.json"
FIXTURE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "assurance_market_brief.json"

SYNTHETIC_PROVIDER = "0x" + "a1" * 20
SYNTHETIC_BOND = "0x" + "b2" * 20
SYNTHETIC_ASSET = "0x3600000000000000000000000000000000000000"


def _timeout_default() -> int:
    raw = os.environ.get(
        "GENLAYER_FINALITY_TIMEOUT_SECONDS",
        str(DEFAULT_FINALITY_TIMEOUT_SECONDS),
    )
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("GENLAYER_FINALITY_TIMEOUT_SECONDS must be an integer") from exc
    if value <= 0:
        raise ValueError("GENLAYER_FINALITY_TIMEOUT_SECONDS must be positive")
    return value


def _synthetic_hash(label: str) -> str:
    return "0x" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def build_synthetic_smoke_payload(
    case: Literal["good", "bad"],
) -> AdjudicationPayload:
    """Build deterministic GenLayer-only evidence; never valid for WarrantyBond."""
    fixture = json.loads(FIXTURE_PATH.read_text())
    response = fixture[f"{case}_response"]
    payment_id = f"synthetic-genlayer-smoke-{case}-v1"
    service = Service(
        name=fixture["service_name"],
        url="https://synthetic.invalid/market-brief",
        description=fixture["service_description"],
        price_usdc="0.25",
        chain=Chain.ARC_TESTNET,
        assurance=AssuranceTerms(
            provider_address=SYNTHETIC_PROVIDER,
            warranty_bond_address=SYNTHETIC_BOND,
            acceptance_criteria=fixture["acceptance_criteria"],
            max_response_time_ms=2_000,
            dispute_window_seconds=3_600,
        ),
    )
    transaction_reference = _synthetic_hash(f"{payment_id}:not-an-arc-transaction")
    exchange = PaymentExchange(
        payment_id=payment_id,
        service_url=service.url,
        amount_usdc="0.25",
        amount_atomic="250000",
        chain="ARC-TESTNET",
        network="eip155:5042002",
        asset=SYNTHETIC_ASSET,
        pay_to=SYNTHETIC_PROVIDER,
        scheme="exact",
        payment_status="unknown",
        request_method="POST",
        request_url=service.url,
        request_body=json.dumps(
            {"prompt": fixture["request"]},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        request_headers={"content-type": "application/json"},
        parsed_payment_required={"synthetic": True, "x402Version": 2},
        selected_requirement={
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": SYNTHETIC_ASSET,
            "payTo": SYNTHETIC_PROVIDER,
            "amount": "250000",
        },
        original_payment_required="synthetic-genlayer-smoke-only",
        payment_payload_hash=_synthetic_hash(f"{payment_id}:payment-payload"),
        request_started_at=1_900_000_000.0,
        payment_required_at=1_900_000_000.1,
        payment_authorized_at=1_900_000_000.2,
        paid_response_received_at=1_900_000_001.0,
        paid_response_status=200,
        paid_response_body=response.encode(),
        paid_response_headers={"content-type": "text/plain"},
        raw_payment_response="synthetic-settlement",
        settlement_response={"transaction": transaction_reference},
        transaction_reference=transaction_reference,
    )
    evidence = AssuranceEvidence.from_exchange(exchange, service=service)
    allegation = (
        "Synthetic smoke allegation: verify the response against every frozen criterion."
    )
    return AdjudicationPayload.from_evidence(
        evidence,
        buyer_dispute_reason=allegation,
    )


def _run_case(
    case: Literal["good", "bad"],
    *,
    raw_client,
    contract_address: str,
    operator_address: str,
    timeout_seconds: int,
    resume_transaction_id: str | None,
) -> dict[str, object]:
    payload = build_synthetic_smoke_payload(case)
    independently_computed_hash = adjudication_input_hash(payload.canonical)
    if independently_computed_hash != payload.input_hash:
        raise RuntimeError("local adjudication input hash mismatch")

    if resume_transaction_id:
        estimate = None
        balance = query_native_balance(raw_client, operator_address)
        adapter = GenLayerAdjudicationClient(
            contract_address=contract_address,
            client=raw_client,
        )
        submission = GenLayerSubmission(
            transaction_id=resume_transaction_id,
            dispute_id=payload.dispute_id,
            adjudication_input_hash=payload.input_hash,
        )
        operation = "resume"
    else:
        estimate = estimate_transaction_fees(raw_client)
        balance = require_operator_balance(raw_client, operator_address, estimate)
        adapter = GenLayerAdjudicationClient(
            contract_address=contract_address,
            client=raw_client,
            fees=estimate.transaction_options,
        )
        submission = adapter.submit_adjudication(payload)
        operation = "submit"
    print(
        json.dumps(
            {
                "case": case.upper(),
                "synthetic": True,
                "never_use_for_warranty_bond": True,
                "operation": operation,
                "operator_balance_atomic_gen": balance,
                "fee_estimate": estimate.summary if estimate else None,
                "transaction_id": submission.transaction_id,
                "adjudication_input_hash": independently_computed_hash,
            },
            indent=2,
        ),
        flush=True,
    )
    retries = max(1, (timeout_seconds + 2) // 3)
    verdict = adapter.wait_for_finalized_verdict(
        submission,
        payload,
        interval=3_000,
        retries=retries,
    )
    expected = case == "bad"
    result = {
        "case": case.upper(),
        "synthetic": True,
        "transaction_id": submission.transaction_id,
        "contract_address": contract_address,
        "final_status": "finalized",
        "sla_violated": verdict.sla_violated,
        "semantic_expectation": expected,
        "matches_semantic_expectation": verdict.sla_violated == expected,
        "reason": verdict.reason,
        "bindings_verified": True,
    }
    print(json.dumps(result, indent=2), flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", required=True, choices=[STUDIO_DEV_NETWORK])
    parser.add_argument("--resume-good-transaction-id")
    parser.add_argument("--resume-bad-transaction-id")
    parser.add_argument(
        "--finality-timeout-seconds",
        type=int,
        default=_timeout_default(),
    )
    args = parser.parse_args()
    if args.finality_timeout_seconds <= 0:
        parser.error("--finality-timeout-seconds must be positive")

    source = CONTRACT_PATH.read_text()
    artifact = load_deployment_artifact(
        ARTIFACT_PATH,
        expected_network=args.network,
        source=source,
    )
    account = load_operator_account()
    operator = account.address
    if operator.lower() != artifact.authorized_submitter:
        parser.error(
            "ASSURANCE_OPERATOR_PRIVATE_KEY does not match the deployment's "
            "authorized submitter"
        )
    raw_client = create_studio_dev_client(account)
    verify_studio_dev_network(raw_client)

    cases: list[tuple[Literal["good", "bad"], str | None]] = []
    if args.resume_good_transaction_id or not args.resume_bad_transaction_id:
        cases.append(("good", args.resume_good_transaction_id))
    cases.append(("bad", args.resume_bad_transaction_id))

    for case, resume_transaction_id in cases:
        try:
            _run_case(
                case,
                raw_client=raw_client,
                contract_address=artifact.contract_address,
                operator_address=operator,
                timeout_seconds=args.finality_timeout_seconds,
                resume_transaction_id=resume_transaction_id,
            )
        except GenLayerFinalityTimeout as exc:
            resume_flag = f"--resume-{case}-transaction-id"
            print(
                json.dumps(
                    {
                        "case": case.upper(),
                        "status": "client-wait-timeout",
                        "transaction_id": exc.transaction_id,
                        "last_state": exc.state,
                        "resume_command": (
                            "python scripts/smoke_genlayer_assurance.py "
                            f"--network studio-dev {resume_flag} {exc.transaction_id}"
                        ),
                    },
                    indent=2,
                ),
                flush=True,
            )
            raise SystemExit(2) from exc


if __name__ == "__main__":
    try:
        main()
    except GenLayerSubmissionOutcomeUnknown as exc:
        raise SystemExit(
            f"error: {exc}. No adjudication retry was attempted; inspect the operator "
            "nonce/transactions before deciding whether a fresh submission is safe."
        ) from exc
    except GenLayerTransportRetryExhausted as exc:
        raise SystemExit(f"error: {exc}") from exc
    except GenLayerDeploymentError as exc:
        raise SystemExit(f"error: {exc}") from exc
    except GenLayerAdjudicationError as exc:
        raise SystemExit(f"error: {exc}") from exc
