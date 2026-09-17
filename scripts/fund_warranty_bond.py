#!/usr/bin/env python3
"""Fund the live Arc Testnet WarrantyBond from one dedicated provider EOA."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

from eth_account import Account
from web3 import Web3

from arc_agent_pay.exceptions import SettlementSubmissionOutcomeUnknown
from arc_agent_pay.onchain import load_abi, rpc_url, warranty_bond_address
from scripts.deploy_warranty_bond import _atomic_json, _safe_read


ARC_TESTNET_CHAIN_ID = 5_042_002
ARC_TESTNET_USDC = "0x3600000000000000000000000000000000000000"
WARRANTY_BOND = "0x74c4b5006136d37134B60F4Ff952E446BC11C901"
ASSURANCE_OPERATOR = "0xAD9B0B4EDa94329c72837263979b0fD3Be4BCD18"
EXPECTED_PROVIDER = "0x5cBC83e3363710772B8812Ad3AB5423116011651"
BOND_AMOUNT = 5_000_000
GAS_LIMIT_MULTIPLIER = 1.20
CONSERVATIVE_DEPOSIT_GAS = 200_000

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = (
    PROJECT_ROOT / "contracts" / "deployments" / "arc-testnet-warranty-bond-provider-funding.json"
)

ERC20_ABI = [
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "allowance",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "approve",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]


def _account() -> Any:
    private_key = os.environ.get("ASSURANCE_PROVIDER_PRIVATE_KEY", "").strip()
    if not private_key:
        raise RuntimeError("ASSURANCE_PROVIDER_PRIVATE_KEY is required")
    try:
        account = Account.from_key(private_key)
    except Exception as exc:
        raise RuntimeError("ASSURANCE_PROVIDER_PRIVATE_KEY is invalid") from exc
    if Web3.to_checksum_address(account.address) != Web3.to_checksum_address(EXPECTED_PROVIDER):
        raise RuntimeError("provider key does not derive the locked provider address")
    return account


def _read(label: str, operation: Any) -> Any:
    return _safe_read(label, operation)


def _tx_hash(value: Any) -> str:
    result = value.hex()
    if not result.startswith("0x"):
        result = "0x" + result
    if len(result) != 66:
        raise SettlementSubmissionOutcomeUnknown(
            "Arc broadcast returned an unusable transaction hash"
        )
    return result.lower()


def _broadcast_once(w3: Web3, account: Any, transaction: Mapping[str, Any]) -> str:
    signed = account.sign_transaction(dict(transaction))
    try:
        return _tx_hash(w3.eth.send_raw_transaction(signed.raw_transaction))
    except SettlementSubmissionOutcomeUnknown:
        raise
    except Exception as exc:
        from arc_agent_pay.assurance.genlayer_transport import (
            is_transient_transport_error,
        )

        if is_transient_transport_error(exc):
            raise SettlementSubmissionOutcomeUnknown(
                "Arc broadcast outcome is unknown; blind resubmission is unsafe"
            ) from exc
        raise RuntimeError(f"Arc transaction submission failed: {exc}") from exc


def _prepare_transaction(
    w3: Web3,
    account: Any,
    function: Any,
) -> tuple[dict[str, Any], int, int]:
    sender = Web3.to_checksum_address(account.address)
    nonce = int(
        _read(
            "provider pending nonce",
            lambda: w3.eth.get_transaction_count(sender, "pending"),
        )
    )
    gas_price = int(_read("Arc gas price", lambda: w3.eth.gas_price))
    estimate = int(
        _read(
            "transaction gas estimate",
            lambda: function.estimate_gas({"from": sender}),
        )
    )
    gas_limit = math.ceil(estimate * GAS_LIMIT_MULTIPLIER)
    transaction = _read(
        "transaction construction",
        lambda: function.build_transaction(
            {
                "from": sender,
                "nonce": nonce,
                "chainId": ARC_TESTNET_CHAIN_ID,
                "gas": gas_limit,
                "gasPrice": gas_price,
            }
        ),
    )
    return transaction, gas_limit, gas_price


def _wait_receipt(w3: Web3, tx_hash: str, label: str) -> Mapping[str, Any]:
    try:
        receipt = _read(
            f"{label} receipt",
            lambda: w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180),
        )
    except Exception as exc:
        raise RuntimeError(
            f"{label} receipt unresolved for {tx_hash}; resume this exact transaction"
        ) from exc
    if int(receipt.get("status", 0)) != 1:
        raise RuntimeError(f"{label} transaction reverted: {tx_hash}")
    return receipt


def _contracts(w3: Web3) -> tuple[Any, Any]:
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(ARC_TESTNET_USDC),
        abi=ERC20_ABI,
    )
    bond = w3.eth.contract(
        address=Web3.to_checksum_address(WARRANTY_BOND),
        abi=load_abi("warranty_bond"),
    )
    return usdc, bond


def _base_checks(w3: Web3, account: Any, usdc: Any, bond: Any) -> dict[str, int]:
    chain_id = int(_read("Arc chain ID", lambda: w3.eth.chain_id))
    if chain_id != ARC_TESTNET_CHAIN_ID:
        raise RuntimeError(f"refusing chain {chain_id}; expected {ARC_TESTNET_CHAIN_ID}")
    provider = Web3.to_checksum_address(account.address)
    operator = Web3.to_checksum_address(ASSURANCE_OPERATOR)
    if provider == operator or int(provider, 16) == 0:
        raise RuntimeError("provider must be non-zero and distinct from the operator")
    configured = warranty_bond_address()
    if not configured or Web3.to_checksum_address(configured) != Web3.to_checksum_address(
        WARRANTY_BOND
    ):
        raise RuntimeError("configured WarrantyBond does not match the live deployment")
    if not _read("WarrantyBond bytecode", lambda: w3.eth.get_code(bond.address)):
        raise RuntimeError("configured WarrantyBond has no runtime bytecode")
    actual_usdc = Web3.to_checksum_address(
        _read("WarrantyBond USDC", lambda: bond.functions.usdc().call())
    )
    actual_resolver = Web3.to_checksum_address(
        _read("WarrantyBond resolver", lambda: bond.functions.resolver().call())
    )
    if actual_usdc != Web3.to_checksum_address(ARC_TESTNET_USDC):
        raise RuntimeError("WarrantyBond reports the wrong USDC")
    if actual_resolver != operator:
        raise RuntimeError("WarrantyBond reports the wrong resolver")
    return {
        "native_balance_atomic": int(
            _read("provider native balance", lambda: w3.eth.get_balance(provider))
        ),
        "provider_usdc_balance": int(
            _read(
                "provider USDC balance",
                lambda: usdc.functions.balanceOf(provider).call(),
            )
        ),
        "bond_usdc_balance": int(
            _read(
                "WarrantyBond USDC balance",
                lambda: usdc.functions.balanceOf(bond.address).call(),
            )
        ),
        "allowance": int(
            _read(
                "provider allowance",
                lambda: usdc.functions.allowance(provider, bond.address).call(),
            )
        ),
        "bond_balance": int(
            _read(
                "provider bond balance",
                lambda: bond.functions.bond_balance(provider).call(),
            )
        ),
    }


def _write_artifact(value: Mapping[str, Any]) -> None:
    _atomic_json(ARTIFACT, value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-testnet-spend", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rpc", default=rpc_url())
    args = parser.parse_args()
    if not args.confirm_testnet_spend:
        parser.error("funding requires --confirm-testnet-spend")

    account = _account()
    provider = Web3.to_checksum_address(account.address)
    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
    usdc, bond = _contracts(w3)
    current = _base_checks(w3, account, usdc, bond)

    artifact: dict[str, Any]
    if ARTIFACT.exists():
        if not args.resume:
            raise RuntimeError(f"funding artifact already exists; resume with --resume: {ARTIFACT}")
        artifact = json.loads(ARTIFACT.read_text())
        if (
            artifact.get("provider") != provider
            or artifact.get("warranty_bond") != Web3.to_checksum_address(WARRANTY_BOND)
            or artifact.get("amount_atomic") != BOND_AMOUNT
        ):
            raise RuntimeError("funding artifact does not match the locked operation")
    else:
        if args.resume:
            raise RuntimeError("no funding artifact exists to resume")
        if current["provider_usdc_balance"] < BOND_AMOUNT:
            raise RuntimeError("provider has insufficient USDC")
        if current["allowance"] != 0:
            raise RuntimeError("expected initial provider allowance to be zero")
        approve_fn = usdc.functions.approve(bond.address, BOND_AMOUNT)
        approve_tx, approve_gas, gas_price = _prepare_transaction(w3, account, approve_fn)
        required_native = (approve_gas + CONSERVATIVE_DEPOSIT_GAS) * gas_price
        if current["native_balance_atomic"] < required_native:
            raise RuntimeError(
                "provider native balance cannot conservatively cover approve and deposit"
            )
        print(
            json.dumps(
                {
                    "chain_id": ARC_TESTNET_CHAIN_ID,
                    "provider": provider,
                    "operator": Web3.to_checksum_address(ASSURANCE_OPERATOR),
                    "warranty_bond": bond.address,
                    "usdc": usdc.address,
                    "bond_amount_atomic": BOND_AMOUNT,
                    "native_balance_atomic": current["native_balance_atomic"],
                    **current,
                    "estimated_native_required_atomic": required_native,
                },
                indent=2,
            ),
            flush=True,
        )
        approve_hash = _broadcast_once(w3, account, approve_tx)
        artifact = {
            "schema_version": "1",
            "status": "approve_submitted",
            "chain_id": ARC_TESTNET_CHAIN_ID,
            "provider": provider,
            "warranty_bond": bond.address,
            "usdc": usdc.address,
            "amount_atomic": BOND_AMOUNT,
            "initial_provider_usdc_balance": current["provider_usdc_balance"],
            "initial_warranty_bond_usdc_balance": current["bond_usdc_balance"],
            "initial_provider_bond_balance": current["bond_balance"],
            "approve_transaction_hash": approve_hash,
        }
        _write_artifact(artifact)
        print(f"APPROVE_TX_HASH={approve_hash}", flush=True)

    if artifact["status"] == "approve_submitted":
        approve_receipt = _wait_receipt(w3, artifact["approve_transaction_hash"], "USDC approval")
        artifact["approve_block_number"] = int(approve_receipt["blockNumber"])
        artifact["status"] = "approve_confirmed"
        _write_artifact(artifact)

    if artifact["status"] == "approve_confirmed":
        allowance = int(
            _read(
                "confirmed provider allowance",
                lambda: usdc.functions.allowance(provider, bond.address).call(),
            )
        )
        if allowance < BOND_AMOUNT:
            raise RuntimeError("confirmed USDC allowance is below the bond amount")
        deposit_fn = bond.functions.deposit(BOND_AMOUNT)
        deposit_tx, _, _ = _prepare_transaction(w3, account, deposit_fn)
        deposit_hash = _broadcast_once(w3, account, deposit_tx)
        artifact["deposit_transaction_hash"] = deposit_hash
        artifact["status"] = "deposit_submitted"
        _write_artifact(artifact)
        print(f"DEPOSIT_TX_HASH={deposit_hash}", flush=True)

    if artifact["status"] == "deposit_submitted":
        deposit_receipt = _wait_receipt(
            w3, artifact["deposit_transaction_hash"], "WarrantyBond deposit"
        )
        events = bond.events.BondDeposited().process_receipt(deposit_receipt)
        matching = [
            event
            for event in events
            if Web3.to_checksum_address(event["args"]["provider"]) == provider
            and int(event["args"]["amount"]) == BOND_AMOUNT
        ]
        if len(matching) != 1:
            raise RuntimeError("deposit receipt lacks one exact BondDeposited event")
        final = _base_checks(w3, account, usdc, bond)
        approve_receipt = _read(
            "confirmed approval receipt",
            lambda: w3.eth.get_transaction_receipt(artifact["approve_transaction_hash"]),
        )
        gas_cost_native_atomic = sum(
            int(receipt["gasUsed"]) * int(receipt["effectiveGasPrice"])
            for receipt in (approve_receipt, deposit_receipt)
        )
        # Arc's native gas token is USDC. The native ledger uses 18 decimals,
        # while balanceOf exposes 6 decimals, so the visible gas debit rounds up.
        gas_cost_usdc_atomic = (gas_cost_native_atomic + 10**12 - 1) // 10**12
        if (
            final["provider_usdc_balance"]
            != artifact["initial_provider_usdc_balance"] - BOND_AMOUNT - gas_cost_usdc_atomic
            or final["bond_usdc_balance"]
            != artifact["initial_warranty_bond_usdc_balance"] + BOND_AMOUNT
            or final["bond_balance"] != artifact["initial_provider_bond_balance"] + BOND_AMOUNT
        ):
            raise RuntimeError("confirmed deposit balances do not match exact expected deltas")
        block = _read(
            "deposit block",
            lambda: w3.eth.get_block(deposit_receipt["blockNumber"]),
        )
        artifact.update(
            {
                "status": "confirmed",
                "deposit_block_number": int(deposit_receipt["blockNumber"]),
                "resulting_provider_bond_balance": final["bond_balance"],
                "final_provider_usdc_balance": final["provider_usdc_balance"],
                "final_warranty_bond_usdc_balance": final["bond_usdc_balance"],
                "final_allowance": final["allowance"],
                "approve_gas_used": int(approve_receipt["gasUsed"]),
                "deposit_gas_used": int(deposit_receipt["gasUsed"]),
                "total_gas_cost_native_atomic": gas_cost_native_atomic,
                "visible_gas_cost_usdc_atomic": gas_cost_usdc_atomic,
                "bond_deposited_event": {
                    "provider": provider,
                    "amount_atomic": BOND_AMOUNT,
                },
                "confirmed_at": datetime.fromtimestamp(int(block["timestamp"]), tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        _write_artifact(artifact)

    print(json.dumps(artifact, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
