"""Arc Testnet x402 payment verification from canonical USDC Transfer logs."""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Mapping, Optional

from eth_utils import keccak
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..exceptions import (
    AmbiguousPaymentTransfer,
    ArcPaymentVerificationError,
    InsufficientConfirmations,
    InvalidTransactionReference,
    MissingTransactionReference,
    PaymentAmountMismatch,
    PaymentTransferNotFound,
    ProviderBindingMismatch,
    TransactionNotFound,
    TransactionReverted,
    WrongArcChain,
    WrongPaymentAsset,
    WrongPaymentNetwork,
)
from ..models import AssuranceTerms
from ..onchain import rpc_url
from ..workflow.models import (
    _MAX_UINT256,
    _normalize_address,
    _normalize_nonzero_bytes32,
)
from .models import AssuranceEvidence

ARC_TESTNET_CHAIN_ID = 5_042_002
ARC_TESTNET_NETWORK = "eip155:5042002"
ARC_TESTNET_USDC = "0x3600000000000000000000000000000000000000"
DEFAULT_MIN_CONFIRMATIONS = 2

PAYMENT_ID_DOMAIN = b"agentpay-assurance:payment:v1"
DISPUTE_ID_DOMAIN = b"agentpay-assurance:dispute:v1"
TRANSFER_TOPIC = keccak(text="Transfer(address,address,uint256)")
_TRANSACTION_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ZERO_ADDRESS = "0x" + "0" * 40


def payment_id_hash(payment_id: str) -> str:
    """Derive the canonical domain-separated payment identifier commitment."""
    if not isinstance(payment_id, str) or not payment_id:
        raise ValueError("payment_id must be a non-empty string")
    return "0x" + keccak(PAYMENT_ID_DOMAIN + payment_id.encode("utf-8")).hex()


def dispute_id(payment_hash: str | bytes, evidence_hash: str | bytes) -> str:
    """Derive the canonical dispute ID from payment and evidence commitments."""
    normalized_payment = _normalize_nonzero_bytes32(payment_hash)
    normalized_evidence = _normalize_nonzero_bytes32(evidence_hash)
    payload = (
        DISPUTE_ID_DOMAIN
        + bytes.fromhex(normalized_payment[2:])
        + bytes.fromhex(normalized_evidence[2:])
    )
    return "0x" + keccak(payload).hex()


class VerifiedArcPayment(BaseModel):
    """Immutable economic proof derived from one successful Arc USDC receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    payment_id: str = Field(min_length=1, max_length=128)
    payment_id_hash: str
    transaction_hash: str
    block_number: int = Field(ge=0)
    block_timestamp: int = Field(ge=0)
    confirmations: int = Field(ge=1)
    buyer: str
    provider: str
    asset: str
    amount_atomic: int = Field(gt=0, le=_MAX_UINT256)
    chain_id: Literal[5_042_002] = ARC_TESTNET_CHAIN_ID
    evidence_hash: str

    _hashes = field_validator(
        "payment_id_hash", "transaction_hash", "evidence_hash", mode="before"
    )(_normalize_nonzero_bytes32)
    _addresses = field_validator("buyer", "provider", "asset", mode="before")(
        _normalize_address
    )


def _value(record: Any, name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _transaction_hash(value: Any) -> str:
    if isinstance(value, bytes):
        raw = bytes(value)
        if len(raw) != 32:
            raise InvalidTransactionReference("transaction reference must be 32 bytes")
        if raw == b"\x00" * 32:
            raise InvalidTransactionReference("transaction reference must be non-zero")
        return "0x" + raw.hex()
    if not isinstance(value, str) or not value:
        raise MissingTransactionReference("Assurance evidence has no transaction reference")
    raw_value = value[2:] if value.startswith(("0x", "0X")) else value
    if not _TRANSACTION_HASH_RE.fullmatch(raw_value):
        raise InvalidTransactionReference(
            "transaction reference must be a 32-byte hexadecimal hash"
        )
    if int(raw_value, 16) == 0:
        raise InvalidTransactionReference("transaction reference must be non-zero")
    return "0x" + raw_value.lower()


def _quote(evidence: AssuranceEvidence) -> tuple[str, str, str, int]:
    try:
        selected = json.loads(evidence.selected_x402_requirement)
    except (json.JSONDecodeError, TypeError) as exc:  # evidence normally prevents this
        raise WrongPaymentNetwork("selected x402 requirement is not readable") from exc
    if not isinstance(selected, dict):
        raise WrongPaymentNetwork("selected x402 requirement must be an object")

    network = selected.get("network")
    if network != ARC_TESTNET_NETWORK:
        raise WrongPaymentNetwork(
            f"selected x402 network must be {ARC_TESTNET_NETWORK}"
        )

    raw_asset = selected.get("asset")
    try:
        asset = _normalize_address(raw_asset)
    except (TypeError, ValueError) as exc:
        raise WrongPaymentAsset("selected x402 asset is not a valid Arc USDC address") from exc
    if asset != ARC_TESTNET_USDC:
        raise WrongPaymentAsset("selected x402 asset is not Arc Testnet USDC")

    try:
        service = json.loads(evidence.service_snapshot)
        terms = AssuranceTerms.model_validate(service["assurance"])
    except Exception as exc:  # evidence normally prevents malformed snapshots
        raise ProviderBindingMismatch(
            "Assurance service snapshot has no valid provider binding"
        ) from exc
    if service.get("chain") != "ARC-TESTNET":
        raise WrongPaymentNetwork("Assurance service snapshot does not target Arc Testnet")

    raw_provider = (
        selected.get("payTo")
        or selected.get("pay_to")
        or selected.get("payToAddress")
        or selected.get("pay_to_address")
    )
    try:
        quoted_provider = _normalize_address(raw_provider)
    except (TypeError, ValueError) as exc:
        raise ProviderBindingMismatch("selected x402 payTo is not a valid provider") from exc

    provider = terms.provider_address
    if quoted_provider != provider:
        raise ProviderBindingMismatch(
            "selected x402 payTo does not match AssuranceTerms.provider_address"
        )

    raw_amount = selected.get("amount")
    if raw_amount is None:
        raw_amount = selected.get("maxAmountRequired", selected.get("max_amount_required"))
    if isinstance(raw_amount, bool):
        raise PaymentAmountMismatch("selected x402 amount must be a positive atomic integer")
    if isinstance(raw_amount, int):
        amount = raw_amount
    elif isinstance(raw_amount, str) and raw_amount.isdigit():
        amount = int(raw_amount)
    else:
        raise PaymentAmountMismatch("selected x402 amount must be a positive atomic integer")
    if not 0 < amount <= _MAX_UINT256:
        raise PaymentAmountMismatch("selected x402 amount must fit a non-zero uint256")
    return network, asset, provider, amount


def _bytes(value: Any) -> Optional[bytes]:
    if isinstance(value, bytes):
        return bytes(value)
    if isinstance(value, str):
        raw = value[2:] if value.startswith(("0x", "0X")) else value
        try:
            return bytes.fromhex(raw)
        except ValueError:
            return None
    return None


def _log_address(value: Any) -> Optional[str]:
    try:
        return _normalize_address(str(value))
    except (TypeError, ValueError):
        return None


def _decode_usdc_transfers(logs: Any) -> list[tuple[str, str, int]]:
    transfers: list[tuple[str, str, int]] = []
    for log in logs or []:
        if _log_address(_value(log, "address")) != ARC_TESTNET_USDC:
            continue
        topics = _value(log, "topics", [])
        if not isinstance(topics, (list, tuple)) or len(topics) != 3:
            continue
        topic0 = _bytes(topics[0])
        sender_topic = _bytes(topics[1])
        recipient_topic = _bytes(topics[2])
        data = _bytes(_value(log, "data"))
        if (
            topic0 != TRANSFER_TOPIC
            or sender_topic is None
            or recipient_topic is None
            or data is None
            or len(sender_topic) != 32
            or len(recipient_topic) != 32
            or len(data) != 32
            or sender_topic[:12] != b"\x00" * 12
            or recipient_topic[:12] != b"\x00" * 12
        ):
            continue
        sender = "0x" + sender_topic[12:].hex()
        recipient = "0x" + recipient_topic[12:].hex()
        transfers.append((sender, recipient, int.from_bytes(data, byteorder="big")))
    return transfers


class ArcPaymentVerifier:
    """Verify x402 settlement economically from Arc Testnet USDC Transfer logs.

    HTTP status and local ``PaymentStatus`` are deliberately ignored. The result
    is based only on the immutable evidence quote plus a successful, sufficiently
    confirmed Arc receipt containing exactly one matching USDC transfer.
    """

    def __init__(
        self,
        *,
        w3: Any = None,
        rpc: Optional[str] = None,
        min_confirmations: int = DEFAULT_MIN_CONFIRMATIONS,
    ) -> None:
        if (
            isinstance(min_confirmations, bool)
            or not isinstance(min_confirmations, int)
            or min_confirmations < 1
        ):
            raise ValueError("min_confirmations must be a positive integer")
        if w3 is None:
            try:
                from web3 import Web3
            except ImportError as exc:  # pragma: no cover - import guard
                raise ImportError(
                    'ArcPaymentVerifier requires web3. Install with: '
                    'pip install "arc-agent-pay[onchain]"'
                ) from exc
            w3 = Web3(Web3.HTTPProvider(rpc or rpc_url()))
        self.w3 = w3
        self.min_confirmations = min_confirmations

    def verify(self, evidence: AssuranceEvidence) -> VerifiedArcPayment:
        try:
            actual_chain_id = int(self.w3.eth.chain_id)
        except Exception as exc:
            raise ArcPaymentVerificationError("could not read the Arc chain ID") from exc
        if actual_chain_id != ARC_TESTNET_CHAIN_ID:
            raise WrongArcChain(
                f"connected chain ID {actual_chain_id} is not Arc Testnet {ARC_TESTNET_CHAIN_ID}"
            )

        _network, asset, provider, amount = _quote(evidence)
        tx_hash = _transaction_hash(evidence.arc_transaction_reference)

        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
        except Exception as exc:
            if exc.__class__.__name__ == "TransactionNotFound":
                raise TransactionNotFound("Arc transaction receipt was not found") from None
            raise ArcPaymentVerificationError("could not fetch the Arc transaction receipt") from exc
        if receipt is None:
            raise TransactionNotFound("Arc transaction receipt was not found")
        if int(_value(receipt, "status", 0)) != 1:
            raise TransactionReverted("Arc transaction execution was not successful")

        try:
            block_number = int(_value(receipt, "blockNumber"))
            latest_block = int(self.w3.eth.block_number)
        except Exception as exc:
            raise ArcPaymentVerificationError("could not determine Arc confirmations") from exc
        confirmations = latest_block - block_number + 1
        if confirmations < self.min_confirmations:
            raise InsufficientConfirmations(confirmations, self.min_confirmations)

        try:
            block = self.w3.eth.get_block(block_number)
            block_timestamp = int(_value(block, "timestamp"))
        except Exception as exc:
            raise ArcPaymentVerificationError("could not read the Arc payment block") from exc

        transfers = _decode_usdc_transfers(_value(receipt, "logs", []))
        recipient_transfers = [transfer for transfer in transfers if transfer[1] == provider]
        matches = [transfer for transfer in recipient_transfers if transfer[2] == amount]
        if not matches:
            if recipient_transfers:
                raise PaymentAmountMismatch(
                    "Arc USDC transfer to the provider does not match the selected x402 amount"
                )
            raise PaymentTransferNotFound(
                "receipt has no Arc USDC transfer matching the assured payment"
            )
        if len(matches) != 1:
            raise AmbiguousPaymentTransfer(
                "receipt has multiple Arc USDC transfers matching the assured payment"
            )
        buyer, transfer_provider, transfer_amount = matches[0]
        if buyer == _ZERO_ADDRESS:
            raise PaymentTransferNotFound(
                "matching Arc USDC event has a zero-address sender and is not a payment"
            )

        return VerifiedArcPayment(
            payment_id=evidence.payment_id,
            payment_id_hash=payment_id_hash(evidence.payment_id),
            transaction_hash=tx_hash,
            block_number=block_number,
            block_timestamp=block_timestamp,
            confirmations=confirmations,
            buyer=buyer,
            provider=transfer_provider,
            asset=asset,
            amount_atomic=transfer_amount,
            chain_id=ARC_TESTNET_CHAIN_ID,
            evidence_hash=evidence.evidence_hash,
        )
