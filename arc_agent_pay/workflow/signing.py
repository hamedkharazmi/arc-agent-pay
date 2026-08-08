"""EIP-712 signing and strict verification for validation verdicts."""

from __future__ import annotations

import time
from typing import Any, Optional

from eth_account import Account
from eth_account.messages import encode_typed_data

from ..exceptions import InvalidVerdictError
from .models import (
    DeliveryEvidence,
    SignedValidationVerdict,
    ValidationVerdict,
    WorkOrder,
    _MAX_UINT256,
    _normalize_address,
    bytes32,
)


VERDICT_DOMAIN_NAME = "ArcAgentPay Validation"
VERDICT_DOMAIN_VERSION = "1"

_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP256K1_HALF_N = _SECP256K1_N // 2

VERDICT_TYPES = {
    "ValidationVerdict": [
        {"name": "orderHash", "type": "bytes32"},
        {"name": "evidenceHash", "type": "bytes32"},
        {"name": "deliveryHash", "type": "bytes32"},
        {"name": "approved", "type": "bool"},
        {"name": "score", "type": "uint8"},
        {"name": "reasonHash", "type": "bytes32"},
        {"name": "issuedAt", "type": "uint256"},
        {"name": "validUntil", "type": "uint256"},
    ]
}


def verdict_domain(*, chain_id: int, verifying_contract: str) -> dict[str, Any]:
    """Build the replay-resistant EIP-712 domain used by an escrow contract."""
    if isinstance(chain_id, bool) or not isinstance(chain_id, int):
        raise ValueError("chain_id must be an integer")
    if not 0 < chain_id <= _MAX_UINT256:
        raise ValueError("chain_id must fit a non-zero uint256")
    contract = _normalize_address(verifying_contract)
    return {
        "name": VERDICT_DOMAIN_NAME,
        "version": VERDICT_DOMAIN_VERSION,
        "chainId": chain_id,
        "verifyingContract": contract,
    }


def verdict_message(verdict: ValidationVerdict) -> dict[str, Any]:
    return {
        "orderHash": bytes32(verdict.order_hash),
        "evidenceHash": bytes32(verdict.evidence_hash),
        "deliveryHash": bytes32(verdict.delivery_hash),
        "approved": verdict.approved,
        "score": verdict.score,
        "reasonHash": bytes32(verdict.reason_hash),
        "issuedAt": verdict.issued_at,
        "validUntil": verdict.valid_until,
    }


def signature_parts(signature: str) -> tuple[int, bytes, bytes]:
    """Split a canonical 65-byte ECDSA signature into contract-ready ``v,r,s``."""
    try:
        raw = bytes.fromhex(signature[2:] if signature.startswith("0x") else signature)
    except (TypeError, ValueError) as exc:
        raise InvalidVerdictError("verdict signature is malformed") from exc
    if len(raw) != 65:
        raise InvalidVerdictError("verdict signature must be exactly 65 bytes")
    r = int.from_bytes(raw[:32], byteorder="big")
    s = int.from_bytes(raw[32:64], byteorder="big")
    v = raw[64]
    if not 0 < r < _SECP256K1_N or not 0 < s <= _SECP256K1_HALF_N or v not in (27, 28):
        raise InvalidVerdictError("verdict signature is not canonical")
    return v, raw[:32], raw[32:64]


def sign_verdict(
    verdict: ValidationVerdict,
    *,
    private_key: Any,
    order: WorkOrder,
) -> SignedValidationVerdict:
    """Sign a verdict for exactly one order's chain and escrow contract."""
    if verdict.order_hash != order.order_hash:
        raise InvalidVerdictError("verdict order_hash does not match the work order")
    validator = Account.from_key(private_key).address.lower()
    if validator != order.validator:
        raise InvalidVerdictError("signing key does not belong to the order's validator")
    signed = Account.sign_typed_data(
        private_key,
        domain_data=verdict_domain(
            chain_id=order.chain_id,
            verifying_contract=order.escrow,
        ),
        message_types=VERDICT_TYPES,
        message_data=verdict_message(verdict),
    )
    return SignedValidationVerdict(
        verdict=verdict,
        validator=validator,
        signature=signed.signature.to_0x_hex(),
    )


def recover_verdict_signer(
    signed: SignedValidationVerdict,
    *,
    chain_id: int,
    verifying_contract: str,
) -> str:
    """Recover the EOA that signed a verdict under the supplied domain."""
    v, r, s = signature_parts(signed.signature)
    signature = r + s + bytes([v])
    signable = encode_typed_data(
        domain_data=verdict_domain(
            chain_id=chain_id,
            verifying_contract=verifying_contract,
        ),
        message_types=VERDICT_TYPES,
        message_data=verdict_message(signed.verdict),
    )
    try:
        return Account.recover_message(
            signable,
            signature=signature,
        ).lower()
    except Exception as exc:  # noqa: BLE001 - normalize crypto library failures
        raise InvalidVerdictError("invalid verdict signature") from exc


def verify_signed_verdict(
    signed: SignedValidationVerdict,
    *,
    order: WorkOrder,
    delivery: DeliveryEvidence,
    now: Optional[int] = None,
    max_clock_skew: int = 30,
    require_approval: bool = False,
) -> str:
    """Validate all bindings, timing, domain separation, and signer identity.

    Returns the recovered validator address. Any mismatch raises
    :class:`InvalidVerdictError`; callers never need to interpret a partial
    boolean result.
    """
    verdict = signed.verdict
    current = int(time.time()) if now is None else int(now)
    if max_clock_skew < 0:
        raise ValueError("max_clock_skew must be non-negative")
    if delivery.order_hash != order.order_hash:
        raise InvalidVerdictError("delivery order_hash does not match the work order")
    if delivery.delivered_at > order.delivery_deadline:
        raise InvalidVerdictError("delivery was submitted after the order deadline")
    if verdict.order_hash != order.order_hash:
        raise InvalidVerdictError("verdict order_hash does not match the work order")
    if verdict.evidence_hash != delivery.evidence_hash:
        raise InvalidVerdictError("verdict evidence_hash does not match the delivery")
    if verdict.delivery_hash != delivery.delivery_hash:
        raise InvalidVerdictError("verdict delivery_hash does not match the delivery")
    if verdict.issued_at < delivery.delivered_at:
        raise InvalidVerdictError("verdict was issued before the delivery")
    if verdict.issued_at > current + max_clock_skew:
        raise InvalidVerdictError("verdict issued_at is in the future")
    if verdict.valid_until <= current:
        raise InvalidVerdictError("verdict has expired")
    if verdict.valid_until > order.refund_after:
        raise InvalidVerdictError("verdict validity extends past the refund deadline")
    if require_approval and not verdict.approved:
        raise InvalidVerdictError("verdict rejected the delivery")

    recovered = recover_verdict_signer(
        signed,
        chain_id=order.chain_id,
        verifying_contract=order.escrow,
    )
    if signed.validator != recovered:
        raise InvalidVerdictError("declared validator does not match the signature")
    if recovered != order.validator:
        raise InvalidVerdictError("verdict was not signed by the order's validator")
    return recovered
