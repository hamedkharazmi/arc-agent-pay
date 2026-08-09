"""ERC-8183 draft adapter for agentic-commerce job contracts.

The standard is still a draft and its prose currently differs from the
published reference implementation in a few function signatures.  This
module deliberately targets the pinned reference revision declared below;
callers may inject another contract object for deployment-specific ABIs.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Optional

from eth_utils import keccak, to_checksum_address
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..exceptions import WorkflowError
from ..onchain import load_abi, rpc_url
from .models import (
    DeliveryEvidence,
    ValidationVerdict,
    WorkOrder,
    _ADDRESS_RE,
    _MAX_UINT256,
    _ZERO_ADDRESS,
    _normalize_address,
    _normalize_bytes32,
    _uint256_word,
    bytes32,
)


ERC8183_SPEC_URL = "https://eips.ethereum.org/EIPS/eip-8183"
ERC8183_SOURCE_REVISION = "a078cab5cc8e9581c15f76c091ed96eed28f02f7"
ERC8183_PROFILE = "draft-reference-a078cab"
ZERO_ADDRESS = _ZERO_ADDRESS

_VERDICT_TYPE = (
    "ValidationVerdict(bytes32 orderHash,bytes32 evidenceHash,bytes32 deliveryHash,"
    "bool approved,uint8 score,bytes32 reasonHash,uint256 issuedAt,uint256 validUntil)"
)
_VERDICT_TYPE_HASH = keccak(text=_VERDICT_TYPE)


def _normalize_optional_address(value: Any) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise ValueError("must be a 20-byte 0x-prefixed EVM address")
    return value.lower()


def _uint256(value: Any, *, name: str, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    minimum = 1 if positive else 0
    if not minimum <= value <= _MAX_UINT256:
        qualifier = "non-zero " if positive else ""
        raise ValueError(f"{name} must fit a {qualifier}uint256")
    return value


def _bytes(value: Any, *, name: str) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError(f"{name} must be bytes")
    return value


class Erc8183Status(IntEnum):
    """Job states defined by the ERC-8183 draft."""

    OPEN = 0
    FUNDED = 1
    SUBMITTED = 2
    COMPLETED = 3
    REJECTED = 4
    EXPIRED = 5


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Erc8183Job(_FrozenModel):
    """Normalized result of the reference contract's ``getJob`` call."""

    job_id: int = Field(ge=0, le=_MAX_UINT256)
    client: str
    provider: str
    evaluator: str
    description: str
    budget: int = Field(ge=0, le=_MAX_UINT256)
    expired_at: int = Field(ge=0, le=_MAX_UINT256)
    status: Erc8183Status
    hook: str

    _client = field_validator("client", "evaluator", mode="before")(_normalize_address)
    _optional_addresses = field_validator("provider", "hook", mode="before")(
        _normalize_optional_address
    )


class Erc8183JobSpec(_FrozenModel):
    """Inputs derived from a partner-neutral :class:`WorkOrder`."""

    provider: str
    evaluator: str
    expired_at: int = Field(gt=0, le=_MAX_UINT256)
    description: str
    budget: int = Field(gt=0, le=_MAX_UINT256)
    work_order_hash: str

    _addresses = field_validator("provider", "evaluator", mode="before")(_normalize_address)
    _hash = field_validator("work_order_hash", mode="before")(_normalize_bytes32)

    @classmethod
    def from_work_order(cls, order: WorkOrder) -> "Erc8183JobSpec":
        return cls(
            provider=order.provider,
            evaluator=order.validator,
            expired_at=order.refund_after,
            description=f"urn:arc-agent-pay:work-order:{order.order_hash}",
            budget=order.amount,
            work_order_hash=order.order_hash,
        )


class Erc8183CreateResult(_FrozenModel):
    job_id: int = Field(gt=0, le=_MAX_UINT256)
    tx_hash: str


def deliverable_commitment(delivery: DeliveryEvidence) -> str:
    """Map delivery evidence to ERC-8183's ``bytes32 deliverable``."""
    return delivery.delivery_hash


def verdict_commitment(verdict: ValidationVerdict) -> str:
    """Commit every verdict field for ERC-8183's ``bytes32 reason``.

    Using only ``reason_hash`` would omit the decision, score, delivery, and
    validity window.  This is the same EIP-712 struct hash signed by validators
    before domain separation is applied.
    """
    encoded = b"".join(
        [
            _VERDICT_TYPE_HASH,
            bytes32(verdict.order_hash),
            bytes32(verdict.evidence_hash),
            bytes32(verdict.delivery_hash),
            _uint256_word(1 if verdict.approved else 0),
            _uint256_word(verdict.score),
            bytes32(verdict.reason_hash),
            _uint256_word(verdict.issued_at),
            _uint256_word(verdict.valid_until),
        ]
    )
    return "0x" + keccak(encoded).hex()


class Erc8183Client:
    """Client for the ERC-8183 draft reference-implementation ABI.

    The default ABI is packaged with the SDK.  ``contract`` and
    ``token_contract`` injection keep the adapter usable with test chains and
    deployments that preserve the reference methods while adding extensions.
    """

    profile = ERC8183_PROFILE

    def __init__(
        self,
        address: str,
        *,
        account: Any = None,
        rpc: Optional[str] = None,
        w3: Any = None,
        contract: Any = None,
        token_contract: Any = None,
    ) -> None:
        self.account = account
        self.token_contract = token_contract
        self.address = _normalize_address(address)
        if contract is not None:
            self.w3 = w3
            self.contract = contract
            return
        try:
            from web3 import Web3
        except ImportError as exc:  # pragma: no cover - import guard
            raise ImportError(
                'Erc8183Client requires web3. Install with: pip install "arc-agent-pay[onchain]"'
            ) from exc
        self.w3 = w3 or Web3(Web3.HTTPProvider(rpc or rpc_url()))
        self.contract = self.w3.eth.contract(
            address=self.w3.to_checksum_address(address),
            abi=load_abi("erc8183_reference"),
        )

    def payment_token(self) -> str:
        return _normalize_address(self.contract.functions.paymentToken().call())

    def get_job(self, job_id: int) -> Erc8183Job:
        job_id = _uint256(job_id, name="job_id", positive=True)
        raw = self.contract.functions.getJob(job_id).call()
        if isinstance(raw, dict):
            values = (
                raw.get("id"),
                raw.get("client"),
                raw.get("provider"),
                raw.get("evaluator"),
                raw.get("description"),
                raw.get("budget"),
                raw.get("expiredAt"),
                raw.get("status"),
                raw.get("hook"),
            )
        else:
            values = tuple(raw)
        if len(values) != 9:
            raise WorkflowError("ERC-8183 getJob returned an unexpected tuple shape")
        return Erc8183Job(
            job_id=values[0],
            client=values[1],
            provider=values[2],
            evaluator=values[3],
            description=values[4],
            budget=values[5],
            expired_at=values[6],
            status=Erc8183Status(values[7]),
            hook=values[8],
        )

    def create_job(
        self,
        *,
        provider: str = ZERO_ADDRESS,
        evaluator: str,
        expired_at: int,
        description: str,
        hook: str = ZERO_ADDRESS,
    ) -> Erc8183CreateResult:
        provider = _normalize_optional_address(provider)
        evaluator = _normalize_address(evaluator)
        hook = _normalize_optional_address(hook)
        expired_at = _uint256(expired_at, name="expired_at", positive=True)
        if not isinstance(description, str):
            raise TypeError("description must be a string")
        tx_hash, receipt = self._send(
            self.contract.functions.createJob(
                to_checksum_address(provider),
                to_checksum_address(evaluator),
                expired_at,
                description,
                to_checksum_address(hook),
            )
        )
        try:
            events = self.contract.events.JobCreated().process_receipt(receipt)
            job_id = int(events[0]["args"]["jobId"])
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise WorkflowError("successful createJob receipt has no JobCreated event") from exc
        return Erc8183CreateResult(job_id=job_id, tx_hash=tx_hash)

    def create_from_work_order(
        self,
        order: WorkOrder,
        *,
        hook: str = ZERO_ADDRESS,
    ) -> Erc8183CreateResult:
        if order.escrow != self.address:
            raise WorkflowError("work order belongs to a different escrow contract")
        if self.account is None:
            raise WorkflowError("creating a work-order job requires an account")
        if _normalize_address(self.account.address) != order.payer:
            raise WorkflowError("work order payer does not match the transaction signer")
        if self.w3 is None:
            raise WorkflowError("creating a work-order job requires an injected Web3 client")
        if int(self.w3.eth.chain_id) != order.chain_id:
            raise WorkflowError("work order belongs to a different chain")
        if self.payment_token() != order.asset:
            raise WorkflowError("work order asset is not the ERC-8183 payment token")
        spec = Erc8183JobSpec.from_work_order(order)
        return self.create_job(
            provider=spec.provider,
            evaluator=spec.evaluator,
            expired_at=spec.expired_at,
            description=spec.description,
            hook=hook,
        )

    def set_provider(self, job_id: int, provider: str) -> str:
        return self._send_hash(
            self.contract.functions.setProvider(
                _uint256(job_id, name="job_id", positive=True),
                to_checksum_address(_normalize_address(provider)),
            )
        )

    def set_budget(self, job_id: int, amount: int, *, opt_params: bytes = b"") -> str:
        return self._send_hash(
            self.contract.functions.setBudget(
                _uint256(job_id, name="job_id", positive=True),
                _uint256(amount, name="amount"),
                _bytes(opt_params, name="opt_params"),
            )
        )

    def approve_payment(self, amount: int) -> str:
        amount = _uint256(amount, name="amount")
        token = self._payment_token_contract()
        return self._send_hash(
            token.functions.approve(to_checksum_address(self.address), amount)
        )

    def fund(self, job_id: int, *, expected_budget: int, opt_params: bytes = b"") -> str:
        """Fund a reference-profile job after checking the observed budget.

        The check catches ordinary mistakes, but the reference ABI does not
        pass ``expected_budget`` on-chain and therefore lacks the draft prose's
        atomic front-running protection.  See the compatibility document.
        """
        job_id = _uint256(job_id, name="job_id", positive=True)
        expected_budget = _uint256(expected_budget, name="expected_budget")
        observed = self.get_job(job_id).budget
        if observed != expected_budget:
            raise WorkflowError(
                f"ERC-8183 budget changed: expected {expected_budget}, observed {observed}"
            )
        return self._send_hash(
            self.contract.functions.fund(
                job_id,
                _bytes(opt_params, name="opt_params"),
            )
        )

    def submit(self, job_id: int, deliverable: str | bytes, *, opt_params: bytes = b"") -> str:
        return self._send_hash(
            self.contract.functions.submit(
                _uint256(job_id, name="job_id", positive=True),
                bytes32(_normalize_bytes32(deliverable)),
                _bytes(opt_params, name="opt_params"),
            )
        )

    def complete(self, job_id: int, reason: str | bytes, *, opt_params: bytes = b"") -> str:
        return self._resolution("complete", job_id, reason, opt_params)

    def reject(self, job_id: int, reason: str | bytes, *, opt_params: bytes = b"") -> str:
        return self._resolution("reject", job_id, reason, opt_params)

    def claim_refund(self, job_id: int) -> str:
        return self._send_hash(
            self.contract.functions.claimRefund(
                _uint256(job_id, name="job_id", positive=True)
            )
        )

    def _resolution(
        self,
        name: str,
        job_id: int,
        reason: str | bytes,
        opt_params: bytes,
    ) -> str:
        fn = getattr(self.contract.functions, name)
        return self._send_hash(
            fn(
                _uint256(job_id, name="job_id", positive=True),
                bytes32(_normalize_bytes32(reason)),
                _bytes(opt_params, name="opt_params"),
            )
        )

    def _payment_token_contract(self) -> Any:
        if self.token_contract is not None:
            return self.token_contract
        if self.w3 is None:
            raise WorkflowError("payment approval requires an injected Web3 client")
        self.token_contract = self.w3.eth.contract(
            address=to_checksum_address(self.payment_token()),
            abi=load_abi("erc20_approve"),
        )
        return self.token_contract

    def _send_hash(self, fn: Any) -> str:
        tx_hash, _ = self._send(fn)
        return tx_hash

    def _send(self, fn: Any) -> tuple[str, Any]:
        if self.account is None or self.w3 is None:
            raise WorkflowError("ERC-8183 writes require both an account and Web3 client")
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
        tx_hex = tx_hash.hex()
        if not tx_hex.startswith("0x"):
            tx_hex = "0x" + tx_hex
        if int(receipt.get("status", 0)) != 1:
            raise WorkflowError(f"ERC-8183 transaction reverted on-chain ({tx_hex})")
        return tx_hex, receipt
