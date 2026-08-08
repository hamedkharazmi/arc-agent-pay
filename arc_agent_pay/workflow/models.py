"""Partner-neutral data models for validation-gated payment workflows.

An order is fixed before work begins. Delivery evidence is attached afterward,
and a validator signs a verdict that binds both hashes. The fixed-width hashing
layout is intentionally Solidity-friendly so the same identifiers can be used
by an escrow contract without serializing JSON on-chain.
"""

from __future__ import annotations

import re
from typing import Any, Self

from eth_utils import keccak
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


WORK_ORDER_TYPE = (
    "WorkOrder(address escrow,address payer,address provider,address validator,"
    "address asset,uint256 amount,uint256 chainId,uint256 deliveryDeadline,"
    "uint256 refundAfter,bytes32 taskHash,bytes32 nonce)"
)
DELIVERY_TYPE = (
    "DeliveryEvidence(bytes32 orderHash,bytes32 evidenceHash,bytes32 uriHash,"
    "uint256 deliveredAt)"
)

WORK_ORDER_TYPE_HASH = keccak(text=WORK_ORDER_TYPE)
DELIVERY_TYPE_HASH = keccak(text=DELIVERY_TYPE)

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BYTES32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_SIGNATURE_RE = re.compile(r"^0x[0-9a-fA-F]{130}$")
_ZERO_ADDRESS = "0x" + "0" * 40
_MAX_UINT256 = 2**256 - 1


def _normalize_address(value: Any) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ValueError("must be a 20-byte 0x-prefixed EVM address")
    normalized = value.lower()
    if normalized == _ZERO_ADDRESS:
        raise ValueError("zero address is not allowed")
    return normalized


def _normalize_bytes32(value: Any) -> str:
    if isinstance(value, bytes):
        if len(value) != 32:
            raise ValueError("must be exactly 32 bytes")
        return "0x" + value.hex()
    if not isinstance(value, str) or not _BYTES32_RE.fullmatch(value):
        raise ValueError("must be a 32-byte 0x-prefixed hex value")
    return value.lower()


def _normalize_nonzero_bytes32(value: Any) -> str:
    normalized = _normalize_bytes32(value)
    if normalized == "0x" + "0" * 64:
        raise ValueError("zero bytes32 value is not allowed")
    return normalized


def _normalize_signature(value: Any) -> str:
    if isinstance(value, bytes):
        if len(value) != 65:
            raise ValueError("must be exactly 65 bytes")
        return "0x" + value.hex()
    if not isinstance(value, str) or not _SIGNATURE_RE.fullmatch(value):
        raise ValueError("must be a 65-byte 0x-prefixed hex signature")
    return value.lower()


def _uint256_word(value: int) -> bytes:
    if not 0 <= value <= _MAX_UINT256:
        raise ValueError("value must fit uint256")
    return value.to_bytes(32, byteorder="big")


def _address_word(value: str) -> bytes:
    return b"\x00" * 12 + bytes.fromhex(value[2:])


def bytes32(value: str) -> bytes:
    """Convert a validated 0x-prefixed bytes32 value to raw bytes."""
    return bytes.fromhex(value[2:])


def hash_content(content: str | bytes) -> str:
    """Keccak-256 an exact UTF-8 string or byte sequence."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if not isinstance(raw, bytes):
        raise TypeError("content must be str or bytes")
    return "0x" + keccak(raw).hex()


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class WorkOrder(_FrozenModel):
    """Terms fixed before funds are authorized or work begins.

    ``amount`` is the asset's smallest unit (for USDC, 1 USDC = 1_000_000).
    ``nonce`` makes otherwise identical repeat orders unique. ``escrow`` and
    ``chain_id`` provide cross-contract and cross-chain replay separation.
    """

    escrow: str
    payer: str
    provider: str
    validator: str
    asset: str
    amount: int = Field(gt=0, le=_MAX_UINT256)
    chain_id: int = Field(gt=0, le=_MAX_UINT256)
    delivery_deadline: int = Field(gt=0, le=_MAX_UINT256)
    refund_after: int = Field(gt=0, le=_MAX_UINT256)
    task_hash: str
    nonce: str

    _addresses = field_validator(
        "escrow", "payer", "provider", "validator", "asset", mode="before"
    )(_normalize_address)
    _hashes = field_validator("task_hash", "nonce", mode="before")(
        _normalize_nonzero_bytes32
    )

    @model_validator(mode="after")
    def deadlines_are_ordered(self) -> Self:
        if self.refund_after <= self.delivery_deadline:
            raise ValueError("refund_after must be greater than delivery_deadline")
        if len({self.payer, self.provider, self.validator}) != 3:
            raise ValueError("payer, provider, and validator must be distinct")
        if self.escrow in {self.payer, self.provider, self.validator}:
            raise ValueError("escrow must be distinct from every workflow party")
        return self

    @property
    def order_hash_bytes(self) -> bytes:
        """EIP-712-style struct hash matching ``keccak256(abi.encode(...))``."""
        encoded = b"".join(
            [
                WORK_ORDER_TYPE_HASH,
                _address_word(self.escrow),
                _address_word(self.payer),
                _address_word(self.provider),
                _address_word(self.validator),
                _address_word(self.asset),
                _uint256_word(self.amount),
                _uint256_word(self.chain_id),
                _uint256_word(self.delivery_deadline),
                _uint256_word(self.refund_after),
                bytes32(self.task_hash),
                bytes32(self.nonce),
            ]
        )
        return keccak(encoded)

    @property
    def order_hash(self) -> str:
        return "0x" + self.order_hash_bytes.hex()


class DeliveryEvidence(_FrozenModel):
    """Content commitment submitted after the provider delivers the work."""

    order_hash: str
    evidence_hash: str
    evidence_uri: str = ""
    delivered_at: int = Field(gt=0, le=_MAX_UINT256)

    _hashes = field_validator("order_hash", "evidence_hash", mode="before")(
        _normalize_nonzero_bytes32
    )

    @property
    def delivery_hash_bytes(self) -> bytes:
        encoded = b"".join(
            [
                DELIVERY_TYPE_HASH,
                bytes32(self.order_hash),
                bytes32(self.evidence_hash),
                keccak(text=self.evidence_uri),
                _uint256_word(self.delivered_at),
            ]
        )
        return keccak(encoded)

    @property
    def delivery_hash(self) -> str:
        return "0x" + self.delivery_hash_bytes.hex()


class ValidationVerdict(_FrozenModel):
    """Unsigned validator decision; EIP-712 signing lives in ``signing.py``."""

    order_hash: str
    evidence_hash: str
    delivery_hash: str
    approved: bool
    score: int = Field(ge=0, le=100)
    reason_hash: str
    issued_at: int = Field(gt=0, le=_MAX_UINT256)
    valid_until: int = Field(gt=0, le=_MAX_UINT256)

    _hashes = field_validator(
        "order_hash", "evidence_hash", "delivery_hash", "reason_hash", mode="before"
    )(_normalize_nonzero_bytes32)

    @model_validator(mode="after")
    def validity_window_is_ordered(self) -> Self:
        if self.valid_until <= self.issued_at:
            raise ValueError("valid_until must be greater than issued_at")
        return self

    @classmethod
    def for_delivery(
        cls,
        delivery: DeliveryEvidence,
        *,
        approved: bool,
        score: int,
        reason: str,
        issued_at: int,
        valid_until: int,
    ) -> Self:
        return cls(
            order_hash=delivery.order_hash,
            evidence_hash=delivery.evidence_hash,
            delivery_hash=delivery.delivery_hash,
            approved=approved,
            score=score,
            reason_hash=hash_content(reason),
            issued_at=issued_at,
            valid_until=valid_until,
        )


class SignedValidationVerdict(_FrozenModel):
    """A verdict plus the validator identity recovered from its signature."""

    verdict: ValidationVerdict
    validator: str
    signature: str

    _validator = field_validator("validator", mode="before")(_normalize_address)
    _signature = field_validator("signature", mode="before")(_normalize_signature)


class SignedFundingAuthorization(_FrozenModel):
    """Payer signature authorizing this escrow to receive one order's funds."""

    order_hash: str
    payer: str
    signature: str

    _order_hash = field_validator("order_hash", mode="before")(
        _normalize_nonzero_bytes32
    )
    _payer = field_validator("payer", mode="before")(_normalize_address)
    _signature = field_validator("signature", mode="before")(_normalize_signature)
