#!/usr/bin/env python3
"""Compile and deploy ValidationEscrow to Arc Testnet.

This script intentionally refuses every other chain. Set the deployer key only
for the command invocation; never store it in the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from eth_account import Account
from web3 import Web3
from vyper import compile_code


ARC_TESTNET_CHAIN_ID = 5_042_002
ARC_TESTNET_RPC = "https://rpc.testnet.arc.network"
ARC_TESTNET_USDC = "0x3600000000000000000000000000000000000000"
CONTRACT = Path(__file__).parents[1] / "contracts" / "ValidationEscrow.vy"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default=os.environ.get("ARC_TESTNET_RPC", ARC_TESTNET_RPC))
    parser.add_argument(
        "--asset",
        default=os.environ.get("ARC_TESTNET_USDC", ARC_TESTNET_USDC),
        help="immutable Arc USDC contract address",
    )
    parser.add_argument(
        "--confirm-testnet",
        action="store_true",
        help="required acknowledgement that this unaudited contract is testnet-only",
    )
    args = parser.parse_args()
    if not args.confirm_testnet:
        parser.error("deployment requires --confirm-testnet")

    private_key = os.environ.get("ESCROW_DEPLOYER_PRIVATE_KEY")
    if not private_key:
        parser.error("ESCROW_DEPLOYER_PRIVATE_KEY is required")

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
    actual_chain_id = w3.eth.chain_id
    if actual_chain_id != ARC_TESTNET_CHAIN_ID:
        raise RuntimeError(
            f"refusing chain {actual_chain_id}; expected Arc Testnet {ARC_TESTNET_CHAIN_ID}"
        )

    asset = w3.to_checksum_address(args.asset)
    if not w3.eth.get_code(asset):
        raise RuntimeError(f"asset has no contract code: {asset}")

    source = CONTRACT.read_text()
    compiled = compile_code(
        source,
        contract_path=CONTRACT,
        output_formats=["abi", "bytecode", "bytecode_runtime"],
    )
    account = Account.from_key(private_key)
    factory = w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])
    tx = factory.constructor(asset).build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            "chainId": actual_chain_id,
        }
    )
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if int(receipt.get("status", 0)) != 1:
        raise RuntimeError(f"deployment reverted ({tx_hash.hex()})")
    address = receipt.get("contractAddress")
    runtime_code = w3.eth.get_code(address) if address else b""
    if not address or not runtime_code:
        raise RuntimeError("deployment receipt did not contain a live contract")
    deployed = w3.eth.contract(address=address, abi=compiled["abi"])
    if deployed.functions.asset().call() != asset:
        raise RuntimeError("deployed contract reports the wrong immutable asset")

    print(
        json.dumps(
            {
                "contract": address,
                "transaction": tx_hash.hex(),
                "deployer": account.address,
                "asset": asset,
                "chain_id": actual_chain_id,
                "source": str(CONTRACT),
                "block_number": receipt.get("blockNumber"),
                "gas_used": receipt.get("gasUsed"),
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "creation_bytecode_sha256": hashlib.sha256(
                    bytes.fromhex(compiled["bytecode"][2:])
                ).hexdigest(),
                "runtime_code_keccak": w3.keccak(runtime_code).hex(),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
