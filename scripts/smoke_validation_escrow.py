#!/usr/bin/env python3
"""Run one low-value fund -> approved release on Arc Testnet.

This deliberately uses dedicated smoke-test credentials and refuses every
other chain. It prints transaction and balance evidence, never private keys.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets

from eth_account import Account
from web3 import Web3

from arc_agent_pay import (
    DeliveryEvidence,
    EscrowClient,
    EscrowStatus,
    ValidationVerdict,
    WorkOrder,
    hash_content,
    sign_funding_authorization,
    sign_verdict,
)


ARC_TESTNET_CHAIN_ID = 5_042_002
ARC_TESTNET_RPC = "https://rpc.testnet.arc.network"
ARC_TESTNET_USDC = "0x3600000000000000000000000000000000000000"
TOKEN_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--escrow", required=True, help="deployed ValidationEscrow")
    parser.add_argument("--provider", required=True, help="recipient of the test release")
    parser.add_argument("--amount", type=int, default=1_000, help="USDC base units")
    parser.add_argument("--rpc", default=os.environ.get("ARC_TESTNET_RPC", ARC_TESTNET_RPC))
    parser.add_argument(
        "--confirm-testnet-spend",
        action="store_true",
        help="required acknowledgement that this sends a real testnet payment",
    )
    args = parser.parse_args()
    if not args.confirm_testnet_spend:
        parser.error("smoke test requires --confirm-testnet-spend")
    if args.amount <= 0 or args.amount > 10_000:
        parser.error("amount must be between 1 and 10000 base units (max 0.01 USDC)")

    payer_key = os.environ.get("ESCROW_SMOKE_PAYER_PRIVATE_KEY")
    validator_key = os.environ.get("ESCROW_SMOKE_VALIDATOR_PRIVATE_KEY")
    if not payer_key or not validator_key:
        parser.error(
            "ESCROW_SMOKE_PAYER_PRIVATE_KEY and "
            "ESCROW_SMOKE_VALIDATOR_PRIVATE_KEY are required"
        )

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
    if w3.eth.chain_id != ARC_TESTNET_CHAIN_ID:
        raise RuntimeError(
            f"refusing chain {w3.eth.chain_id}; expected Arc Testnet "
            f"{ARC_TESTNET_CHAIN_ID}"
        )

    escrow_address = w3.to_checksum_address(args.escrow)
    provider = w3.to_checksum_address(args.provider)
    asset = w3.to_checksum_address(ARC_TESTNET_USDC)
    if not w3.eth.get_code(escrow_address):
        raise RuntimeError(f"escrow has no contract code: {escrow_address}")

    payer = Account.from_key(payer_key)
    validator = Account.from_key(validator_key)
    escrow = EscrowClient(escrow_address, account=payer, w3=w3)
    if escrow.contract.functions.asset().call() != asset:
        raise RuntimeError("escrow immutable asset is not Arc Testnet USDC")

    token = w3.eth.contract(address=asset, abi=TOKEN_ABI)
    payer_before = token.functions.balanceOf(payer.address).call()
    payer_native_before = w3.eth.get_balance(payer.address)
    provider_before = token.functions.balanceOf(provider).call()
    if payer_before < args.amount:
        raise RuntimeError(
            f"payer has {payer_before} base units; smoke test needs {args.amount} plus gas"
        )
    if payer_native_before == 0:
        raise RuntimeError("payer has no native Arc Testnet USDC for transaction gas")

    now = int(w3.eth.get_block("latest")["timestamp"])
    order = WorkOrder(
        escrow=escrow_address,
        payer=payer.address,
        provider=provider,
        validator=validator.address,
        asset=asset,
        amount=args.amount,
        chain_id=ARC_TESTNET_CHAIN_ID,
        delivery_deadline=now + 600,
        refund_after=now + 1_200,
        task_hash=hash_content("arc-agent-pay escrow smoke test"),
        nonce="0x" + secrets.token_hex(32),
    )

    authorization = sign_funding_authorization(order, private_key=payer_key)
    fund_tx = escrow.fund(order, authorization)

    funded_at = int(w3.eth.get_block("latest")["timestamp"])
    delivery = DeliveryEvidence(
        order_hash=order.order_hash,
        evidence_hash=hash_content("arc-agent-pay escrow smoke result"),
        evidence_uri="urn:arc-agent-pay:smoke-test",
        delivered_at=funded_at,
    )
    verdict = ValidationVerdict.for_delivery(
        delivery,
        approved=True,
        score=100,
        reason="automated Arc Testnet smoke test passed",
        issued_at=funded_at,
        valid_until=min(funded_at + 300, order.refund_after),
    )
    signed_verdict = sign_verdict(verdict, private_key=validator_key, order=order)
    release_tx = escrow.release(order, delivery, signed_verdict)

    final_status = escrow.status(order)
    provider_after = token.functions.balanceOf(provider).call()
    if final_status is not EscrowStatus.RELEASED:
        raise RuntimeError(f"unexpected final escrow status: {final_status.name}")
    if provider_after - provider_before != args.amount:
        raise RuntimeError("provider did not receive the exact order amount")

    print(
        json.dumps(
            {
                "chain_id": ARC_TESTNET_CHAIN_ID,
                "escrow": escrow_address,
                "asset": asset,
                "order_hash": order.order_hash,
                "amount_base_units": args.amount,
                "payer": payer.address,
                "provider": provider,
                "validator": validator.address,
                "fund_transaction": fund_tx,
                "release_transaction": release_tx,
                "final_status": final_status.name,
                "provider_balance_delta": provider_after - provider_before,
                "payer_native_balance_before_wei": payer_native_before,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
