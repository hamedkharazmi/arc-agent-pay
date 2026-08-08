"""EIP-3009 funding authorizations for validation-gated escrow orders."""

from __future__ import annotations

from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data

from ..exceptions import InvalidFundingAuthorizationError, InvalidVerdictError
from .models import (
    SignedFundingAuthorization,
    WorkOrder,
    _MAX_UINT256,
    _normalize_address,
)
from .signing import signature_parts


FUNDING_TOKEN_NAME = "USDC"
FUNDING_TOKEN_VERSION = "2"

FUNDING_TYPES = {
    "ReceiveWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]
}


def _signature_parts(signature: str) -> tuple[int, bytes, bytes]:
    try:
        return signature_parts(signature)
    except InvalidVerdictError as exc:
        raise InvalidFundingAuthorizationError(
            str(exc).replace("verdict signature", "funding signature")
        ) from exc


def funding_domain(
    *,
    chain_id: int,
    token: str,
    token_name: str = FUNDING_TOKEN_NAME,
    token_version: str = FUNDING_TOKEN_VERSION,
) -> dict[str, Any]:
    """Build the Arc USDC EIP-712 domain for an escrow funding signature."""
    if isinstance(chain_id, bool) or not isinstance(chain_id, int):
        raise ValueError("chain_id must be an integer")
    if not 0 < chain_id <= _MAX_UINT256:
        raise ValueError("chain_id must fit a non-zero uint256")
    if not token_name or not token_version:
        raise ValueError("token_name and token_version must be non-empty")
    return {
        "name": token_name,
        "version": token_version,
        "chainId": chain_id,
        "verifyingContract": _normalize_address(token),
    }


def funding_message(order: WorkOrder) -> dict[str, Any]:
    """Return the receiver-safe EIP-3009 message derived from an order."""
    return {
        "from": order.payer,
        "to": order.escrow,
        "value": order.amount,
        "validAfter": 0,
        "validBefore": order.delivery_deadline,
        # EIP-3009 treats nonce as an opaque bytes32. Using the complete order
        # hash binds every escrow term to the payer's token authorization.
        "nonce": order.order_hash_bytes,
    }


def sign_funding_authorization(
    order: WorkOrder,
    *,
    private_key: Any,
    token_name: str = FUNDING_TOKEN_NAME,
    token_version: str = FUNDING_TOKEN_VERSION,
) -> SignedFundingAuthorization:
    """Authorize only the escrow contract to pull this order's exact amount."""
    payer = Account.from_key(private_key).address.lower()
    if payer != order.payer:
        raise InvalidFundingAuthorizationError(
            "funding key does not belong to the order's payer"
        )
    signed = Account.sign_typed_data(
        private_key,
        domain_data=funding_domain(
            chain_id=order.chain_id,
            token=order.asset,
            token_name=token_name,
            token_version=token_version,
        ),
        message_types=FUNDING_TYPES,
        message_data=funding_message(order),
    )
    return SignedFundingAuthorization(
        order_hash=order.order_hash,
        payer=payer,
        signature=signed.signature.to_0x_hex(),
    )


def recover_funding_signer(
    authorization: SignedFundingAuthorization,
    *,
    order: WorkOrder,
    token_name: str = FUNDING_TOKEN_NAME,
    token_version: str = FUNDING_TOKEN_VERSION,
) -> str:
    """Recover the payer under the order's token, chain, and message fields."""
    v, r, s = _signature_parts(authorization.signature)
    signable = encode_typed_data(
        domain_data=funding_domain(
            chain_id=order.chain_id,
            token=order.asset,
            token_name=token_name,
            token_version=token_version,
        ),
        message_types=FUNDING_TYPES,
        message_data=funding_message(order),
    )
    try:
        return Account.recover_message(signable, signature=r + s + bytes([v])).lower()
    except Exception as exc:  # noqa: BLE001 - normalize crypto library failures
        raise InvalidFundingAuthorizationError("invalid funding signature") from exc


def verify_funding_authorization(
    authorization: SignedFundingAuthorization,
    *,
    order: WorkOrder,
    token_name: str = FUNDING_TOKEN_NAME,
    token_version: str = FUNDING_TOKEN_VERSION,
) -> str:
    """Verify all declared funding bindings and return the recovered payer."""
    if authorization.order_hash != order.order_hash:
        raise InvalidFundingAuthorizationError(
            "funding order_hash does not match the work order"
        )
    recovered = recover_funding_signer(
        authorization,
        order=order,
        token_name=token_name,
        token_version=token_version,
    )
    if authorization.payer != recovered:
        raise InvalidFundingAuthorizationError(
            "declared payer does not match the funding signature"
        )
    if recovered != order.payer:
        raise InvalidFundingAuthorizationError(
            "funding authorization was not signed by the order's payer"
        )
    return recovered
