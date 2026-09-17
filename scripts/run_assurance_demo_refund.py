#!/usr/bin/env python3
"""Execute or safely resume the one Phase 5F WarrantyBond refund through the relay."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from web3 import Web3
from web3.logs import DISCARD

from arc_agent_pay.assurance import (
    AdjudicationPayload,
    ArcPaymentVerifier,
    AssuranceSettlementBinding,
    AssuranceSettlementRelay,
    AssuranceStore,
    FinalizedGenLayerAdjudication,
    GenLayerAdjudicationClient,
    GenLayerStoredVerdict,
    GenLayerSubmission,
    SettlementStatus,
    WarrantyBondClient,
)
from arc_agent_pay.assurance.arc_payment import (
    ARC_TESTNET_CHAIN_ID,
    ARC_TESTNET_USDC,
)
from arc_agent_pay.assurance.genlayer_live import (
    STUDIO_DEV_CHAIN_ID,
    STUDIO_DEV_NETWORK,
    create_studio_dev_client,
    load_deployment_artifact,
    load_operator_account,
    verify_studio_dev_network,
)
from arc_agent_pay.exceptions import (
    SettlementAlreadyProcessed,
    SettlementPending,
    SettlementSubmissionOutcomeUnknown,
)
from arc_agent_pay.onchain import load_abi, rpc_url, warranty_bond_address
from arc_agent_pay.workflow.models import bytes32
from scripts.deploy_warranty_bond import _atomic_json, _safe_read
from scripts.fund_warranty_bond import ERC20_ABI


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DB = PROJECT_ROOT / "contracts" / "deployments" / "arc-testnet-assurance-demo-evidence.db"
DISPUTE_ARTIFACT = (
    PROJECT_ROOT / "contracts" / "deployments" / "arc-testnet-assurance-demo-dispute.json"
)
ADJUDICATION_ARTIFACT = (
    PROJECT_ROOT / "contracts" / "deployments" / "arc-testnet-assurance-demo-adjudication.json"
)
REFUND_ARTIFACT = (
    PROJECT_ROOT / "contracts" / "deployments" / "arc-testnet-assurance-demo-refund.json"
)
GENLAYER_ARTIFACT = PROJECT_ROOT / "genlayer" / "deployments" / "studio-dev.json"
GENLAYER_SOURCE = PROJECT_ROOT / "genlayer" / "contracts" / "AgentPayAssurance.py"

PAYMENT_ID = "assurance_cf161b91748a4d4f8b10308b35a9e174"
PAYMENT_ID_HASH = "0x909d5162f2c77c92e27ae79b1e0c1fdf73b1475972973a9416fb4c61fe914268"
EVIDENCE_HASH = "0xeeda66cc3adb4e4dc22cfb172aae7feeb39d743f6506159786f54c8ce3491a7f"
TERMS_HASH = "0x3bb78690528858d113197ab92b916646f528cdad602bda87edd03516342dd389"
DISPUTE_ID = "0x7ab7aba2fb6fd39b26e4c492b332a9f179ff609e31e6096bbb92098aabd52844"
INPUT_HASH = "0x00e255414263a5336dc2eba78f7ed71f3109a25a5839d5e8e68c5f6ee270067e"
BINDING_HASH = "0x526189d20627951f7d5eaf143c6fda79e957de61de604e6d85160b33231f3c6a"
ARC_PAYMENT_TX = "0xbd863ffbe75ca3e9357538790b3929acecd27fd8e6bf9f515d0a263a1f135617"
GENLAYER_CONTRACT = "0x41c5de73bda3379351a2d77648cb77389841dc53"
GENLAYER_TX = "0xb6d17051ec64f15d6cab6a2809b5d61fabb69ade69a8cbf3490eb9e2901b81c8"
WARRANTY_BOND = "0x74c4b5006136d37134B60F4Ff952E446BC11C901"
OPERATOR = "0xAD9B0B4EDa94329c72837263979b0fD3Be4BCD18"
PROVIDER = "0x5cBC83e3363710772B8812Ad3AB5423116011651"
BUYER = "0x3Ea7d677Ee657c0903Fd0bF54c8316330848eE88"
AMOUNT_ATOMIC = 1_000_000
INITIAL_BOND = 5_000_000
FROZEN_CONFIRMATIONS = 2_399


def _require_equal(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise RuntimeError(f"{label} mismatch: expected {expected!r}, found {actual!r}")


def _require_address(label: str, actual: str, expected: str) -> None:
    _require_equal(label, Web3.to_checksum_address(actual), Web3.to_checksum_address(expected))


def _load_sources() -> tuple[
    AssuranceStore,
    Any,
    Any,
    AdjudicationPayload,
    dict[str, Any],
]:
    if not all(path.exists() for path in (EVIDENCE_DB, DISPUTE_ARTIFACT, ADJUDICATION_ARTIFACT)):
        raise RuntimeError("Phase 5C, 5D, and 5E persisted state is required")
    phase5d = json.loads(DISPUTE_ARTIFACT.read_text())
    phase5e = json.loads(ADJUDICATION_ARTIFACT.read_text())
    locked = {
        "payment_id": PAYMENT_ID,
        "payment_id_hash": PAYMENT_ID_HASH,
        "evidence_hash": EVIDENCE_HASH,
        "terms_hash": TERMS_HASH,
        "dispute_id": DISPUTE_ID,
        "adjudication_input_hash": INPUT_HASH,
    }
    for field, expected in locked.items():
        _require_equal(f"Phase 5D {field}", phase5d.get(field), expected)
        _require_equal(f"Phase 5E {field}", phase5e.get(field), expected)
    _require_equal("Phase 5E status", phase5e.get("status"), "finalized")
    _require_equal("Phase 5E execution", phase5e.get("execution_result"), "FINISHED_WITH_RETURN")
    _require_equal("Phase 5E transaction", phase5e.get("genlayer_transaction_id"), GENLAYER_TX)
    _require_equal("Phase 5E SLA decision", phase5e.get("sla_violated"), True)
    preview = phase5e.get("settlement_binding_preview", {})
    _require_equal("Phase 5E binding hash", preview.get("binding_hash"), BINDING_HASH)
    _require_equal(
        "Phase 5E frozen confirmations",
        preview.get("fresh_arc_confirmations"),
        FROZEN_CONFIRMATIONS,
    )
    payload = AdjudicationPayload.model_validate_json(phase5d["adjudication_payload_canonical"])
    _require_equal("frozen payload input hash", payload.input_hash, INPUT_HASH)
    store = AssuranceStore(EVIDENCE_DB)
    evidence = store.get_evidence_by_payment(PAYMENT_ID)
    dispute = store.get_dispute(DISPUTE_ID)
    if evidence is None or dispute is None:
        raise RuntimeError("persisted evidence or dispute is missing")
    _require_equal("local dispute status", dispute.status.value, "upheld")
    _require_equal("local adjudication tx", dispute.verdict.adjudicator_reference, GENLAYER_TX)
    return store, evidence, dispute, payload, phase5e


def _verify_genlayer_finalized(
    phase5e: dict[str, Any], payload: AdjudicationPayload
) -> tuple[Any, Any, Any]:
    deployment = load_deployment_artifact(
        GENLAYER_ARTIFACT,
        expected_network=STUDIO_DEV_NETWORK,
        source=GENLAYER_SOURCE.read_text(),
    )
    _require_address("GenLayer contract", deployment.contract_address, GENLAYER_CONTRACT)
    _require_address("GenLayer operator", deployment.authorized_submitter, OPERATOR)
    account = load_operator_account()
    _require_address("GenLayer signer", account.address, OPERATOR)
    client = create_studio_dev_client(account)
    _require_equal("Studio-dev chain", verify_studio_dev_network(client), STUDIO_DEV_CHAIN_ID)
    submission = GenLayerSubmission(
        transaction_id=GENLAYER_TX,
        dispute_id=DISPUTE_ID,
        adjudication_input_hash=INPUT_HASH,
    )
    adapter = GenLayerAdjudicationClient(
        contract_address=GENLAYER_CONTRACT,
        client=client,
    )
    verdict = adapter.wait_for_finalized_verdict(
        submission,
        payload,
        interval=3_000,
        retries=2,
    )
    status = adapter.get_transaction_status(GENLAYER_TX)
    _require_equal("GenLayer finality", status.state, "finalized")
    _require_equal("GenLayer outcome", status.outcome, "accepted")
    _require_equal("GenLayer execution", status.execution_result, "FINISHED_WITH_RETURN")
    _require_equal("GenLayer verdict violation", verdict.sla_violated, True)
    persisted = GenLayerStoredVerdict.model_validate(phase5e["finalized_verdict"])
    _require_equal("LATEST_FINAL verdict", verdict, persisted)
    return submission, status, verdict


def _build_locked_binding(
    *,
    evidence: Any,
    dispute: Any,
    payload: AdjudicationPayload,
    submission: Any,
    status: Any,
    verdict: Any,
    w3: Web3,
) -> tuple[AssuranceSettlementBinding, Any]:
    fresh = ArcPaymentVerifier(w3=w3).verify(evidence)
    _require_address("fresh Arc buyer", fresh.buyer, BUYER)
    _require_address("fresh Arc provider", fresh.provider, PROVIDER)
    _require_address("fresh Arc asset", fresh.asset, ARC_TESTNET_USDC)
    _require_equal("fresh Arc chain", fresh.chain_id, ARC_TESTNET_CHAIN_ID)
    _require_equal("fresh Arc amount", fresh.amount_atomic, AMOUNT_ATOMIC)
    _require_equal("fresh Arc payment tx", fresh.transaction_hash, ARC_PAYMENT_TX)
    if fresh.confirmations < FROZEN_CONFIRMATIONS:
        raise RuntimeError("fresh Arc confirmation count regressed below the frozen binding")
    frozen_verified = fresh.model_copy(update={"confirmations": FROZEN_CONFIRMATIONS})
    finalized = FinalizedGenLayerAdjudication(
        contract_address=GENLAYER_CONTRACT,
        submission=submission,
        transaction_status=status,
        verdict=verdict,
    )
    binding = AssuranceSettlementBinding.from_sources(
        evidence=evidence,
        dispute=dispute,
        verified_payment=frozen_verified,
        adjudication_payload=payload,
        adjudication=finalized,
        configured_genlayer_contract=GENLAYER_CONTRACT,
    )
    binding.ensure_eligible()
    _require_equal("locked settlement binding hash", binding.binding_hash, BINDING_HASH)
    return binding, fresh


def _token_balance(token: Any, address: str) -> int:
    return int(_safe_read("USDC balance", lambda: token.functions.balanceOf(address).call()))


class RecordingWarrantyBondClient(WarrantyBondClient):
    """Persist the returned tx hash before control returns to relay receipt polling."""

    def __init__(self, *args: Any, on_transaction: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._on_transaction = on_transaction

    def submit_refund(self, *args: Any, **kwargs: Any) -> str:
        transaction_hash = super().submit_refund(*args, **kwargs)
        self._on_transaction(transaction_hash)
        return transaction_hash


def _base_artifact(binding: AssuranceSettlementBinding, initial: dict[str, int]) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "phase": "5F",
        "status": "preflight_complete",
        "payment_id": PAYMENT_ID,
        "payment_id_hash": PAYMENT_ID_HASH,
        "evidence_hash": EVIDENCE_HASH,
        "terms_hash": TERMS_HASH,
        "dispute_id": DISPUTE_ID,
        "adjudication_input_hash": INPUT_HASH,
        "original_arc_payment_transaction": ARC_PAYMENT_TX,
        "buyer": Web3.to_checksum_address(BUYER),
        "provider": Web3.to_checksum_address(PROVIDER),
        "amount_atomic": AMOUNT_ATOMIC,
        "genlayer_contract": Web3.to_checksum_address(GENLAYER_CONTRACT),
        "genlayer_adjudication_transaction": GENLAYER_TX,
        "sla_violated": True,
        "settlement_binding_hash": binding.binding_hash,
        "warranty_bond": Web3.to_checksum_address(WARRANTY_BOND),
        "initial_buyer_usdc": initial["buyer_usdc"],
        "initial_provider_external_usdc": initial["provider_usdc"],
        "initial_provider_bond": initial["provider_bond"],
        "initial_warranty_bond_usdc": initial["bond_usdc"],
        "initial_operator_native": initial["operator_native"],
        "prepared_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-testnet-refund", action="store_true")
    args = parser.parse_args()
    if not args.confirm_testnet_refund:
        parser.error("the live refund requires --confirm-testnet-refund")
    if not os.environ.get("ASSURANCE_OPERATOR_PRIVATE_KEY", "").strip():
        raise RuntimeError("ASSURANCE_OPERATOR_PRIVATE_KEY is required")

    store, evidence, dispute, payload, phase5e = _load_sources()
    submission, status, verdict = _verify_genlayer_finalized(phase5e, payload)
    account = load_operator_account()
    _require_address("Arc resolver signer", account.address, OPERATOR)
    w3 = Web3(Web3.HTTPProvider(rpc_url(), request_kwargs={"timeout": 30}))
    _require_equal(
        "Arc chain", int(_safe_read("Arc chain ID", lambda: w3.eth.chain_id)), ARC_TESTNET_CHAIN_ID
    )
    binding, fresh_payment = _build_locked_binding(
        evidence=evidence,
        dispute=dispute,
        payload=payload,
        submission=submission,
        status=status,
        verdict=verdict,
        w3=w3,
    )

    configured_bond = warranty_bond_address()
    if not configured_bond:
        raise RuntimeError("WarrantyBond is not configured")
    _require_address("configured WarrantyBond", configured_bond, WARRANTY_BOND)
    token = w3.eth.contract(address=Web3.to_checksum_address(ARC_TESTNET_USDC), abi=ERC20_ABI)
    raw_bond = w3.eth.contract(
        address=Web3.to_checksum_address(WARRANTY_BOND),
        abi=load_abi("warranty_bond"),
    )

    recovery: dict[str, Any]
    if REFUND_ARTIFACT.exists():
        recovery = json.loads(REFUND_ARTIFACT.read_text())
        _require_equal(
            "existing refund binding", recovery.get("settlement_binding_hash"), BINDING_HASH
        )
        _require_equal("existing refund dispute", recovery.get("dispute_id"), DISPUTE_ID)
    else:
        recovery = {}

    def on_transaction(tx_hash: str) -> None:
        recovery.update(
            {
                "status": "broadcast",
                "refund_transaction_hash": tx_hash,
                "broadcast_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
        _atomic_json(REFUND_ARTIFACT, recovery)
        print(f"ARC_REFUND_TRANSACTION_HASH={tx_hash}", flush=True)

    bond = RecordingWarrantyBondClient(
        address=WARRANTY_BOND,
        account=account,
        w3=w3,
        on_transaction=on_transaction,
    )
    _require_address("WarrantyBond USDC", bond.usdc(), ARC_TESTNET_USDC)
    _require_address("WarrantyBond resolver", bond.resolver(), OPERATOR)
    _require_equal("provider bond", bond.bond_balance(PROVIDER), INITIAL_BOND)
    _require_equal("dispute replay flag", bond.dispute_processed(DISPUTE_ID), False)
    _require_equal("payment replay flag", bond.payment_refunded(PAYMENT_ID_HASH), False)

    operator = Web3.to_checksum_address(account.address)
    provider = Web3.to_checksum_address(PROVIDER)
    buyer = Web3.to_checksum_address(BUYER)
    initial = {
        "buyer_usdc": _token_balance(token, buyer),
        "provider_usdc": _token_balance(token, provider),
        "bond_usdc": _token_balance(token, raw_bond.address),
        "provider_bond": bond.bond_balance(provider),
        "operator_native": int(
            _safe_read("operator native balance", lambda: w3.eth.get_balance(operator))
        ),
    }
    _require_equal("initial provider bond", initial["provider_bond"], INITIAL_BOND)
    _require_equal("initial WarrantyBond USDC", initial["bond_usdc"], INITIAL_BOND)
    refund_function = raw_bond.functions.refund(
        bytes32(binding.dispute_id),
        bytes32(binding.payment_id_hash),
        Web3.to_checksum_address(binding.provider),
        Web3.to_checksum_address(binding.buyer),
        binding.payment_amount,
    )
    estimated_gas = int(
        _safe_read(
            "refund gas estimate",
            lambda: refund_function.estimate_gas({"from": operator}),
        )
    )
    gas_price = int(_safe_read("Arc gas price", lambda: w3.eth.gas_price))
    estimated_cost = estimated_gas * gas_price
    if initial["operator_native"] < estimated_cost:
        raise RuntimeError("operator lacks Arc native balance for the refund")

    if not recovery:
        recovery.update(_base_artifact(binding, initial))
        recovery.update(
            {
                "fresh_arc_confirmations": fresh_payment.confirmations,
                "estimated_refund_gas": estimated_gas,
                "estimated_refund_cost_native": estimated_cost,
            }
        )
        _atomic_json(REFUND_ARTIFACT, recovery)

    relay = AssuranceSettlementRelay(
        store=store,
        bond_client=bond,
        payment_verifier=ArcPaymentVerifier(w3=w3),
        operator_address=OPERATOR,
        genlayer_contract_address=GENLAYER_CONTRACT,
    )
    try:
        result = relay.settle(binding)
    except SettlementSubmissionOutcomeUnknown as exc:
        recovery["status"] = "ambiguous_submission"
        recovery["error"] = str(exc)
        _atomic_json(REFUND_ARTIFACT, recovery)
        raise
    except SettlementPending as exc:
        recovery["status"] = "pending"
        recovery["refund_transaction_hash"] = exc.transaction_hash
        recovery["error"] = str(exc)
        _atomic_json(REFUND_ARTIFACT, recovery)
        raise

    receipt = _safe_read(
        "confirmed refund receipt",
        lambda: w3.eth.get_transaction_receipt(result.arc_refund_transaction_hash),
    )
    _require_equal("refund receipt status", int(receipt["status"]), 1)
    events = raw_bond.events.WarrantyRefunded().process_receipt(receipt, errors=DISCARD)
    _require_equal("WarrantyRefunded event count", len(events), 1)
    event = events[0]["args"]
    _require_equal("event dispute ID", Web3.to_hex(event["dispute_id"]), DISPUTE_ID)
    _require_equal("event payment hash", Web3.to_hex(event["payment_id_hash"]), PAYMENT_ID_HASH)
    _require_address("event provider", event["provider"], PROVIDER)
    _require_address("event buyer", event["buyer"], BUYER)
    _require_equal("event amount", int(event["payment_amount"]), AMOUNT_ATOMIC)

    final = {
        "buyer_usdc": _token_balance(token, buyer),
        "provider_usdc": _token_balance(token, provider),
        "bond_usdc": _token_balance(token, raw_bond.address),
        "provider_bond": bond.bond_balance(provider),
        "operator_native": int(
            _safe_read("operator native balance", lambda: w3.eth.get_balance(operator))
        ),
        "dispute_processed": bond.dispute_processed(DISPUTE_ID),
        "payment_refunded": bond.payment_refunded(PAYMENT_ID_HASH),
    }
    _require_equal("buyer refund", final["buyer_usdc"], initial["buyer_usdc"] + AMOUNT_ATOMIC)
    _require_equal("provider external USDC", final["provider_usdc"], initial["provider_usdc"])
    _require_equal("final provider bond", final["provider_bond"], INITIAL_BOND - AMOUNT_ATOMIC)
    _require_equal("final WarrantyBond USDC", final["bond_usdc"], INITIAL_BOND - AMOUNT_ATOMIC)
    _require_equal("processed dispute", final["dispute_processed"], True)
    _require_equal("refunded payment", final["payment_refunded"], True)

    replay_preflight_refused = False
    try:
        binding.ensure_eligible()
        relay._preflight(binding)
    except SettlementAlreadyProcessed:
        replay_preflight_refused = True
    if not replay_preflight_refused:
        raise RuntimeError("relay preflight did not reject the consumed settlement")

    record = store.get_settlement(DISPUTE_ID)
    if record is None:
        raise RuntimeError("confirmed local settlement record is missing")
    _require_equal("local settlement status", record.status, SettlementStatus.CONFIRMED)
    _require_equal("local settlement binding", record.binding_hash, BINDING_HASH)
    _require_equal(
        "local refund transaction", record.arc_transaction_hash, result.arc_refund_transaction_hash
    )

    block = _safe_read("refund block", lambda: w3.eth.get_block(result.arc_block_number))
    recovery.update(
        {
            "status": "confirmed",
            "refund_transaction_hash": result.arc_refund_transaction_hash,
            "refund_block_number": result.arc_block_number,
            "gas_used": result.gas_used,
            "warranty_refunded_event": {
                "dispute_id": DISPUTE_ID,
                "payment_id_hash": PAYMENT_ID_HASH,
                "provider": provider,
                "buyer": buyer,
                "payment_amount": AMOUNT_ATOMIC,
            },
            "final_buyer_usdc": final["buyer_usdc"],
            "final_provider_external_usdc": final["provider_usdc"],
            "final_provider_bond": final["provider_bond"],
            "final_warranty_bond_usdc": final["bond_usdc"],
            "final_operator_native": final["operator_native"],
            "dispute_processed": final["dispute_processed"],
            "payment_refunded": final["payment_refunded"],
            "local_settlement_status": record.status.value,
            "second_settlement_preflight_refused": replay_preflight_refused,
            "settled_at": datetime.fromtimestamp(int(block["timestamp"]), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
    )
    _atomic_json(REFUND_ARTIFACT, recovery)
    print(json.dumps(recovery, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
