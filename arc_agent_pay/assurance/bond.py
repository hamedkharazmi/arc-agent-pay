"""Web3 client for the Arc Testnet AgentPay Assurance WarrantyBond.

The contract does not validate GenLayer verdicts. During the hackathon MVP, a
trusted off-chain resolver verifies a finalized verdict and the original x402
payment before calling :meth:`WarrantyBondClient.refund`. Trustless cross-chain
verification is deliberately outside this client and the MVP.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Optional

from eth_utils import to_checksum_address

from ..exceptions import (
    SettlementSubmissionOutcomeUnknown,
    SettlementTransactionFailed,
    WorkflowError,
)
from ..onchain import load_abi, rpc_url, warranty_bond_address
from ..workflow.models import (
    _MAX_UINT256,
    _normalize_address,
    _normalize_nonzero_bytes32,
    bytes32,
)
from .genlayer_transport import GenLayerRetryPolicy, is_transient_transport_error


def _positive_amount(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("amount must be an integer in token base units")
    if not 0 < value <= _MAX_UINT256:
        raise ValueError("amount must fit a non-zero uint256")
    return value


@dataclass(frozen=True)
class BondTransactionResult:
    """Confirmed WarrantyBond transaction and its receipt metadata."""

    tx_hash: str
    block_number: int
    gas_used: Optional[int]
    receipt: Any


class WarrantyBondClient:
    """Read and transact with one fixed-USDC WarrantyBond deployment.

    ``deposit`` requires the provider to approve the bond contract to spend the
    requested USDC amount first. Amounts are token base units. This class only
    performs contract wiring and input validation; it contains no verdict,
    dispute, x402 receipt, or relay logic.
    """

    def __init__(
        self,
        address: Optional[str] = None,
        *,
        account: Any = None,
        rpc: Optional[str] = None,
        w3: Any = None,
        contract: Any = None,
        retry_policy: Optional[GenLayerRetryPolicy] = None,
        sleep: Any = time.sleep,
        jitter: Any = lambda: 0.0,
    ) -> None:
        configured_address = address or warranty_bond_address()
        if not configured_address:
            raise WorkflowError(
                "WarrantyBond is not deployed/configured; provide address or "
                "set WARRANTY_BOND_ADDRESS"
            )
        normalized = _normalize_address(configured_address)
        self.account = account
        self.retry_policy = retry_policy or GenLayerRetryPolicy()
        self._sleep = sleep
        self._jitter = jitter
        if contract is not None:
            self.w3 = w3
            self.contract = contract
            self.address = normalized
            return
        try:
            from web3 import Web3
        except ImportError as exc:  # pragma: no cover - import guard
            raise ImportError(
                'WarrantyBondClient requires web3. Install with: pip install "arc-agent-pay[onchain]"'
            ) from exc
        self.w3 = w3 or Web3(Web3.HTTPProvider(rpc or rpc_url()))
        checksum = self.w3.to_checksum_address(normalized)
        self.address = checksum.lower()
        self.contract = self.w3.eth.contract(
            address=checksum,
            abi=load_abi("warranty_bond"),
        )

    def bond_balance(self, provider: str) -> int:
        normalized = _normalize_address(provider)
        return int(
            self.contract.functions.bond_balance(to_checksum_address(normalized)).call()
        )

    def chain_id(self) -> int:
        if self.w3 is None:
            raise WorkflowError("WarrantyBond chain query requires Web3")
        return int(self.w3.eth.chain_id)

    def resolver(self) -> str:
        return _normalize_address(self.contract.functions.resolver().call())

    def usdc(self) -> str:
        return _normalize_address(self.contract.functions.usdc().call())

    def dispute_processed(self, dispute_id: str | bytes) -> bool:
        normalized = _normalize_nonzero_bytes32(dispute_id)
        return bool(
            self.contract.functions.processed_disputes(bytes32(normalized)).call()
        )

    def payment_refunded(self, payment_id_hash: str | bytes) -> bool:
        normalized = _normalize_nonzero_bytes32(payment_id_hash)
        return bool(
            self.contract.functions.refunded_payments(bytes32(normalized)).call()
        )

    def deposit(self, amount: int) -> BondTransactionResult:
        return self._send(self.contract.functions.deposit(_positive_amount(amount)))

    def refund(
        self,
        dispute_id: str | bytes,
        payment_id_hash: str | bytes,
        provider: str,
        buyer: str,
        payment_amount: int,
    ) -> BondTransactionResult:
        tx_hash = self.submit_refund(
            dispute_id,
            payment_id_hash,
            provider,
            buyer,
            payment_amount,
        )
        try:
            return self.wait_for_refund(tx_hash)
        except SettlementTransactionFailed as exc:
            # Preserve the Phase 2 combined-client exception contract.
            raise WorkflowError(str(exc)) from exc

    def submit_refund(
        self,
        dispute_id: str | bytes,
        payment_id_hash: str | bytes,
        provider: str,
        buyer: str,
        payment_amount: int,
    ) -> str:
        """Broadcast one refund exactly once and return its Arc tx hash."""
        normalized_dispute = _normalize_nonzero_bytes32(dispute_id)
        normalized_payment = _normalize_nonzero_bytes32(payment_id_hash)
        normalized_provider = _normalize_address(provider)
        normalized_buyer = _normalize_address(buyer)
        amount = _positive_amount(payment_amount)
        fn = self.contract.functions.refund(
            bytes32(normalized_dispute),
            bytes32(normalized_payment),
            to_checksum_address(normalized_provider),
            to_checksum_address(normalized_buyer),
            amount,
        )
        return self._submit(fn)

    def wait_for_refund(self, tx_hash: str) -> BondTransactionResult:
        """Resolve only an already-known Arc transaction; never resubmit it."""
        normalized = _normalize_nonzero_bytes32(tx_hash)
        if self.w3 is None:
            raise WorkflowError("WarrantyBond receipt lookup requires Web3")
        receipt = self._safe_transport_call(
            "WarrantyBond receipt lookup",
            lambda: self.w3.eth.wait_for_transaction_receipt(normalized),
        )
        if int(receipt.get("status", 0)) != 1:
            raise SettlementTransactionFailed(
                f"WarrantyBond refund reverted on-chain ({normalized})"
            )
        return BondTransactionResult(
            tx_hash=normalized,
            block_number=int(receipt.get("blockNumber", 0)),
            gas_used=(
                int(receipt["gasUsed"])
                if receipt.get("gasUsed") is not None
                else None
            ),
            receipt=receipt,
        )

    def _send(self, fn: Any) -> BondTransactionResult:
        tx_hash = self._submit(fn)
        try:
            return self.wait_for_refund(tx_hash)
        except SettlementTransactionFailed as exc:
            raise WorkflowError(str(exc)) from exc

    def _submit(self, fn: Any) -> str:
        if self.account is None or self.w3 is None:
            raise WorkflowError("WarrantyBond writes require both an account and Web3 client")
        tx = self._prepare_transaction(fn)
        signed = self.account.sign_transaction(tx)
        try:
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        except Exception as exc:
            if is_transient_transport_error(exc):
                raise SettlementSubmissionOutcomeUnknown(
                    "Arc transport failed during WarrantyBond broadcast"
                ) from exc
            raise WorkflowError(f"WarrantyBond transaction submission failed: {exc}") from exc
        try:
            value = tx_hash.hex()
            if not value.startswith("0x"):
                value = "0x" + value
            return _normalize_nonzero_bytes32(value)
        except Exception as exc:
            raise SettlementSubmissionOutcomeUnknown(
                "Arc accepted a WarrantyBond broadcast but returned an unusable tx hash"
            ) from exc

    def _prepare_transaction(self, fn: Any) -> Any:
        return self._safe_transport_call(
            "WarrantyBond transaction preparation",
            lambda: fn.build_transaction(
                {
                    "from": self.account.address,
                    "nonce": self.w3.eth.get_transaction_count(
                        self.account.address,
                        "pending",
                    ),
                }
            ),
        )

    def _safe_transport_call(self, label: str, operation: Any) -> Any:
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                return operation()
            except Exception as exc:
                if not is_transient_transport_error(exc):
                    raise
                if attempt == self.retry_policy.max_attempts:
                    raise WorkflowError(
                        f"{label} transport retries exhausted"
                    ) from exc
                self._sleep(
                    self.retry_policy.delay_before_attempt(
                        attempt + 1,
                        self._jitter(),
                    )
                )
        raise RuntimeError("unreachable retry state")
