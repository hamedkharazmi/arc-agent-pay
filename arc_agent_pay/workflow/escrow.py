"""Web3 client for the ArcAgentPay validation escrow contract."""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Optional

from eth_utils import keccak, to_checksum_address

from ..exceptions import InvalidVerdictError, WorkflowError
from ..onchain import load_abi, rpc_url
from .funding import verify_funding_authorization
from .models import (
    DeliveryEvidence,
    SignedFundingAuthorization,
    SignedValidationVerdict,
    ValidationVerdict,
    WorkOrder,
    _normalize_address,
    bytes32,
)
from .signing import signature_parts, verify_signed_verdict


class EscrowStatus(IntEnum):
    NONE = 0
    FUNDED = 1
    RELEASED = 2
    REFUNDED = 3


def order_tuple(order: WorkOrder) -> tuple[Any, ...]:
    """Encode a work order in the contract ABI's struct field order."""
    return (
        to_checksum_address(order.escrow),
        to_checksum_address(order.payer),
        to_checksum_address(order.provider),
        to_checksum_address(order.validator),
        to_checksum_address(order.asset),
        order.amount,
        order.chain_id,
        order.delivery_deadline,
        order.refund_after,
        bytes32(order.task_hash),
        bytes32(order.nonce),
    )


def delivery_tuple(delivery: DeliveryEvidence) -> tuple[Any, ...]:
    """Encode delivery content, URI commitment, and time for the contract."""
    return (
        bytes32(delivery.order_hash),
        bytes32(delivery.evidence_hash),
        keccak(text=delivery.evidence_uri),
        delivery.delivered_at,
    )


def verdict_tuple(verdict: ValidationVerdict) -> tuple[Any, ...]:
    """Encode a validation verdict in the contract ABI's struct field order."""
    return (
        bytes32(verdict.order_hash),
        bytes32(verdict.evidence_hash),
        bytes32(verdict.delivery_hash),
        verdict.approved,
        verdict.score,
        bytes32(verdict.reason_hash),
        verdict.issued_at,
        verdict.valid_until,
    )


class EscrowClient:
    """Fund and resolve validation-gated orders on one deployed escrow contract."""

    def __init__(
        self,
        address: str,
        *,
        account: Any = None,
        rpc: Optional[str] = None,
        w3: Any = None,
        contract: Any = None,
    ) -> None:
        self.account = account
        if contract is not None:
            self.w3 = w3
            self.contract = contract
            self.address = _normalize_address(address)
            return
        try:
            from web3 import Web3
        except ImportError as exc:  # pragma: no cover - import guard
            raise ImportError(
                'EscrowClient requires web3. Install with: pip install "arc-agent-pay[onchain]"'
            ) from exc
        self.w3 = w3 or Web3(Web3.HTTPProvider(rpc or rpc_url()))
        checksum = self.w3.to_checksum_address(address)
        self.address = checksum.lower()
        self.contract = self.w3.eth.contract(
            address=checksum,
            abi=load_abi("validation_escrow"),
        )

    def status(self, order: WorkOrder | str) -> EscrowStatus:
        order_hash = order.order_hash if isinstance(order, WorkOrder) else order
        return EscrowStatus(self.contract.functions.status(bytes32(order_hash)).call())

    def fund(
        self,
        order: WorkOrder,
        authorization: SignedFundingAuthorization,
    ) -> str:
        self._require_order_contract(order)
        verify_funding_authorization(authorization, order=order)
        v, r, s = signature_parts(authorization.signature)
        return self._send(self.contract.functions.fund(order_tuple(order), v, r, s))

    def release(
        self,
        order: WorkOrder,
        delivery: DeliveryEvidence,
        signed: SignedValidationVerdict,
    ) -> str:
        self._require_order_contract(order)
        verify_signed_verdict(
            signed,
            order=order,
            delivery=delivery,
            now=self._chain_timestamp(),
            max_clock_skew=0,
            require_approval=True,
        )
        v, r, s = signature_parts(signed.signature)
        return self._send(
            self.contract.functions.release(
                order_tuple(order),
                delivery_tuple(delivery),
                verdict_tuple(signed.verdict),
                v,
                r,
                s,
            )
        )

    def refund_rejected(
        self,
        order: WorkOrder,
        delivery: DeliveryEvidence,
        signed: SignedValidationVerdict,
    ) -> str:
        self._require_order_contract(order)
        verify_signed_verdict(
            signed,
            order=order,
            delivery=delivery,
            now=self._chain_timestamp(),
            max_clock_skew=0,
        )
        if signed.verdict.approved:
            raise InvalidVerdictError("approved verdict cannot authorize a refund")
        v, r, s = signature_parts(signed.signature)
        return self._send(
            self.contract.functions.refund_rejected(
                order_tuple(order),
                delivery_tuple(delivery),
                verdict_tuple(signed.verdict),
                v,
                r,
                s,
            )
        )

    def refund_timeout(self, order: WorkOrder) -> str:
        self._require_order_contract(order)
        return self._send(self.contract.functions.refund_timeout(order_tuple(order)))

    def _require_order_contract(self, order: WorkOrder) -> None:
        if order.escrow != self.address:
            raise WorkflowError("work order belongs to a different escrow contract")

    def _chain_timestamp(self) -> int:
        if self.w3 is None:
            raise WorkflowError("chain timestamp requires an injected Web3 client")
        return int(self.w3.eth.get_block("latest")["timestamp"])

    def _send(self, fn: Any) -> str:
        if self.account is None or self.w3 is None:
            raise WorkflowError("escrow writes require both an account and Web3 client")
        tx = fn.build_transaction(
            {
                "from": self.account.address,
                "nonce": self.w3.eth.get_transaction_count(
                    self.account.address,
                    "pending",
                ),
            }
        )
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        if int(receipt.get("status", 0)) != 1:
            raise WorkflowError(f"escrow transaction reverted on-chain ({tx_hash.hex()})")
        return tx_hash.hex()
