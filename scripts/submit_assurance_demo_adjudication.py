#!/usr/bin/env python3
"""Submit or safely resume the one frozen Phase 5D GenLayer adjudication."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Mapping

from web3 import Web3

from arc_agent_pay.assurance import (
    AdjudicationPayload,
    ArcPaymentVerifier,
    AssuranceSettlementBinding,
    AssuranceStore,
    AssuranceVerdict,
    DisputeStatus,
    FinalizedGenLayerAdjudication,
    GenLayerAdjudicationClient,
    GenLayerSubmission,
    WarrantyBondClient,
    adjudication_input_hash,
)
from arc_agent_pay.assurance.arc_payment import (
    ARC_TESTNET_CHAIN_ID,
    ARC_TESTNET_USDC,
)
from arc_agent_pay.assurance.genlayer_live import (
    DEFAULT_FINALITY_TIMEOUT_SECONDS,
    STUDIO_DEV_CHAIN_ID,
    STUDIO_DEV_NETWORK,
    create_studio_dev_client,
    estimate_transaction_fees,
    load_deployment_artifact,
    load_operator_account,
    require_operator_balance,
    verify_studio_dev_network,
)
from arc_agent_pay.exceptions import GenLayerFinalityTimeout
from arc_agent_pay.onchain import rpc_url
from scripts.deploy_warranty_bond import _atomic_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DB = PROJECT_ROOT / "contracts" / "deployments" / "arc-testnet-assurance-demo-evidence.db"
DISPUTE_ARTIFACT = (
    PROJECT_ROOT / "contracts" / "deployments" / "arc-testnet-assurance-demo-dispute.json"
)
ADJUDICATION_ARTIFACT = (
    PROJECT_ROOT / "contracts" / "deployments" / "arc-testnet-assurance-demo-adjudication.json"
)
GENLAYER_ARTIFACT = PROJECT_ROOT / "genlayer" / "deployments" / "studio-dev.json"
GENLAYER_SOURCE = PROJECT_ROOT / "genlayer" / "contracts" / "AgentPayAssurance.py"

PAYMENT_ID = "assurance_cf161b91748a4d4f8b10308b35a9e174"
PAYMENT_ID_HASH = "0x909d5162f2c77c92e27ae79b1e0c1fdf73b1475972973a9416fb4c61fe914268"
EVIDENCE_HASH = "0xeeda66cc3adb4e4dc22cfb172aae7feeb39d743f6506159786f54c8ce3491a7f"
TERMS_HASH = "0x3bb78690528858d113197ab92b916646f528cdad602bda87edd03516342dd389"
DISPUTE_ID = "0x7ab7aba2fb6fd39b26e4c492b332a9f179ff609e31e6096bbb92098aabd52844"
INPUT_HASH = "0x00e255414263a5336dc2eba78f7ed71f3109a25a5839d5e8e68c5f6ee270067e"
PAYLOAD_SIZE = 1_511
GENLAYER_CONTRACT = "0x41c5de73bda3379351a2d77648cb77389841dc53"
OPERATOR = "0xAD9B0B4EDa94329c72837263979b0fD3Be4BCD18"
BUYER = "0x3Ea7d677Ee657c0903Fd0bF54c8316330848eE88"
PROVIDER = "0x5cBC83e3363710772B8812Ad3AB5423116011651"
AMOUNT_ATOMIC = 1_000_000


def _require_equal(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise RuntimeError(f"{label} mismatch: expected {expected!r}, found {actual!r}")


def _require_address(label: str, actual: str, expected: str) -> None:
    _require_equal(label, Web3.to_checksum_address(actual), Web3.to_checksum_address(expected))


def _load_frozen_sources() -> tuple[dict[str, Any], AdjudicationPayload, AssuranceStore, Any, Any]:
    if not DISPUTE_ARTIFACT.exists() or not EVIDENCE_DB.exists():
        raise RuntimeError("the frozen Phase 5D artifact and evidence store are required")
    artifact = json.loads(DISPUTE_ARTIFACT.read_text())
    locked = {
        "payment_id": PAYMENT_ID,
        "payment_id_hash": PAYMENT_ID_HASH,
        "evidence_hash": EVIDENCE_HASH,
        "terms_hash": TERMS_HASH,
        "dispute_id": DISPUTE_ID,
        "adjudication_input_hash": INPUT_HASH,
    }
    for field, expected in locked.items():
        _require_equal(f"Phase 5D {field}", artifact.get(field), expected)
    _require_equal(
        "Phase 5D GenLayer chain",
        artifact.get("genlayer", {}).get("chain_id"),
        STUDIO_DEV_CHAIN_ID,
    )
    _require_address(
        "Phase 5D GenLayer contract",
        artifact.get("genlayer", {}).get("contract_address", ""),
        GENLAYER_CONTRACT,
    )
    canonical = artifact.get("adjudication_payload_canonical")
    if not isinstance(canonical, str):
        raise RuntimeError("Phase 5D artifact has no canonical adjudication payload")
    _require_equal("canonical payload byte size", len(canonical.encode("utf-8")), PAYLOAD_SIZE)
    _require_equal("canonical payload SHA-256", adjudication_input_hash(canonical), INPUT_HASH)
    payload = AdjudicationPayload.model_validate_json(canonical)
    _require_equal("canonical payload round trip", payload.canonical, canonical)
    _require_equal("payload input hash", payload.input_hash, INPUT_HASH)

    store = AssuranceStore(EVIDENCE_DB)
    evidence = store.get_evidence_by_payment(PAYMENT_ID)
    dispute = store.get_dispute(DISPUTE_ID)
    if evidence is None or dispute is None:
        raise RuntimeError("the frozen evidence or dispute is missing from AssuranceStore")
    _require_equal("stored evidence hash", evidence.evidence_hash, EVIDENCE_HASH)
    _require_equal("stored terms hash", evidence.terms_hash, TERMS_HASH)
    _require_equal("stored dispute payment", dispute.payment_id, PAYMENT_ID)
    _require_equal("stored dispute evidence", dispute.evidence_hash, EVIDENCE_HASH)
    return artifact, payload, store, evidence, dispute


def _timing_semantics_check(payload: AdjudicationPayload, evidence: Any) -> dict[str, Any]:
    authorized_ms = int(Decimal(str(evidence.payment_authorized_at)) * 1000)
    received_ms = int(Decimal(str(evidence.paid_response_received_at)) * 1000)
    _require_equal(
        "payload authorization timestamp", payload.timing.payment_authorized_at_ms, authorized_ms
    )
    _require_equal(
        "payload response timestamp", payload.timing.paid_response_received_at_ms, received_ms
    )
    _require_equal(
        "payload delivery interval", payload.timing.delivery_elapsed_ms, received_ms - authorized_ms
    )
    _require_equal("frozen delivery interval", payload.timing.delivery_elapsed_ms, 2_908)
    _require_equal("frozen SLA threshold", payload.sla.max_response_time_ms, 2_000)
    if evidence.payment_required_at > evidence.payment_authorized_at:
        raise RuntimeError("evidence timing order is invalid")
    return {
        "metric": "payment_authorized_at_to_paid_response_received_at",
        "excludes_initial_402": True,
        "excludes_buyer_signing": True,
        "includes_paid_retry_and_provider_settlement_before_delivery": True,
        "delivery_elapsed_ms": payload.timing.delivery_elapsed_ms,
        "sla_max_response_time_ms": payload.sla.max_response_time_ms,
        "matches_frozen_demo_sla_boundary": True,
    }


def _latest_final_has_verdict(client: Any, contract: str, dispute_id: str) -> bool:
    from genlayer_py.types import TransactionHashVariant

    value = client.read_contract(
        address=Web3.to_checksum_address(contract),
        function_name="has_verdict",
        args=[dispute_id],
        transaction_hash_variant=TransactionHashVariant.LATEST_FINAL,
    )
    if not isinstance(value, bool):
        raise RuntimeError("GenLayer has_verdict returned a malformed value")
    return value


def _pending_artifact(
    *,
    transaction_id: str,
    operator_balance: int,
    fee_estimate: int,
    timing: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "phase": "5E",
        "status": "submitted",
        "payment_id": PAYMENT_ID,
        "payment_id_hash": PAYMENT_ID_HASH,
        "evidence_hash": EVIDENCE_HASH,
        "terms_hash": TERMS_HASH,
        "dispute_id": DISPUTE_ID,
        "adjudication_input_hash": INPUT_HASH,
        "phase_5d_artifact": str(DISPUTE_ARTIFACT.relative_to(PROJECT_ROOT)),
        "genlayer_contract": Web3.to_checksum_address(GENLAYER_CONTRACT),
        "genlayer_operator": Web3.to_checksum_address(OPERATOR),
        "genlayer_transaction_id": transaction_id,
        "operator_balance_before_submission_atomic_gen": operator_balance,
        "estimated_fee_atomic_gen": fee_estimate,
        "timing_semantics": timing,
        "semantic_expectation": {"sla_violated": True, "included_in_payload": False},
        "submitted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _set_adjudicating(store: AssuranceStore, dispute: Any) -> Any:
    if dispute.status == DisputeStatus.ADJUDICATING:
        return dispute
    if dispute.status != DisputeStatus.OPEN:
        return dispute
    updated = dispute.model_copy(
        update={"status": DisputeStatus.ADJUDICATING, "updated_at": time.time()}
    )
    return store.update_dispute(updated)


def _persist_final_dispute(store: AssuranceStore, dispute: Any, verdict: Any, tx_id: str) -> Any:
    target_status = DisputeStatus.UPHELD if verdict.sla_violated else DisputeStatus.REJECTED
    if dispute.status in {DisputeStatus.UPHELD, DisputeStatus.REJECTED}:
        _require_equal(
            "existing local verdict decision", dispute.verdict.sla_violated, verdict.sla_violated
        )
        _require_equal("existing local verdict tx", dispute.verdict.adjudicator_reference, tx_id)
        return dispute
    local_verdict = AssuranceVerdict(
        dispute_id=verdict.dispute_id,
        evidence_hash=verdict.evidence_hash,
        sla_violated=verdict.sla_violated,
        reason=verdict.reason,
        decided_at=time.time(),
        adjudicator_reference=tx_id,
    )
    updated_at = max(time.time(), dispute.updated_at + 0.000001)
    return store.update_dispute(
        dispute.model_copy(
            update={
                "status": target_status,
                "updated_at": updated_at,
                "verdict": local_verdict,
            }
        )
    )


def _settlement_preview(
    *,
    store: AssuranceStore,
    evidence: Any,
    dispute: Any,
    payload: AdjudicationPayload,
    submission: GenLayerSubmission,
    status: Any,
    verdict: Any,
) -> dict[str, Any]:
    w3 = Web3(Web3.HTTPProvider(rpc_url(), request_kwargs={"timeout": 30}))
    verified = ArcPaymentVerifier(w3=w3).verify(evidence)
    _require_address("fresh verified buyer", verified.buyer, BUYER)
    _require_address("fresh verified provider", verified.provider, PROVIDER)
    _require_equal("fresh verified amount", verified.amount_atomic, AMOUNT_ATOMIC)
    _require_equal("fresh verified chain", verified.chain_id, ARC_TESTNET_CHAIN_ID)
    _require_address("fresh verified asset", verified.asset, ARC_TESTNET_USDC)
    finalized = FinalizedGenLayerAdjudication(
        contract_address=GENLAYER_CONTRACT,
        submission=submission,
        transaction_status=status,
        verdict=verdict,
    )
    binding = AssuranceSettlementBinding.from_sources(
        evidence=evidence,
        dispute=dispute,
        verified_payment=verified,
        adjudication_payload=payload,
        adjudication=finalized,
        configured_genlayer_contract=GENLAYER_CONTRACT,
    )
    binding.ensure_eligible()
    if store.get_settlement(DISPUTE_ID) is not None:
        raise RuntimeError("a local settlement already exists for this dispute")
    bond = WarrantyBondClient(w3=w3)
    _require_equal("WarrantyBond Arc chain", bond.chain_id(), ARC_TESTNET_CHAIN_ID)
    _require_address("WarrantyBond resolver", bond.resolver(), OPERATOR)
    _require_address("WarrantyBond USDC", bond.usdc(), ARC_TESTNET_USDC)
    processed = bond.dispute_processed(DISPUTE_ID)
    refunded = bond.payment_refunded(PAYMENT_ID_HASH)
    balance = bond.bond_balance(PROVIDER)
    if processed or refunded:
        raise RuntimeError("WarrantyBond replay state already consumed this dispute or payment")
    if balance < AMOUNT_ATOMIC:
        raise RuntimeError("provider WarrantyBond balance is insufficient")
    return {
        "eligible": True,
        "binding_hash": binding.binding_hash,
        "fresh_arc_confirmations": verified.confirmations,
        "buyer": Web3.to_checksum_address(binding.buyer),
        "provider": Web3.to_checksum_address(binding.provider),
        "amount_atomic": binding.payment_amount,
        "asset": Web3.to_checksum_address(binding.arc_asset),
        "arc_chain_id": binding.arc_chain_id,
        "evidence_is_synthetic": False,
        "local_settlement_exists": False,
        "warranty_bond_dispute_processed": processed,
        "warranty_bond_payment_refunded": refunded,
        "provider_bond_balance": balance,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--finality-timeout-seconds",
        type=int,
        default=int(
            os.environ.get(
                "GENLAYER_FINALITY_TIMEOUT_SECONDS",
                str(DEFAULT_FINALITY_TIMEOUT_SECONDS),
            )
        ),
    )
    args = parser.parse_args()
    if args.finality_timeout_seconds <= 0:
        parser.error("--finality-timeout-seconds must be positive")
    if not os.environ.get("ASSURANCE_OPERATOR_PRIVATE_KEY", "").strip():
        raise RuntimeError("ASSURANCE_OPERATOR_PRIVATE_KEY is required")

    _phase5d, payload, store, evidence, dispute = _load_frozen_sources()
    timing = _timing_semantics_check(payload, evidence)
    deployment = load_deployment_artifact(
        GENLAYER_ARTIFACT,
        expected_network=STUDIO_DEV_NETWORK,
        source=GENLAYER_SOURCE.read_text(),
    )
    _require_address("configured GenLayer contract", deployment.contract_address, GENLAYER_CONTRACT)
    _require_address("configured GenLayer operator", deployment.authorized_submitter, OPERATOR)
    account = load_operator_account()
    operator = Web3.to_checksum_address(account.address)
    _require_address("operator signer", operator, OPERATOR)
    client = create_studio_dev_client(account)
    _require_equal("Studio-dev chain", verify_studio_dev_network(client), STUDIO_DEV_CHAIN_ID)

    existing_artifact = (
        json.loads(ADJUDICATION_ARTIFACT.read_text()) if ADJUDICATION_ARTIFACT.exists() else None
    )
    if existing_artifact is not None:
        for field, expected in (
            ("payment_id_hash", PAYMENT_ID_HASH),
            ("evidence_hash", EVIDENCE_HASH),
            ("terms_hash", TERMS_HASH),
            ("dispute_id", DISPUTE_ID),
            ("adjudication_input_hash", INPUT_HASH),
        ):
            _require_equal(f"existing Phase 5E {field}", existing_artifact.get(field), expected)
        transaction_id = existing_artifact.get("genlayer_transaction_id")
        if not isinstance(transaction_id, str):
            raise RuntimeError("existing Phase 5E artifact has no resumable transaction ID")
        submission = GenLayerSubmission(
            transaction_id=transaction_id,
            dispute_id=DISPUTE_ID,
            adjudication_input_hash=INPUT_HASH,
        )
        adapter = GenLayerAdjudicationClient(
            contract_address=GENLAYER_CONTRACT,
            client=client,
        )
        print(f"GENLAYER_ADJUDICATION_TRANSACTION_ID={transaction_id}", flush=True)
    else:
        if _latest_final_has_verdict(client, GENLAYER_CONTRACT, DISPUTE_ID):
            raise RuntimeError("LATEST_FINAL already contains this dispute without a local tx ID")
        estimate = estimate_transaction_fees(client)
        balance = require_operator_balance(client, operator, estimate)
        adapter = GenLayerAdjudicationClient(
            contract_address=GENLAYER_CONTRACT,
            client=client,
            fees=estimate.transaction_options,
        )
        submission = adapter.submit_adjudication(payload)
        print(
            f"GENLAYER_ADJUDICATION_TRANSACTION_ID={submission.transaction_id}",
            flush=True,
        )
        existing_artifact = _pending_artifact(
            transaction_id=submission.transaction_id,
            operator_balance=balance,
            fee_estimate=estimate.fee_value,
            timing=timing,
        )
        _atomic_json(ADJUDICATION_ARTIFACT, existing_artifact)
        dispute = _set_adjudicating(store, dispute)

    retries = max(1, math.ceil(args.finality_timeout_seconds / 3))
    try:
        verdict = adapter.wait_for_finalized_verdict(
            submission,
            payload,
            interval=3_000,
            retries=retries,
        )
    except GenLayerFinalityTimeout:
        print(
            "Resume with: python scripts/submit_assurance_demo_adjudication.py",
            flush=True,
        )
        raise
    status = adapter.get_transaction_status(submission.transaction_id)
    if not status.finalized or status.execution_result != "FINISHED_WITH_RETURN":
        raise RuntimeError("GenLayer transaction did not finalize with a successful return")
    transaction = client.get_transaction(transaction_hash=submission.transaction_id)
    if not isinstance(transaction, Mapping):
        raise RuntimeError("GenLayer finalized transaction lookup returned no transaction")
    tx_execution_result = transaction.get("txExecutionResult")
    tx_execution_result_name = transaction.get("txExecutionResultName")
    _require_equal("GenLayer txExecutionResult", tx_execution_result, 1)
    _require_equal(
        "GenLayer txExecutionResultName",
        tx_execution_result_name,
        "FINISHED_WITH_RETURN",
    )
    if not _latest_final_has_verdict(client, GENLAYER_CONTRACT, DISPUTE_ID):
        raise RuntimeError("LATEST_FINAL does not contain the finalized dispute verdict")

    dispute = _persist_final_dispute(store, dispute, verdict, submission.transaction_id)
    expectation_matched = verdict.sla_violated is True
    preview = None
    if verdict.sla_violated:
        preview = _settlement_preview(
            store=store,
            evidence=evidence,
            dispute=dispute,
            payload=payload,
            submission=submission,
            status=status,
            verdict=verdict,
        )

    result = dict(existing_artifact)
    result.update(
        {
            "status": "finalized",
            "lifecycle_state": status.state,
            "lifecycle_outcome": status.outcome,
            "execution_result": status.execution_result,
            "tx_execution_result": tx_execution_result,
            "tx_execution_result_name": tx_execution_result_name,
            "finalized_verdict_found": True,
            "finalized_verdict": verdict.model_dump(mode="json"),
            "sla_violated": verdict.sla_violated,
            "verdict_reason": verdict.reason,
            "all_verdict_bindings_verified": True,
            "semantic_expectation_matched": expectation_matched,
            "local_dispute_status": dispute.status.value,
            "settlement_binding_preview": preview,
            "finalized_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )
    _atomic_json(ADJUDICATION_ARTIFACT, result)
    print(json.dumps(result, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
