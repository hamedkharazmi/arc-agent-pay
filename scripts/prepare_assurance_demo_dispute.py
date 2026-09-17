#!/usr/bin/env python3
"""Prepare the real Phase 5C payment for GenLayer adjudication without submitting it."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any

from web3 import Web3

from arc_agent_pay.assurance import (
    AdjudicationPayload,
    ArcPaymentVerifier,
    AssuranceStore,
    Dispute,
    DisputeStatus,
    adjudication_input_hash,
    dispute_id,
    payment_id_hash,
)
from arc_agent_pay.assurance.arc_payment import (
    ARC_TESTNET_CHAIN_ID,
    ARC_TESTNET_NETWORK,
    ARC_TESTNET_USDC,
)
from arc_agent_pay.assurance.genlayer_live import (
    STUDIO_DEV_CHAIN_ID,
    STUDIO_DEV_NETWORK,
    create_studio_dev_client,
    estimate_transaction_fees,
    load_deployment_artifact,
    load_operator_account,
    require_operator_balance,
    verify_studio_dev_network,
)
from arc_agent_pay.onchain import rpc_url
from scripts.deploy_warranty_bond import _atomic_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DB = PROJECT_ROOT / "contracts" / "deployments" / "arc-testnet-assurance-demo-evidence.db"
PAYMENT_ARTIFACT = (
    PROJECT_ROOT / "contracts" / "deployments" / "arc-testnet-assurance-demo-payment.json"
)
DISPUTE_ARTIFACT = (
    PROJECT_ROOT / "contracts" / "deployments" / "arc-testnet-assurance-demo-dispute.json"
)
GENLAYER_ARTIFACT = PROJECT_ROOT / "genlayer" / "deployments" / "studio-dev.json"
GENLAYER_CONTRACT_SOURCE = PROJECT_ROOT / "genlayer" / "contracts" / "AgentPayAssurance.py"

PAYMENT_ID = "assurance_cf161b91748a4d4f8b10308b35a9e174"
PAYMENT_ID_HASH = "0x909d5162f2c77c92e27ae79b1e0c1fdf73b1475972973a9416fb4c61fe914268"
EVIDENCE_HASH = "0xeeda66cc3adb4e4dc22cfb172aae7feeb39d743f6506159786f54c8ce3491a7f"
TERMS_HASH = "0x3bb78690528858d113197ab92b916646f528cdad602bda87edd03516342dd389"
ARC_PAYMENT_TX = "0xbd863ffbe75ca3e9357538790b3929acecd27fd8e6bf9f515d0a263a1f135617"
ARC_PAYMENT_BLOCK = 62_517_041
BUYER = "0x3Ea7d677Ee657c0903Fd0bF54c8316330848eE88"
PROVIDER = "0x5cBC83e3363710772B8812Ad3AB5423116011651"
AMOUNT_ATOMIC = 1_000_000
GENLAYER_CONTRACT = "0x41c5de73bda3379351a2d77648cb77389841dc53"
OPERATOR = "0xAD9B0B4EDa94329c72837263979b0fD3Be4BCD18"
ALLEGATION = (
    "The paid response violated the agreed SLA because it did not cover BTC, ETH, or SOL, "
    "did not distinguish facts from analysis, and did not include a risk or caveat."
)


def _require_equal(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise RuntimeError(f"{label} mismatch: expected {expected!r}, found {actual!r}")


def _require_address(label: str, actual: str, expected: str) -> None:
    _require_equal(
        label,
        Web3.to_checksum_address(actual),
        Web3.to_checksum_address(expected),
    )


def _load_and_validate_sources() -> tuple[AssuranceStore, Any, dict[str, Any]]:
    if not EVIDENCE_DB.exists() or not PAYMENT_ARTIFACT.exists():
        raise RuntimeError("Phase 5C evidence store and payment artifact are required")
    store = AssuranceStore(EVIDENCE_DB)
    evidence = store.get_evidence_by_payment(PAYMENT_ID)
    if evidence is None:
        raise RuntimeError("the locked Phase 5C AssuranceEvidence is not persisted")
    payment = json.loads(PAYMENT_ARTIFACT.read_text())
    _require_equal("payment artifact status", payment.get("status"), "verified")
    _require_equal("evidence payment_id", evidence.payment_id, PAYMENT_ID)
    _require_equal("derived payment_id_hash", payment_id_hash(evidence.payment_id), PAYMENT_ID_HASH)
    _require_equal("evidence hash", evidence.evidence_hash, EVIDENCE_HASH)
    _require_equal("evidence ID", evidence.evidence_id, EVIDENCE_HASH)
    _require_equal("terms hash", evidence.terms_hash, TERMS_HASH)
    _require_equal("Arc settlement transaction", evidence.arc_transaction_reference, ARC_PAYMENT_TX)
    _require_equal("artifact payment ID hash", payment.get("payment_id_hash"), PAYMENT_ID_HASH)
    _require_equal("artifact evidence hash", payment.get("evidence_hash"), EVIDENCE_HASH)
    _require_equal("artifact terms hash", payment.get("terms_hash"), TERMS_HASH)
    _require_equal("artifact Arc transaction", payment.get("arc_transaction_hash"), ARC_PAYMENT_TX)
    _require_equal("artifact Arc block", payment.get("arc_block_number"), ARC_PAYMENT_BLOCK)

    quote = json.loads(evidence.selected_x402_requirement)
    _require_equal("x402 network", quote.get("network"), ARC_TESTNET_NETWORK)
    _require_address("x402 asset", quote.get("asset", ""), ARC_TESTNET_USDC)
    _require_address("x402 payTo", quote.get("payTo", ""), PROVIDER)
    _require_equal("x402 amount", quote.get("amount"), str(AMOUNT_ATOMIC))
    service = json.loads(evidence.service_snapshot)
    _require_address(
        "SLA provider",
        service.get("assurance", {}).get("provider_address", ""),
        PROVIDER,
    )
    return store, evidence, payment


def _verify_arc_payment(evidence: Any) -> Any:
    w3 = Web3(Web3.HTTPProvider(rpc_url(), request_kwargs={"timeout": 30}))
    verified = ArcPaymentVerifier(w3=w3).verify(evidence)
    _require_equal("verified Arc chain", verified.chain_id, ARC_TESTNET_CHAIN_ID)
    _require_address("verified Arc asset", verified.asset, ARC_TESTNET_USDC)
    _require_equal("verified payment ID", verified.payment_id, PAYMENT_ID)
    _require_equal("verified payment ID hash", verified.payment_id_hash, PAYMENT_ID_HASH)
    _require_equal("verified evidence hash", verified.evidence_hash, EVIDENCE_HASH)
    _require_equal("verified transaction", verified.transaction_hash, ARC_PAYMENT_TX)
    _require_equal("verified Arc block", verified.block_number, ARC_PAYMENT_BLOCK)
    _require_address("verified buyer", verified.buyer, BUYER)
    _require_address("verified provider", verified.provider, PROVIDER)
    _require_equal("verified amount", verified.amount_atomic, AMOUNT_ATOMIC)
    return verified


def _create_or_reuse_dispute(store: AssuranceStore, evidence: Any) -> Dispute:
    expected_id = dispute_id(PAYMENT_ID_HASH, EVIDENCE_HASH)
    existing = store.get_dispute_by_payment(PAYMENT_ID)
    if existing is not None:
        _require_equal("existing dispute ID", existing.dispute_id, expected_id)
        _require_equal("existing dispute evidence ID", existing.evidence_id, EVIDENCE_HASH)
        _require_equal("existing dispute evidence hash", existing.evidence_hash, EVIDENCE_HASH)
        _require_equal("existing dispute allegation", existing.reason, ALLEGATION)
        return existing
    opened_at = time.time()
    return store.create_dispute(
        Dispute(
            dispute_id=expected_id,
            payment_id=PAYMENT_ID,
            evidence_id=evidence.evidence_id,
            evidence_hash=evidence.evidence_hash,
            reason=ALLEGATION,
            status=DisputeStatus.OPEN,
            opened_at=opened_at,
            updated_at=opened_at,
        )
    )


def _build_payload(evidence: Any, dispute: Dispute) -> AdjudicationPayload:
    first = AdjudicationPayload.from_evidence(
        evidence,
        buyer_dispute_reason=dispute.reason,
    )
    second = AdjudicationPayload.from_evidence(
        evidence,
        buyer_dispute_reason=dispute.reason,
    )
    if first != second or first.canonical != second.canonical:
        raise RuntimeError("canonical adjudication payload is not deterministic")
    if adjudication_input_hash(first.canonical) != first.input_hash:
        raise RuntimeError("independent adjudication input hash does not match")
    _require_equal("payload dispute ID", first.dispute_id, dispute.dispute_id)
    _require_equal("payload payment ID hash", first.payment_id_hash, PAYMENT_ID_HASH)
    _require_equal("payload evidence hash", first.evidence_hash, EVIDENCE_HASH)
    _require_equal("payload terms hash", first.terms_hash, TERMS_HASH)
    return first


def _genlayer_read_only_preflight(dispute: Dispute) -> dict[str, Any]:
    source = GENLAYER_CONTRACT_SOURCE.read_text()
    deployment = load_deployment_artifact(
        GENLAYER_ARTIFACT,
        expected_network=STUDIO_DEV_NETWORK,
        source=source,
    )
    _require_equal("GenLayer artifact chain", deployment.chain_id, STUDIO_DEV_CHAIN_ID)
    _require_address("GenLayer contract", deployment.contract_address, GENLAYER_CONTRACT)
    _require_address("authorized submitter artifact", deployment.authorized_submitter, OPERATOR)

    account = load_operator_account()
    operator = Web3.to_checksum_address(account.address)
    _require_address("operator signer", operator, OPERATOR)
    client = create_studio_dev_client(account)
    chain_id = verify_studio_dev_network(client)
    estimate = estimate_transaction_fees(client)
    balance = require_operator_balance(client, operator, estimate)

    from genlayer_py.types import TransactionHashVariant

    contract = Web3.to_checksum_address(deployment.contract_address)
    has_verdict = client.read_contract(
        address=contract,
        function_name="has_verdict",
        args=[dispute.dispute_id],
        transaction_hash_variant=TransactionHashVariant.LATEST_FINAL,
    )
    if not isinstance(has_verdict, bool):
        raise RuntimeError("GenLayer has_verdict returned a malformed value")
    if has_verdict:
        raise RuntimeError("the finalized GenLayer contract already has this dispute verdict")
    return {
        "network": STUDIO_DEV_NETWORK,
        "chain_id": chain_id,
        "contract_address": contract,
        "operator_address": operator,
        "operator_balance_atomic_gen": balance,
        "estimated_fee_atomic_gen": estimate.fee_value,
        "existing_verdict": False,
        "state_read": "LATEST_FINAL",
    }


def _artifact_document(
    *,
    dispute: Dispute,
    payload: AdjudicationPayload,
    verified: Any,
    genlayer: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "phase": "5D",
        "payment_id": PAYMENT_ID,
        "payment_id_hash": PAYMENT_ID_HASH,
        "evidence_hash": EVIDENCE_HASH,
        "terms_hash": TERMS_HASH,
        "dispute_id": dispute.dispute_id,
        "dispute_status": dispute.status.value,
        "dispute_allegation": dispute.reason,
        "buyer": Web3.to_checksum_address(verified.buyer),
        "provider": Web3.to_checksum_address(verified.provider),
        "amount_atomic": verified.amount_atomic,
        "asset": Web3.to_checksum_address(verified.asset),
        "arc_chain_id": verified.chain_id,
        "arc_payment_transaction": verified.transaction_hash,
        "arc_payment_block": verified.block_number,
        "arc_confirmations_at_preparation": verified.confirmations,
        "genlayer": genlayer,
        "adjudication_payload_canonical": payload.canonical,
        "adjudication_payload_size_bytes": payload.size_bytes,
        "adjudication_input_hash": payload.input_hash,
        "semantic_demo_expectation": {
            "sla_violated": True,
            "trusted_adjudication_input": False,
            "note": "Local demo expectation only; not submitted as judging instructions.",
        },
        "evidence_store": str(EVIDENCE_DB.relative_to(PROJECT_ROOT)),
        "prepared_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _persist_idempotently(document: dict[str, Any]) -> dict[str, Any]:
    if not DISPUTE_ARTIFACT.exists():
        _atomic_json(DISPUTE_ARTIFACT, document)
        return document
    existing = json.loads(DISPUTE_ARTIFACT.read_text())
    immutable_fields = (
        "payment_id",
        "payment_id_hash",
        "evidence_hash",
        "terms_hash",
        "dispute_id",
        "dispute_allegation",
        "buyer",
        "provider",
        "amount_atomic",
        "asset",
        "arc_chain_id",
        "arc_payment_transaction",
        "arc_payment_block",
        "adjudication_payload_canonical",
        "adjudication_payload_size_bytes",
        "adjudication_input_hash",
        "semantic_demo_expectation",
        "evidence_store",
    )
    for field in immutable_fields:
        _require_equal(f"existing Phase 5D artifact {field}", existing.get(field), document[field])
    _require_address(
        "existing Phase 5D GenLayer contract",
        existing.get("genlayer", {}).get("contract_address", ""),
        document["genlayer"]["contract_address"],
    )
    return existing


def main() -> None:
    if not os.environ.get("ASSURANCE_OPERATOR_PRIVATE_KEY", "").strip():
        raise RuntimeError("ASSURANCE_OPERATOR_PRIVATE_KEY is required for operator verification")
    store, evidence, _payment = _load_and_validate_sources()
    verified = _verify_arc_payment(evidence)
    dispute = _create_or_reuse_dispute(store, evidence)
    payload = _build_payload(evidence, dispute)
    genlayer = _genlayer_read_only_preflight(dispute)
    document = _persist_idempotently(
        _artifact_document(
            dispute=dispute,
            payload=payload,
            verified=verified,
            genlayer=genlayer,
        )
    )
    print(json.dumps(document, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
