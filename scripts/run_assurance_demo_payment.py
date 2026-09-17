#!/usr/bin/env python3
"""Execute exactly one real assured x402 payment and verify it on Arc Testnet."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any

from eth_account import Account
from eth_utils import keccak
import httpx
from web3 import Web3
from x402.http.utils import decode_payment_required_header

from arc_agent_pay import (
    ArcPaymentVerifier,
    AssuranceEvidence,
    AssuranceStore,
    PaymentClient,
    PaymentPolicy,
    SqlitePaymentStore,
)
from arc_agent_pay._payment_identifier import generate_payment_id
from arc_agent_pay.assurance.demo_provider import (
    ARC_TESTNET_CHAIN_ID,
    ARC_TESTNET_NETWORK,
    ARC_TESTNET_USDC,
    BAD_RESPONSE,
    PAYMENT_AMOUNT,
    PROVIDER,
    WARRANTY_BOND,
    demo_service,
)
from arc_agent_pay.onchain import load_abi, rpc_url, warranty_bond_address
from scripts.deploy_warranty_bond import _atomic_json, _safe_read
from scripts.fund_warranty_bond import ERC20_ABI


BUYER = "0x3Ea7d677Ee657c0903Fd0bF54c8316330848eE88"
ENDPOINT = "http://127.0.0.1:8402/assurance-demo"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENTS = PROJECT_ROOT / "contracts" / "deployments"
ARTIFACT = DEPLOYMENTS / "arc-testnet-assurance-demo-payment.json"
EVIDENCE_DB = DEPLOYMENTS / "arc-testnet-assurance-demo-evidence.db"
PAYMENT_DB = DEPLOYMENTS / "arc-testnet-assurance-demo-payment-journal.db"
TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
REFUND_TOPIC = "0x" + keccak(text="WarrantyRefunded(bytes32,bytes32,address,address,uint256)").hex()


class EvidenceRecorder:
    def __init__(self, service: Any, store: AssuranceStore):
        self.service = service
        self.store = store
        self.evidence: AssuranceEvidence | None = None
        self.error: Exception | None = None

    def on_exchange(self, exchange: Any) -> None:
        try:
            evidence = AssuranceEvidence.from_exchange(exchange, service=self.service)
            self.evidence = self.store.put_evidence(evidence)
        except Exception as exc:  # PaymentClient observer is intentionally fail-open.
            self.error = exc


def _account() -> Any:
    private_key = os.environ.get("ASSURANCE_BUYER_PRIVATE_KEY", "").strip()
    if not private_key:
        raise RuntimeError("ASSURANCE_BUYER_PRIVATE_KEY is required")
    try:
        account = Account.from_key(private_key)
    except Exception as exc:
        raise RuntimeError("ASSURANCE_BUYER_PRIVATE_KEY is invalid") from exc
    if Web3.to_checksum_address(account.address) != Web3.to_checksum_address(BUYER):
        raise RuntimeError("buyer key does not derive the locked buyer address")
    return account


def _requirements() -> dict[str, Any]:
    response = httpx.get(ENDPOINT, timeout=10)
    if response.status_code != 402:
        raise RuntimeError(f"demo service preflight returned HTTP {response.status_code}")
    raw = response.headers.get("PAYMENT-REQUIRED")
    if not raw:
        raise RuntimeError("demo service returned no PAYMENT-REQUIRED header")
    required = decode_payment_required_header(raw)
    if required.x402_version != 2 or len(required.accepts) != 1:
        raise RuntimeError("demo service does not advertise one x402 v2 quote")
    quote = required.accepts[0]
    if (
        quote.scheme != "exact"
        or str(quote.network) != ARC_TESTNET_NETWORK
        or Web3.to_checksum_address(quote.asset) != Web3.to_checksum_address(ARC_TESTNET_USDC)
        or quote.amount != str(PAYMENT_AMOUNT)
        or Web3.to_checksum_address(quote.pay_to) != Web3.to_checksum_address(PROVIDER)
    ):
        raise RuntimeError("demo service quote differs from the locked payment terms")
    return required.model_dump(mode="json", by_alias=True, exclude_none=True)


def _read_state(w3: Web3, buyer: str, provider: str, token: Any, bond: Any) -> dict[str, int]:
    return {
        "buyer_native": int(_safe_read("buyer native balance", lambda: w3.eth.get_balance(buyer))),
        "buyer_usdc": int(
            _safe_read("buyer USDC balance", lambda: token.functions.balanceOf(buyer).call())
        ),
        "provider_usdc": int(
            _safe_read("provider USDC balance", lambda: token.functions.balanceOf(provider).call())
        ),
        "warranty_bond_usdc": int(
            _safe_read(
                "WarrantyBond USDC balance",
                lambda: token.functions.balanceOf(bond.address).call(),
            )
        ),
        "provider_bond": int(
            _safe_read(
                "provider bond balance",
                lambda: bond.functions.bond_balance(provider).call(),
            )
        ),
    }


def _wait_for_confirmations(w3: Web3, block_number: int, minimum: int = 2) -> None:
    deadline = time.monotonic() + 90
    while True:
        latest = int(_safe_read("latest Arc block", lambda: w3.eth.block_number))
        if latest - block_number + 1 >= minimum:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("payment is confirmed but still lacks two Arc confirmations")
        time.sleep(1)


def _finalize_existing_payment(
    *,
    artifact: dict[str, Any],
    evidence: AssuranceEvidence,
    w3: Web3,
    buyer: str,
    provider: str,
    token: Any,
    bond: Any,
) -> None:
    """Verify and reconcile an already-settled payment without resubmitting it."""
    settlement = json.loads(evidence.decoded_settlement_response or "{}")
    tx_hash = str(settlement.get("transaction") or "")
    if (
        len(tx_hash) != 66
        or tx_hash.lower() != str(artifact.get("arc_transaction_hash", "")).lower()
    ):
        raise RuntimeError("persisted evidence and recovery artifact disagree on the Arc tx")
    if evidence.paid_response_status != 200:
        raise RuntimeError(f"paid response returned HTTP {evidence.paid_response_status}")
    if evidence.response_body.content is None:
        raise RuntimeError("persisted evidence has no paid response body")
    response_body = json.loads(evidence.response_body.content)
    if response_body != BAD_RESPONSE:
        raise RuntimeError("paid response is not the locked deterministic bad response")

    receipt = _safe_read("Arc settlement receipt", lambda: w3.eth.get_transaction_receipt(tx_hash))
    if int(receipt["status"]) != 1:
        raise RuntimeError("Arc settlement transaction reverted")
    block_number = int(receipt["blockNumber"])
    _wait_for_confirmations(w3, block_number)
    verified = ArcPaymentVerifier(w3=w3).verify(evidence)
    if (
        Web3.to_checksum_address(verified.buyer) != buyer
        or Web3.to_checksum_address(verified.provider) != provider
        or verified.amount_atomic != PAYMENT_AMOUNT
    ):
        raise RuntimeError("ArcPaymentVerifier derived unexpected payment parties or amount")

    final = _read_state(w3, buyer, provider, token, bond)
    initial_buyer = int(artifact["initial_buyer_usdc"])
    initial_provider = int(artifact["initial_provider_usdc"])
    gas_native = int(receipt["gasUsed"]) * int(receipt["effectiveGasPrice"])
    # Arc Testnet debits the native gas charge from the provider's USDC balance
    # at 1e12 native atomic units per USDC atomic unit, truncating the remainder.
    gas_usdc = gas_native // 10**12
    if final["buyer_usdc"] != initial_buyer - PAYMENT_AMOUNT:
        raise RuntimeError("buyer balance did not decrease by the exact signed payment")
    if final["provider_usdc"] != initial_provider + PAYMENT_AMOUNT - gas_usdc:
        raise RuntimeError("provider payment receipt does not reconcile after settlement gas")
    if final["provider_bond"] != 5_000_000 or final["warranty_bond_usdc"] != 5_000_000:
        raise RuntimeError("WarrantyBond changed during x402 payment")
    refund_logs = _safe_read(
        "WarrantyBond refund logs",
        lambda: w3.eth.get_logs(
            {
                "address": bond.address,
                "fromBlock": int(artifact["preflight_block_number"]),
                "toBlock": "latest",
                "topics": [REFUND_TOPIC],
            }
        ),
    )
    if refund_logs:
        raise RuntimeError("a WarrantyRefunded event occurred during the payment")

    block = _safe_read("Arc payment block", lambda: w3.eth.get_block(block_number))
    artifact.update(
        {
            "status": "verified",
            "payment_id_hash": verified.payment_id_hash,
            "terms_hash": evidence.terms_hash,
            "evidence_hash": evidence.evidence_hash,
            "evidence_id": evidence.evidence_id,
            "evidence_store": str(EVIDENCE_DB.relative_to(PROJECT_ROOT)),
            "payment_journal": str(PAYMENT_DB.relative_to(PROJECT_ROOT)),
            "arc_transaction_hash": verified.transaction_hash,
            "arc_block_number": verified.block_number,
            "verified_buyer": Web3.to_checksum_address(verified.buyer),
            "verified_provider": Web3.to_checksum_address(verified.provider),
            "verified_amount_atomic": verified.amount_atomic,
            "confirmations_at_verification": verified.confirmations,
            "http_status": evidence.paid_response_status,
            "payment_response": settlement,
            "bad_response": response_body,
            "final_buyer_usdc": final["buyer_usdc"],
            "final_provider_usdc": final["provider_usdc"],
            "final_provider_bond": final["provider_bond"],
            "final_warranty_bond_usdc": final["warranty_bond_usdc"],
            "provider_settlement_gas_native_atomic": gas_native,
            "provider_settlement_gas_usdc_atomic": gas_usdc,
            "verified_at": datetime.fromtimestamp(int(block["timestamp"]), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
    )
    _atomic_json(ARTIFACT, artifact)
    print(json.dumps({"verified_payment": artifact}, indent=2), flush=True)


def _resume_existing_payment() -> None:
    """Resume verification from a known tx and evidence; never create a payment."""
    if not ARTIFACT.exists() or not EVIDENCE_DB.exists() or not PAYMENT_DB.exists():
        raise RuntimeError("resume requires the payment artifact, journal, and evidence store")
    artifact = json.loads(ARTIFACT.read_text())
    tx_hash = str(artifact.get("arc_transaction_hash") or "")
    if len(tx_hash) != 66:
        raise RuntimeError("resume artifact has no known Arc settlement transaction")
    account = _account()
    buyer = Web3.to_checksum_address(account.address)
    provider = Web3.to_checksum_address(PROVIDER)
    w3 = Web3(Web3.HTTPProvider(rpc_url(), request_kwargs={"timeout": 30}))
    if int(_safe_read("Arc chain ID", lambda: w3.eth.chain_id)) != ARC_TESTNET_CHAIN_ID:
        raise RuntimeError("resume RPC is not Arc Testnet")
    token = w3.eth.contract(address=Web3.to_checksum_address(ARC_TESTNET_USDC), abi=ERC20_ABI)
    bond = w3.eth.contract(
        address=Web3.to_checksum_address(WARRANTY_BOND), abi=load_abi("warranty_bond")
    )
    evidence = AssuranceStore(EVIDENCE_DB).get_evidence_by_payment(artifact["payment_id"])
    if evidence is None:
        raise RuntimeError("resume found no persisted AssuranceEvidence")
    _finalize_existing_payment(
        artifact=artifact,
        evidence=evidence,
        w3=w3,
        buyer=buyer,
        provider=provider,
        token=token,
        bond=bond,
    )


async def _run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-testnet-payment", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="verify the known persisted transaction without creating another payment",
    )
    args = parser.parse_args()
    if args.resume:
        _resume_existing_payment()
        return
    if not args.confirm_testnet_payment:
        parser.error("real payment requires --confirm-testnet-payment")
    if ARTIFACT.exists() or EVIDENCE_DB.exists() or PAYMENT_DB.exists():
        raise RuntimeError(
            "demo payment state already exists; refusing to create another authorization"
        )

    account = _account()
    buyer = Web3.to_checksum_address(account.address)
    provider = Web3.to_checksum_address(PROVIDER)
    bond_address = Web3.to_checksum_address(WARRANTY_BOND)
    configured_bond = warranty_bond_address()
    if not configured_bond or Web3.to_checksum_address(configured_bond) != bond_address:
        raise RuntimeError("configured WarrantyBond differs from the locked deployment")

    requirements = _requirements()
    w3 = Web3(Web3.HTTPProvider(rpc_url(), request_kwargs={"timeout": 30}))
    chain_id = int(_safe_read("Arc chain ID", lambda: w3.eth.chain_id))
    if chain_id != ARC_TESTNET_CHAIN_ID:
        raise RuntimeError(f"connected chain {chain_id} is not Arc Testnet")
    token = w3.eth.contract(address=Web3.to_checksum_address(ARC_TESTNET_USDC), abi=ERC20_ABI)
    bond = w3.eth.contract(address=bond_address, abi=load_abi("warranty_bond"))
    initial = _read_state(w3, buyer, provider, token, bond)
    if initial["buyer_usdc"] < PAYMENT_AMOUNT * 2 or initial["buyer_native"] <= 0:
        raise RuntimeError("buyer lacks the payment plus a reasonable remaining buffer")
    if initial["provider_bond"] != 5_000_000 or initial["warranty_bond_usdc"] != 5_000_000:
        raise RuntimeError("WarrantyBond funding changed before the payment")
    preflight_block = int(_safe_read("preflight Arc block", lambda: w3.eth.block_number))
    payment_id = generate_payment_id("assurance_")
    artifact: dict[str, Any] = {
        "schema_version": "1",
        "status": "preflight_complete",
        "chain_id": chain_id,
        "buyer": buyer,
        "provider": provider,
        "endpoint": ENDPOINT,
        "amount_atomic": PAYMENT_AMOUNT,
        "payment_id": payment_id,
        "preflight_block_number": preflight_block,
        "initial_buyer_usdc": initial["buyer_usdc"],
        "initial_provider_usdc": initial["provider_usdc"],
        "initial_provider_bond": initial["provider_bond"],
        "initial_warranty_bond_usdc": initial["warranty_bond_usdc"],
        "payment_requirements": requirements,
    }
    _atomic_json(ARTIFACT, artifact)
    print(json.dumps({"preflight": artifact}, indent=2), flush=True)

    service = demo_service(ENDPOINT)
    evidence_store = AssuranceStore(EVIDENCE_DB)
    recorder = EvidenceRecorder(service, evidence_store)

    def on_event(event_type: str, payload: dict[str, Any]) -> None:
        if payload.get("payment_id") == payment_id:
            artifact["status"] = event_type
            if payload.get("tx_hash"):
                artifact["arc_transaction_hash"] = payload["tx_hash"]
            _atomic_json(ARTIFACT, artifact)
        print(f"PAYMENT_EVENT={event_type}", flush=True)

    policy = PaymentPolicy(
        max_payment_usdc="1",
        allowed_hosts=["127.0.0.1"],
        allowed_networks=[ARC_TESTNET_NETWORK],
        allowed_assets=[ARC_TESTNET_USDC],
        allowed_pay_to=[PROVIDER],
    )
    async with PaymentClient(
        account=account,
        budget_usdc="1",
        timeout=60,
        max_retries=0,
        policy=policy,
        payment_store=SqlitePaymentStore(str(PAYMENT_DB)),
        exchange_observer=recorder,
        on_event=on_event,
    ) as client:
        response = await client.get(ENDPOINT, payment_id=payment_id)

    if response.status_code != 200:
        raise RuntimeError(f"paid response returned HTTP {response.status_code}")
    settlement = PaymentClient.extract_payment_response(response.headers)
    tx_hash = str(settlement.get("transaction") or "")
    if len(tx_hash) != 66:
        raise RuntimeError("paid response lacks a usable Arc settlement transaction")
    if response.json() != BAD_RESPONSE:
        raise RuntimeError("paid response is not the locked deterministic bad response")
    if recorder.error is not None:
        raise RuntimeError(f"evidence recorder failed: {recorder.error}")
    evidence = recorder.evidence
    if evidence is None:
        raise RuntimeError("PaymentClient observer produced no Assurance evidence")
    persisted = evidence_store.get_evidence_by_payment(payment_id)
    if persisted != evidence or evidence.arc_transaction_reference != tx_hash.lower():
        raise RuntimeError("persisted evidence does not bind the settlement transaction")

    _finalize_existing_payment(
        artifact=artifact,
        evidence=evidence,
        w3=w3,
        buyer=buyer,
        provider=provider,
        token=token,
        bond=bond,
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
