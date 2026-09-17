#!/usr/bin/env python3
"""Safely deploy the fixed AgentPay WarrantyBond to Arc Testnet exactly once."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Mapping, Optional

from eth_account import Account
from web3 import Web3
from vyper import __version__ as vyper_version
from vyper import compile_code

from arc_agent_pay.assurance.genlayer_transport import (
    GenLayerRetryPolicy,
    is_transient_transport_error,
)
from arc_agent_pay.exceptions import SettlementSubmissionOutcomeUnknown

ARC_TESTNET_CHAIN_ID = 5_042_002
ARC_TESTNET_RPC = "https://rpc.testnet.arc.network"
ARC_TESTNET_USDC = "0x3600000000000000000000000000000000000000"
ASSURANCE_OPERATOR = "0xAD9B0B4EDa94329c72837263979b0fD3Be4BCD18"
GAS_LIMIT_MULTIPLIER = 1.20

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = PROJECT_ROOT / "contracts" / "WarrantyBond.vy"
PACKAGED_ABI = PROJECT_ROOT / "arc_agent_pay" / "onchain" / "abi" / "warranty_bond.json"
ADDRESSES = PROJECT_ROOT / "arc_agent_pay" / "onchain" / "addresses.json"
DEPLOYMENTS = PROJECT_ROOT / "contracts" / "deployments"
ARTIFACT = DEPLOYMENTS / "arc-testnet-warranty-bond.json"
PENDING_ARTIFACT = DEPLOYMENTS / ".arc-testnet-warranty-bond-pending.json"

SAMPLE_PROVIDER = "0x000000000000000000000000000000000000dEaD"
SAMPLE_DISPUTE = "0x" + hashlib.sha256(b"agentpay-phase5a-dispute").hexdigest()
SAMPLE_PAYMENT = "0x" + hashlib.sha256(b"agentpay-phase5a-payment").hexdigest()


@dataclass(frozen=True)
class Compilation:
    source: str
    abi: list[dict[str, Any]]
    bytecode: str
    runtime_bytecode: str
    source_sha256: str
    creation_bytecode_sha256: str
    runtime_bytecode_sha256: str


@dataclass(frozen=True)
class DeploymentPreflight:
    chain_id: int
    signer: str
    native_balance_atomic: int
    usdc: str
    resolver: str
    vyper_version: str
    source_sha256: str
    creation_bytecode_sha256: str
    runtime_bytecode_sha256: str
    estimated_gas: int
    gas_limit: int
    gas_price_atomic: int
    estimated_transaction_cost_atomic: int
    nonce: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "signer": self.signer,
            "native_balance_atomic": self.native_balance_atomic,
            "usdc": self.usdc,
            "resolver": self.resolver,
            "vyper_version": self.vyper_version,
            "source_sha256": self.source_sha256,
            "creation_bytecode_sha256": self.creation_bytecode_sha256,
            "runtime_bytecode_sha256": self.runtime_bytecode_sha256,
            "estimated_gas": self.estimated_gas,
            "gas_limit": self.gas_limit,
            "gas_price_atomic": self.gas_price_atomic,
            "estimated_transaction_cost_atomic": self.estimated_transaction_cost_atomic,
        }


def _abi_signature(abi: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            entry.get("type"),
            entry.get("name", ""),
            tuple(
                (item.get("name"), item.get("type"), item.get("indexed"))
                for item in entry.get("inputs", [])
            ),
            tuple((item.get("name"), item.get("type")) for item in entry.get("outputs", [])),
            entry.get("stateMutability"),
        )
        for entry in abi
    )


def compile_warranty_bond() -> Compilation:
    source = CONTRACT.read_text()
    compiled = compile_code(
        source,
        contract_path=CONTRACT,
        output_formats=["abi", "bytecode", "bytecode_runtime"],
    )
    packaged = json.loads(PACKAGED_ABI.read_text())
    if _abi_signature(compiled["abi"]) != _abi_signature(packaged):
        raise RuntimeError("packaged WarrantyBond ABI does not match current source")
    constructors = [entry for entry in compiled["abi"] if entry.get("type") == "constructor"]
    expected_inputs = [("_usdc", "address"), ("_resolver", "address")]
    if (
        len(constructors) != 1
        or [(item.get("name"), item.get("type")) for item in constructors[0].get("inputs", [])]
        != expected_inputs
    ):
        raise RuntimeError("WarrantyBond constructor ABI is not (address,address)")
    if vyper_version != "0.4.3" or "#pragma version ==0.4.3" not in source:
        raise RuntimeError("WarrantyBond must compile with the pinned Vyper 0.4.3")
    creation = bytes.fromhex(compiled["bytecode"][2:])
    runtime = bytes.fromhex(compiled["bytecode_runtime"][2:])
    return Compilation(
        source=source,
        abi=compiled["abi"],
        bytecode=compiled["bytecode"],
        runtime_bytecode=compiled["bytecode_runtime"],
        source_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        creation_bytecode_sha256=hashlib.sha256(creation).hexdigest(),
        runtime_bytecode_sha256=hashlib.sha256(runtime).hexdigest(),
    )


def _safe_read(
    label: str,
    operation: Callable[[], Any],
    *,
    policy: Optional[GenLayerRetryPolicy] = None,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = lambda: 0.0,
) -> Any:
    selected = policy or GenLayerRetryPolicy()
    for attempt in range(1, selected.max_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if not is_transient_transport_error(exc):
                raise RuntimeError(f"{label} failed: {exc}") from exc
            if attempt == selected.max_attempts:
                raise RuntimeError(f"{label} transport failed after {attempt} attempts") from exc
            sleep(selected.delay_before_attempt(attempt + 1, jitter()))
    raise RuntimeError("unreachable retry state")


def _configured_warranty_bond(
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    source = os.environ if environ is None else environ
    env_address = source.get("WARRANTY_BOND_ADDRESS", "").strip()
    configured = json.loads(ADDRESSES.read_text())["arc_testnet"]["warranty_bond"]
    return env_address or configured


def _assert_no_existing_deployment(
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    configured = _configured_warranty_bond(environ)
    if configured:
        raise RuntimeError(
            f"WarrantyBond is already configured at {configured}; refusing redeployment"
        )
    if ARTIFACT.exists():
        raise RuntimeError(f"WarrantyBond deployment artifact already exists: {ARTIFACT}")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            json.dump(value, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _persist_configured_address(address: str) -> None:
    document = json.loads(ADDRESSES.read_text())
    existing = document["arc_testnet"].get("warranty_bond")
    if existing and existing.lower() != address.lower():
        raise RuntimeError(
            f"refusing to overwrite configured WarrantyBond {existing} with {address}"
        )
    document["arc_testnet"]["warranty_bond"] = address
    _atomic_json(ADDRESSES, document)


def deployment_preflight(
    w3: Web3,
    account: Any,
    compilation: Compilation,
) -> tuple[DeploymentPreflight, Any]:
    _assert_no_existing_deployment()
    chain_id = int(_safe_read("Arc chain ID", lambda: w3.eth.chain_id))
    if chain_id != ARC_TESTNET_CHAIN_ID:
        raise RuntimeError(
            f"refusing chain {chain_id}; expected Arc Testnet {ARC_TESTNET_CHAIN_ID}"
        )
    signer = Web3.to_checksum_address(account.address)
    expected_operator = Web3.to_checksum_address(ASSURANCE_OPERATOR)
    if signer != expected_operator:
        raise RuntimeError(f"signer {signer} is not the Assurance operator {expected_operator}")
    usdc = Web3.to_checksum_address(ARC_TESTNET_USDC)
    usdc_code = _safe_read("Arc USDC bytecode", lambda: w3.eth.get_code(usdc))
    if not usdc_code:
        raise RuntimeError(f"configured Arc Testnet USDC has no code: {usdc}")
    balance = int(_safe_read("operator native balance", lambda: w3.eth.get_balance(signer)))
    nonce = int(
        _safe_read(
            "operator pending nonce",
            lambda: w3.eth.get_transaction_count(signer, "pending"),
        )
    )
    gas_price = int(_safe_read("Arc gas price", lambda: w3.eth.gas_price))
    factory = w3.eth.contract(abi=compilation.abi, bytecode=compilation.bytecode)
    constructor = factory.constructor(usdc, expected_operator)
    estimate = int(
        _safe_read(
            "WarrantyBond deployment gas estimate",
            lambda: constructor.estimate_gas({"from": signer}),
        )
    )
    gas_limit = math.ceil(estimate * GAS_LIMIT_MULTIPLIER)
    cost = gas_limit * gas_price
    preflight = DeploymentPreflight(
        chain_id=chain_id,
        signer=signer,
        native_balance_atomic=balance,
        usdc=usdc,
        resolver=expected_operator,
        vyper_version=vyper_version,
        source_sha256=compilation.source_sha256,
        creation_bytecode_sha256=compilation.creation_bytecode_sha256,
        runtime_bytecode_sha256=compilation.runtime_bytecode_sha256,
        estimated_gas=estimate,
        gas_limit=gas_limit,
        gas_price_atomic=gas_price,
        estimated_transaction_cost_atomic=cost,
        nonce=nonce,
    )
    return preflight, constructor


def _require_sufficient_funds(preflight: DeploymentPreflight) -> None:
    if preflight.native_balance_atomic < preflight.estimated_transaction_cost_atomic:
        raise RuntimeError(
            f"operator has {preflight.native_balance_atomic} atomic native token; "
            "deployment requires approximately "
            f"{preflight.estimated_transaction_cost_atomic} at the safety gas limit"
        )


def _broadcast_once(w3: Web3, account: Any, transaction: dict[str, Any]) -> str:
    signed = account.sign_transaction(transaction)
    try:
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    except Exception as exc:
        if is_transient_transport_error(exc):
            raise SettlementSubmissionOutcomeUnknown(
                "Arc WarrantyBond deployment broadcast outcome is unknown"
            ) from exc
        raise RuntimeError(f"WarrantyBond deployment submission failed: {exc}") from exc
    value = tx_hash.hex()
    if not value.startswith("0x"):
        value = "0x" + value
    if len(value) != 66:
        raise SettlementSubmissionOutcomeUnknown(
            "Arc accepted the deployment but returned an unusable transaction hash"
        )
    return value.lower()


def _verify_and_finalize(
    w3: Web3,
    compilation: Compilation,
    preflight: DeploymentPreflight,
    tx_hash: str,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    if int(receipt.get("status", 0)) != 1:
        raise RuntimeError(f"WarrantyBond deployment reverted ({tx_hash})")
    raw_address = receipt.get("contractAddress")
    if not raw_address:
        raise RuntimeError("deployment receipt has no contract address")
    address = Web3.to_checksum_address(raw_address)
    runtime_code = _safe_read(
        "deployed WarrantyBond bytecode",
        lambda: w3.eth.get_code(address),
    )
    if not runtime_code:
        raise RuntimeError("confirmed WarrantyBond address has no bytecode")
    deployed = w3.eth.contract(address=address, abi=compilation.abi)
    configured_usdc = _safe_read(
        "deployed WarrantyBond USDC",
        lambda: deployed.functions.usdc().call(),
    )
    if Web3.to_checksum_address(configured_usdc) != preflight.usdc:
        raise RuntimeError("deployed WarrantyBond reports the wrong USDC")
    configured_resolver = _safe_read(
        "deployed WarrantyBond resolver",
        lambda: deployed.functions.resolver().call(),
    )
    if Web3.to_checksum_address(configured_resolver) != preflight.resolver:
        raise RuntimeError("deployed WarrantyBond reports the wrong resolver")
    sample_provider = Web3.to_checksum_address(SAMPLE_PROVIDER)
    sample_balance = _safe_read(
        "deployed WarrantyBond sample balance",
        lambda: deployed.functions.bond_balance(sample_provider).call(),
    )
    if int(sample_balance) != 0:
        raise RuntimeError("new WarrantyBond has a non-zero initial provider balance")
    sample_dispute_processed = _safe_read(
        "deployed WarrantyBond sample dispute replay flag",
        lambda: deployed.functions.processed_disputes(bytes.fromhex(SAMPLE_DISPUTE[2:])).call(),
    )
    if sample_dispute_processed:
        raise RuntimeError("new WarrantyBond has a consumed sample dispute")
    sample_payment_refunded = _safe_read(
        "deployed WarrantyBond sample payment replay flag",
        lambda: deployed.functions.refunded_payments(bytes.fromhex(SAMPLE_PAYMENT[2:])).call(),
    )
    if sample_payment_refunded:
        raise RuntimeError("new WarrantyBond has a refunded sample payment")

    block_number = int(receipt.get("blockNumber", 0))
    block = _safe_read("deployment block", lambda: w3.eth.get_block(block_number))
    deployed_at = (
        datetime.fromtimestamp(
            int(block["timestamp"]),
            tz=timezone.utc,
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
    artifact = {
        "schema_version": "1",
        "chain_id": preflight.chain_id,
        "contract_address": address,
        "deployment_transaction_hash": tx_hash,
        "deployer": preflight.signer,
        "resolver": preflight.resolver,
        "usdc": preflight.usdc,
        "block_number": block_number,
        "gas_used": int(receipt.get("gasUsed", 0)),
        "deployed_at": deployed_at,
        "vyper_version": preflight.vyper_version,
        "source_sha256": preflight.source_sha256,
        "creation_bytecode_sha256": preflight.creation_bytecode_sha256,
        "compiled_runtime_bytecode_sha256": preflight.runtime_bytecode_sha256,
        "deployed_runtime_code_keccak": Web3.keccak(runtime_code).hex(),
    }
    _atomic_json(ARTIFACT, artifact)
    _persist_configured_address(address)
    return artifact


def _load_account(environ: Optional[Mapping[str, str]] = None) -> Any:
    source = os.environ if environ is None else environ
    private_key = source.get("ASSURANCE_OPERATOR_PRIVATE_KEY", "").strip()
    if not private_key:
        raise RuntimeError("ASSURANCE_OPERATOR_PRIVATE_KEY is required")
    try:
        return Account.from_key(private_key)
    except Exception as exc:
        raise RuntimeError("ASSURANCE_OPERATOR_PRIVATE_KEY is invalid") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc", default=os.environ.get("ARC_TESTNET_RPC", ARC_TESTNET_RPC))
    parser.add_argument("--confirm-testnet", action="store_true")
    parser.add_argument("--resume-transaction-hash")
    args = parser.parse_args()
    if not args.confirm_testnet:
        parser.error("deployment requires --confirm-testnet")

    account = _load_account()
    compilation = compile_warranty_bond()
    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
    if not _safe_read("Arc RPC connection", w3.is_connected):
        raise RuntimeError(f"could not connect to Arc Testnet RPC: {args.rpc}")

    preflight, constructor = deployment_preflight(w3, account, compilation)
    print(json.dumps({"preflight": preflight.as_dict()}, indent=2), flush=True)

    if args.resume_transaction_hash:
        tx_hash = args.resume_transaction_hash.lower()
        if PENDING_ARTIFACT.exists():
            pending = json.loads(PENDING_ARTIFACT.read_text())
            if pending.get("deployment_transaction_hash") != tx_hash:
                raise RuntimeError("resume transaction does not match pending artifact")
    else:
        _require_sufficient_funds(preflight)
        if PENDING_ARTIFACT.exists():
            pending = json.loads(PENDING_ARTIFACT.read_text())
            raise RuntimeError(
                "a deployment may already be pending; resume transaction "
                f"{pending.get('deployment_transaction_hash')}"
            )
        transaction = constructor.build_transaction(
            {
                "from": preflight.signer,
                "nonce": preflight.nonce,
                "chainId": preflight.chain_id,
                "gas": preflight.gas_limit,
                "gasPrice": preflight.gas_price_atomic,
            }
        )
        tx_hash = _broadcast_once(w3, account, transaction)
        _atomic_json(
            PENDING_ARTIFACT,
            {
                "chain_id": preflight.chain_id,
                "deployment_transaction_hash": tx_hash,
                "deployer": preflight.signer,
                "source_sha256": preflight.source_sha256,
            },
        )
        print(f"WARRANTY_BOND_DEPLOYMENT_TX_HASH={tx_hash}", flush=True)

    try:
        receipt = _safe_read(
            "WarrantyBond deployment receipt",
            lambda: w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180),
        )
    except Exception as exc:
        raise RuntimeError(
            "deployment receipt is unresolved; do not resubmit. Resume with "
            f"--resume-transaction-hash {tx_hash}"
        ) from exc
    artifact = _verify_and_finalize(w3, compilation, preflight, tx_hash, receipt)
    print(json.dumps({"deployment": artifact}, indent=2), flush=True)


if __name__ == "__main__":
    main()
