"""Trusted MVP binding and relay from finalized GenLayer verdicts to Arc.

This module deliberately does not make GenLayer trustless on Arc.  It gives the
configured Assurance operator one auditable, fail-closed path to relay a fully
bound SLA violation into the fixed Arc Testnet WarrantyBond contract.
"""

from __future__ import annotations

from enum import Enum
import json
import time
from typing import Any, Callable, Literal, Optional, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..exceptions import (
    AssuranceSettlementError,
    SettlementAlreadyProcessed,
    SettlementBindingError,
    SettlementIneligible,
    SettlementPending,
    SettlementPreflightError,
    SettlementSubmissionOutcomeUnknown,
    SettlementTransactionFailed,
)
from ..models import AssuranceTerms
from ..workflow.models import _normalize_address, _normalize_nonzero_bytes32
from .arc_payment import (
    ARC_TESTNET_CHAIN_ID,
    ARC_TESTNET_NETWORK,
    ARC_TESTNET_USDC,
    ArcPaymentVerifier,
    VerifiedArcPayment,
    dispute_id as derive_dispute_id,
    payment_id_hash as derive_payment_id_hash,
)
from .bond import BondTransactionResult, WarrantyBondClient
from .genlayer import (
    AdjudicationPayload,
    GenLayerStoredVerdict,
    GenLayerSubmission,
    GenLayerTransactionStatus,
)
from .genlayer_transport import (
    GenLayerRetryPolicy,
    is_transient_transport_error,
)
from .models import AssuranceEvidence, Dispute, DisputeStatus, canonical_hash
from .store import AssuranceStore


class _FrozenStrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class FinalizedGenLayerAdjudication(_FrozenStrictModel):
    """A successful finalized transaction plus its LATEST_FINAL verdict."""

    contract_address: str
    submission: GenLayerSubmission
    transaction_status: GenLayerTransactionStatus
    verdict: GenLayerStoredVerdict
    verdict_state: Literal["latest-final"] = "latest-final"

    _contract = field_validator("contract_address", mode="before")(_normalize_address)

    @model_validator(mode="after")
    def validate_finalized_adjudication(self) -> Self:
        status = self.transaction_status
        if status.transaction_id != self.submission.transaction_id:
            raise ValueError("GenLayer status transaction does not match submission")
        if not status.finalized:
            raise ValueError("GenLayer adjudication is not protocol-finalized")
        if status.outcome != "accepted":
            raise ValueError("GenLayer adjudication was not accepted")
        if status.execution_result != "FINISHED_WITH_RETURN":
            raise ValueError("GenLayer adjudication execution did not succeed")
        if self.verdict.dispute_id != self.submission.dispute_id:
            raise ValueError("GenLayer verdict dispute does not match submission")
        if self.verdict.adjudication_input_hash != self.submission.adjudication_input_hash:
            raise ValueError("GenLayer verdict input hash does not match submission")
        return self


def _service_terms(evidence: AssuranceEvidence) -> AssuranceTerms:
    try:
        service = json.loads(evidence.service_snapshot)
        return AssuranceTerms.model_validate(service["assurance"])
    except Exception as exc:
        raise SettlementBindingError("evidence has no valid snapshotted Assurance terms") from exc


def _selected_network(evidence: AssuranceEvidence) -> str:
    try:
        requirement = json.loads(evidence.selected_x402_requirement)
        network = requirement["network"]
    except Exception as exc:
        raise SettlementBindingError("evidence has no selected x402 network") from exc
    if not isinstance(network, str):
        raise SettlementBindingError("selected x402 network must be text")
    return network


def _selected_payment_terms(evidence: AssuranceEvidence) -> tuple[str, str, int]:
    try:
        requirement = json.loads(evidence.selected_x402_requirement)
        asset = _normalize_address(requirement["asset"])
        provider = _normalize_address(requirement.get("payTo") or requirement.get("pay_to"))
        raw_amount = requirement.get("amount", requirement.get("maxAmountRequired"))
        if isinstance(raw_amount, bool):
            raise ValueError("boolean amount")
        amount = int(raw_amount)
    except Exception as exc:
        raise SettlementBindingError("evidence has no valid selected x402 payment terms") from exc
    if amount <= 0:
        raise SettlementBindingError("selected x402 payment amount is not positive")
    return asset, provider, amount


def _is_synthetic_evidence(evidence: AssuranceEvidence) -> bool:
    payment_id = evidence.payment_id.lower()
    if payment_id.startswith(("synthetic-", "smoke-", "test-")):
        return True
    try:
        service = json.loads(evidence.service_snapshot)
        hostname = (urlsplit(str(service.get("url", ""))).hostname or "").lower()
        required = json.loads(evidence.parsed_payment_required)
    except Exception:
        return True
    if hostname.endswith(".invalid"):
        return True
    return isinstance(required, dict) and required.get("synthetic") is True


class AssuranceSettlementBinding(_FrozenStrictModel):
    """Canonical cross-chain commitment assembled only from verified sources."""

    schema_version: Literal["1"] = "1"
    evidence: AssuranceEvidence
    dispute: Dispute
    verified_payment: VerifiedArcPayment
    adjudication_payload: AdjudicationPayload
    adjudication: FinalizedGenLayerAdjudication
    payment_id: str
    payment_id_hash: str
    evidence_hash: str
    dispute_id: str
    terms_hash: str
    adjudication_input_hash: str
    genlayer_contract_address: str
    genlayer_transaction_id: str
    provider: str
    buyer: str
    payment_amount: int = Field(gt=0)
    arc_asset: str
    arc_chain_id: Literal[5_042_002]
    x402_network: Literal["eip155:5042002"]
    original_arc_payment_transaction: str
    warranty_bond_address: str
    sla_violated: bool
    binding_hash: str

    _hashes = field_validator(
        "payment_id_hash",
        "evidence_hash",
        "dispute_id",
        "terms_hash",
        "adjudication_input_hash",
        "genlayer_transaction_id",
        "original_arc_payment_transaction",
        "binding_hash",
        mode="before",
    )(_normalize_nonzero_bytes32)
    _addresses = field_validator(
        "genlayer_contract_address",
        "provider",
        "buyer",
        "arc_asset",
        "warranty_bond_address",
        mode="before",
    )(_normalize_address)

    def _hash_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"binding_hash"})

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        evidence = self.evidence
        dispute = self.dispute
        payment = self.verified_payment
        payload = self.adjudication_payload
        final = self.adjudication
        terms = _service_terms(evidence)
        quoted_asset, quoted_provider, quoted_amount = _selected_payment_terms(evidence)

        status = final.transaction_status
        if status.transaction_id != final.submission.transaction_id:
            raise ValueError("GenLayer status transaction does not match submission")
        if not status.finalized:
            raise ValueError("GenLayer adjudication is not protocol-finalized")
        if status.outcome != "accepted":
            raise ValueError("GenLayer adjudication was not accepted")
        if status.execution_result != "FINISHED_WITH_RETURN":
            raise ValueError("GenLayer adjudication execution did not succeed")
        if final.verdict_state != "latest-final":
            raise ValueError("GenLayer verdict was not read from finalized state")

        expected_payment_hash = derive_payment_id_hash(evidence.payment_id)
        expected_dispute = derive_dispute_id(expected_payment_hash, evidence.evidence_hash)
        expected = {
            "payment_id": evidence.payment_id,
            "payment_id_hash": expected_payment_hash,
            "evidence_hash": evidence.evidence_hash,
            "dispute_id": expected_dispute,
            "terms_hash": evidence.terms_hash,
            "adjudication_input_hash": payload.input_hash,
            "genlayer_contract_address": final.contract_address,
            "genlayer_transaction_id": final.submission.transaction_id,
            "provider": payment.provider,
            "buyer": payment.buyer,
            "payment_amount": payment.amount_atomic,
            "arc_asset": payment.asset,
            "arc_chain_id": payment.chain_id,
            "x402_network": _selected_network(evidence),
            "original_arc_payment_transaction": payment.transaction_hash,
            "warranty_bond_address": terms.warranty_bond_address,
            "sla_violated": final.verdict.sla_violated,
        }
        for field, value in expected.items():
            if getattr(self, field) != value:
                raise ValueError(f"settlement {field} does not match its verified source")

        if payment.payment_id != evidence.payment_id:
            raise ValueError("verified Arc payment has the wrong payment_id")
        if payment.payment_id_hash != expected_payment_hash:
            raise ValueError("verified Arc payment has the wrong payment_id_hash")
        if payment.evidence_hash != evidence.evidence_hash:
            raise ValueError("verified Arc payment has the wrong evidence_hash")
        if dispute.payment_id != evidence.payment_id:
            raise ValueError("dispute has the wrong payment_id")
        if dispute.evidence_id != evidence.evidence_id:
            raise ValueError("dispute has the wrong evidence_id")
        if dispute.evidence_hash != evidence.evidence_hash:
            raise ValueError("dispute has the wrong evidence_hash")
        if dispute.dispute_id != expected_dispute:
            raise ValueError("dispute_id does not match payment and evidence")
        if dispute.status not in {DisputeStatus.UPHELD, DisputeStatus.REJECTED}:
            raise ValueError("local dispute does not contain a finalized verdict")
        if dispute.verdict is None:
            raise ValueError("local dispute verdict is missing")
        if dispute.verdict.sla_violated != final.verdict.sla_violated:
            raise ValueError("local and GenLayer verdict decisions differ")
        if dispute.verdict.adjudicator_reference != final.submission.transaction_id:
            raise ValueError("local verdict is not bound to the GenLayer transaction")

        if payload.payment_id_hash != expected_payment_hash:
            raise ValueError("adjudication payload has the wrong payment_id_hash")
        if payload.evidence_hash != evidence.evidence_hash:
            raise ValueError("adjudication payload has the wrong evidence_hash")
        if payload.dispute_id != expected_dispute:
            raise ValueError("adjudication payload has the wrong dispute_id")
        if payload.terms_hash != evidence.terms_hash:
            raise ValueError("adjudication payload has the wrong terms_hash")

        verdict = final.verdict
        for field, value in (
            ("payment_id_hash", expected_payment_hash),
            ("evidence_hash", evidence.evidence_hash),
            ("dispute_id", expected_dispute),
            ("terms_hash", evidence.terms_hash),
            ("adjudication_input_hash", payload.input_hash),
        ):
            if getattr(verdict, field) != value:
                raise ValueError(f"GenLayer verdict has the wrong {field}")
        if final.submission.dispute_id != expected_dispute:
            raise ValueError("GenLayer submission has the wrong dispute_id")
        if final.submission.adjudication_input_hash != payload.input_hash:
            raise ValueError("GenLayer submission has the wrong input hash")

        if self.arc_chain_id != ARC_TESTNET_CHAIN_ID:
            raise ValueError("settlement is not bound to Arc Testnet")
        if self.arc_asset != ARC_TESTNET_USDC:
            raise ValueError("settlement is not bound to Arc Testnet USDC")
        if self.x402_network != ARC_TESTNET_NETWORK:
            raise ValueError("settlement x402 network is not Arc Testnet")
        if self.provider != terms.provider_address:
            raise ValueError("verified provider does not match Assurance terms")
        if payment.asset != quoted_asset:
            raise ValueError("verified Arc asset does not match selected x402 quote")
        if payment.provider != quoted_provider:
            raise ValueError("verified Arc provider does not match selected x402 quote")
        if payment.amount_atomic != quoted_amount:
            raise ValueError("verified Arc amount does not match selected x402 quote")
        if canonical_hash(self._hash_payload()) != self.binding_hash:
            raise ValueError("settlement binding_hash does not match its contents")
        return self

    @classmethod
    def from_sources(
        cls,
        *,
        evidence: AssuranceEvidence,
        dispute: Dispute,
        verified_payment: VerifiedArcPayment,
        adjudication_payload: AdjudicationPayload,
        adjudication: FinalizedGenLayerAdjudication,
        configured_genlayer_contract: str,
    ) -> Self:
        if not isinstance(verified_payment, VerifiedArcPayment):
            raise SettlementBindingError("settlement requires an ArcPaymentVerifier result")
        try:
            evidence = AssuranceEvidence.model_validate_json(evidence.model_dump_json())
            dispute = Dispute.model_validate_json(dispute.model_dump_json())
            verified_payment = VerifiedArcPayment.model_validate_json(
                verified_payment.model_dump_json()
            )
            adjudication_payload = AdjudicationPayload.model_validate_json(
                adjudication_payload.model_dump_json()
            )
            adjudication = FinalizedGenLayerAdjudication.model_validate_json(
                adjudication.model_dump_json()
            )
        except Exception as exc:
            raise SettlementBindingError(
                f"a settlement source object failed immutable-model revalidation: {exc}"
            ) from exc
        configured = _normalize_address(configured_genlayer_contract)
        if adjudication.contract_address != configured:
            raise SettlementBindingError(
                "GenLayer verdict does not belong to the configured contract"
            )
        terms = _service_terms(evidence)
        values = {
            "schema_version": "1",
            "evidence": evidence,
            "dispute": dispute,
            "verified_payment": verified_payment,
            "adjudication_payload": adjudication_payload,
            "adjudication": adjudication,
            "payment_id": evidence.payment_id,
            "payment_id_hash": verified_payment.payment_id_hash,
            "evidence_hash": evidence.evidence_hash,
            "dispute_id": dispute.dispute_id,
            "terms_hash": evidence.terms_hash,
            "adjudication_input_hash": adjudication_payload.input_hash,
            "genlayer_contract_address": adjudication.contract_address,
            "genlayer_transaction_id": adjudication.submission.transaction_id,
            "provider": verified_payment.provider,
            "buyer": verified_payment.buyer,
            "payment_amount": verified_payment.amount_atomic,
            "arc_asset": verified_payment.asset,
            "arc_chain_id": verified_payment.chain_id,
            "x402_network": _selected_network(evidence),
            "original_arc_payment_transaction": verified_payment.transaction_hash,
            "warranty_bond_address": terms.warranty_bond_address,
            "sla_violated": adjudication.verdict.sla_violated,
        }
        digest = canonical_hash(values)
        try:
            return cls(binding_hash=digest, **values)
        except ValueError as exc:
            raise SettlementBindingError(str(exc)) from exc

    def ensure_eligible(self) -> None:
        """Reject non-violations and all recognized synthetic/test evidence."""
        try:
            validated = type(self).model_validate_json(self.model_dump_json())
            if validated != self:
                raise ValueError("settlement changed during canonical revalidation")
        except Exception as exc:
            raise SettlementBindingError(str(exc)) from exc
        if not self.sla_violated:
            raise SettlementIneligible(
                "a fulfilled SLA verdict cannot authorize a WarrantyBond refund"
            )
        if _is_synthetic_evidence(self.evidence):
            raise SettlementIneligible(
                "synthetic, smoke, or test evidence cannot authorize settlement"
            )


class SettlementStatus(str, Enum):
    ELIGIBLE = "eligible"
    SUBMISSION_ATTEMPTED = "submission_attempted"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED_BEFORE_SUBMISSION = "failed_before_submission"
    AMBIGUOUS_SUBMISSION = "ambiguous_submission"
    ALREADY_SETTLED = "already_settled"
    FAILED = "failed"


class AssuranceSettlementResult(_FrozenStrictModel):
    """Confirmed Arc refund and every source binding needed for audit."""

    status: Literal["confirmed"] = "confirmed"
    arc_refund_transaction_hash: str
    arc_block_number: int = Field(ge=0)
    gas_used: Optional[int] = Field(default=None, ge=0)
    dispute_id: str
    payment_id_hash: str
    evidence_hash: str
    provider: str
    buyer: str
    amount_atomic: int = Field(gt=0)
    arc_asset: str
    original_arc_payment_transaction: str
    genlayer_transaction_id: str
    genlayer_contract_address: str
    sla_violated: Literal[True]
    settled_at: float = Field(gt=0)

    _hashes = field_validator(
        "arc_refund_transaction_hash",
        "dispute_id",
        "payment_id_hash",
        "evidence_hash",
        "original_arc_payment_transaction",
        "genlayer_transaction_id",
        mode="before",
    )(_normalize_nonzero_bytes32)
    _addresses = field_validator(
        "provider", "buyer", "arc_asset", "genlayer_contract_address", mode="before"
    )(_normalize_address)


class SettlementRecord(_FrozenStrictModel):
    """Durable relay state; WarrantyBond remains the replay source of truth."""

    binding: AssuranceSettlementBinding
    dispute_id: str
    payment_id_hash: str
    binding_hash: str
    status: SettlementStatus
    attempt_count: int = Field(ge=0)
    arc_transaction_hash: Optional[str] = None
    result: Optional[AssuranceSettlementResult] = None
    error: Optional[str] = Field(default=None, max_length=2000)
    created_at: float = Field(gt=0)
    updated_at: float = Field(gt=0)

    _hashes = field_validator("dispute_id", "payment_id_hash", "binding_hash", mode="before")(
        _normalize_nonzero_bytes32
    )
    _tx_hash = field_validator("arc_transaction_hash", mode="before")(
        lambda value: None if value is None else _normalize_nonzero_bytes32(value)
    )

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if self.dispute_id != self.binding.dispute_id:
            raise ValueError("settlement record dispute does not match binding")
        if self.payment_id_hash != self.binding.payment_id_hash:
            raise ValueError("settlement record payment does not match binding")
        if self.binding_hash != self.binding.binding_hash:
            raise ValueError("settlement record hash does not match binding")
        if self.updated_at < self.created_at:
            raise ValueError("settlement updated_at precedes created_at")
        if self.status == SettlementStatus.ELIGIBLE and self.attempt_count != 0:
            raise ValueError("eligible settlement cannot have an attempt")
        if self.status in {SettlementStatus.PENDING, SettlementStatus.CONFIRMED}:
            if self.arc_transaction_hash is None:
                raise ValueError("pending/confirmed settlement requires an Arc tx hash")
        if self.status == SettlementStatus.CONFIRMED:
            if self.result is None:
                raise ValueError("confirmed settlement requires a result")
            if self.result.arc_refund_transaction_hash != self.arc_transaction_hash:
                raise ValueError("settlement result transaction hash mismatch")
        elif self.result is not None:
            raise ValueError("only confirmed settlement may contain a result")
        return self

    @classmethod
    def eligible(
        cls,
        binding: AssuranceSettlementBinding,
        *,
        now: float,
    ) -> Self:
        return cls(
            binding=binding,
            dispute_id=binding.dispute_id,
            payment_id_hash=binding.payment_id_hash,
            binding_hash=binding.binding_hash,
            status=SettlementStatus.ELIGIBLE,
            attempt_count=0,
            created_at=now,
            updated_at=now,
        )


class AssuranceSettlementRelay:
    """Trusted, single-submit Arc relay for one configured operator and bond."""

    def __init__(
        self,
        *,
        store: AssuranceStore,
        bond_client: WarrantyBondClient,
        payment_verifier: ArcPaymentVerifier,
        operator_address: str,
        genlayer_contract_address: str,
        clock: Callable[[], float] = time.time,
        retry_policy: Optional[GenLayerRetryPolicy] = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = lambda: 0.0,
    ) -> None:
        self.store = store
        self.bond = bond_client
        self.payment_verifier = payment_verifier
        self.operator_address = _normalize_address(operator_address)
        self.genlayer_contract_address = _normalize_address(genlayer_contract_address)
        self.clock = clock
        self.retry_policy = retry_policy or GenLayerRetryPolicy()
        self.sleep = sleep
        self.jitter = jitter

    def settle(self, binding: AssuranceSettlementBinding) -> AssuranceSettlementResult:
        """Settle or resume exactly one fully derived WarrantyBond refund."""
        binding.ensure_eligible()
        if binding.genlayer_contract_address != self.genlayer_contract_address:
            raise SettlementBindingError("binding uses the wrong GenLayer contract")

        record = self.store.get_settlement(binding.dispute_id)
        if record is None:
            record = self.store.create_settlement(
                SettlementRecord.eligible(binding, now=self.clock())
            )
        elif record.binding_hash != binding.binding_hash:
            raise SettlementAlreadyProcessed(
                "local dispute or payment is already bound to another settlement"
            )

        if record.status == SettlementStatus.CONFIRMED:
            assert record.result is not None
            return record.result
        if record.status == SettlementStatus.PENDING:
            return self._resolve_pending(record)
        if record.status in {
            SettlementStatus.SUBMISSION_ATTEMPTED,
            SettlementStatus.AMBIGUOUS_SUBMISSION,
        }:
            return self._resolve_ambiguous(record)
        if record.status in {
            SettlementStatus.ALREADY_SETTLED,
            SettlementStatus.FAILED,
            SettlementStatus.FAILED_BEFORE_SUBMISSION,
        }:
            raise SettlementAlreadyProcessed(f"local settlement is already {record.status.value}")

        try:
            self._preflight(binding)
        except SettlementAlreadyProcessed as exc:
            self._transition(
                record,
                status=SettlementStatus.ALREADY_SETTLED,
                error=str(exc),
            )
            raise
        except Exception as exc:
            self._transition(
                record,
                status=SettlementStatus.FAILED_BEFORE_SUBMISSION,
                error=str(exc),
            )
            raise

        attempted = self._transition(
            record,
            status=SettlementStatus.SUBMISSION_ATTEMPTED,
            attempt_count=record.attempt_count + 1,
        )
        try:
            tx_hash = self.bond.submit_refund(
                binding.dispute_id,
                binding.payment_id_hash,
                binding.verified_payment.provider,
                binding.verified_payment.buyer,
                binding.verified_payment.amount_atomic,
            )
        except SettlementSubmissionOutcomeUnknown as exc:
            self._transition(
                attempted,
                status=SettlementStatus.AMBIGUOUS_SUBMISSION,
                arc_transaction_hash=exc.transaction_hash,
                error=str(exc),
            )
            raise
        except Exception as exc:
            self._transition(
                attempted,
                status=SettlementStatus.FAILED_BEFORE_SUBMISSION,
                error=str(exc),
            )
            raise

        pending = self._transition(
            attempted,
            status=SettlementStatus.PENDING,
            arc_transaction_hash=tx_hash,
        )
        return self._resolve_pending(pending)

    def _preflight(self, binding: AssuranceSettlementBinding) -> None:
        reverified = self._safe_read(
            "Arc x402 payment verification",
            lambda: self.payment_verifier.verify(binding.evidence),
        )
        bound_payment = binding.verified_payment
        immutable_fields = (
            "payment_id",
            "payment_id_hash",
            "transaction_hash",
            "block_number",
            "block_timestamp",
            "buyer",
            "provider",
            "asset",
            "amount_atomic",
            "chain_id",
            "evidence_hash",
        )
        if (
            any(
                getattr(reverified, field) != getattr(bound_payment, field)
                for field in immutable_fields
            )
            or reverified.confirmations < bound_payment.confirmations
        ):
            raise SettlementBindingError(
                "fresh ArcPaymentVerifier result does not match settlement binding"
            )
        account = getattr(self.bond, "account", None)
        account_address = getattr(account, "address", None)
        if account_address is None or _normalize_address(account_address) != self.operator_address:
            raise SettlementPreflightError(
                "WarrantyBond signer does not match the configured Assurance operator"
            )
        if _normalize_address(self.bond.address) != binding.warranty_bond_address:
            raise SettlementPreflightError(
                "configured WarrantyBond does not match snapshotted Assurance terms"
            )
        chain_id = int(self._safe_read("Arc chain ID", self.bond.chain_id))
        if chain_id != ARC_TESTNET_CHAIN_ID:
            raise SettlementPreflightError("WarrantyBond client is not on Arc Testnet")
        resolver = _normalize_address(self._safe_read("WarrantyBond resolver", self.bond.resolver))
        if resolver != self.operator_address:
            raise SettlementPreflightError(
                "WarrantyBond resolver does not match the Assurance operator"
            )
        asset = _normalize_address(self._safe_read("WarrantyBond USDC", self.bond.usdc))
        if asset != ARC_TESTNET_USDC or asset != binding.arc_asset:
            raise SettlementPreflightError("WarrantyBond asset is not Arc Testnet USDC")
        processed = bool(
            self._safe_read(
                "WarrantyBond dispute replay flag",
                lambda: self.bond.dispute_processed(binding.dispute_id),
            )
        )
        refunded = bool(
            self._safe_read(
                "WarrantyBond payment replay flag",
                lambda: self.bond.payment_refunded(binding.payment_id_hash),
            )
        )
        if processed or refunded:
            raise SettlementAlreadyProcessed(
                "WarrantyBond already processed the dispute or refunded the payment"
            )
        balance = int(
            self._safe_read(
                "provider warranty balance",
                lambda: self.bond.bond_balance(binding.provider),
            )
        )
        if balance < binding.payment_amount:
            raise SettlementPreflightError(
                f"provider bond has {balance}; refund requires {binding.payment_amount}"
            )

    def _resolve_ambiguous(self, record: SettlementRecord) -> AssuranceSettlementResult:
        if record.arc_transaction_hash is not None:
            pending = self._transition(
                record,
                status=SettlementStatus.PENDING,
                arc_transaction_hash=record.arc_transaction_hash,
            )
            return self._resolve_pending(pending)
        binding = record.binding
        processed = bool(
            self._safe_read(
                "WarrantyBond dispute replay flag",
                lambda: self.bond.dispute_processed(binding.dispute_id),
            )
        )
        refunded = bool(
            self._safe_read(
                "WarrantyBond payment replay flag",
                lambda: self.bond.payment_refunded(binding.payment_id_hash),
            )
        )
        if processed or refunded:
            self._transition(
                record,
                status=SettlementStatus.ALREADY_SETTLED,
                error="on-chain replay state shows the refund was consumed",
            )
            raise SettlementAlreadyProcessed(
                "ambiguous submission is already consumed by WarrantyBond"
            )
        if record.status == SettlementStatus.SUBMISSION_ATTEMPTED:
            record = self._transition(
                record,
                status=SettlementStatus.AMBIGUOUS_SUBMISSION,
                error="submission attempt has no safely recoverable transaction hash",
            )
        raise SettlementSubmissionOutcomeUnknown(
            record.error or "Arc settlement submission outcome remains unknown"
        )

    def _resolve_pending(self, record: SettlementRecord) -> AssuranceSettlementResult:
        tx_hash = record.arc_transaction_hash
        if tx_hash is None:
            raise AssuranceSettlementError("pending settlement has no transaction hash")
        try:
            receipt = self.bond.wait_for_refund(tx_hash)
        except SettlementTransactionFailed as exc:
            self._transition(
                record,
                status=SettlementStatus.FAILED,
                error=str(exc),
            )
            raise
        except Exception as exc:
            raise SettlementPending(
                tx_hash,
                f"could not resolve Arc refund receipt: {exc}",
            ) from exc
        result = self._result(record.binding, receipt)
        self._transition(
            record,
            status=SettlementStatus.CONFIRMED,
            result=result,
            arc_transaction_hash=result.arc_refund_transaction_hash,
        )
        return result

    def _result(
        self,
        binding: AssuranceSettlementBinding,
        receipt: BondTransactionResult,
    ) -> AssuranceSettlementResult:
        return AssuranceSettlementResult(
            arc_refund_transaction_hash=receipt.tx_hash,
            arc_block_number=receipt.block_number,
            gas_used=receipt.gas_used,
            dispute_id=binding.dispute_id,
            payment_id_hash=binding.payment_id_hash,
            evidence_hash=binding.evidence_hash,
            provider=binding.verified_payment.provider,
            buyer=binding.verified_payment.buyer,
            amount_atomic=binding.verified_payment.amount_atomic,
            arc_asset=binding.arc_asset,
            original_arc_payment_transaction=binding.original_arc_payment_transaction,
            genlayer_transaction_id=binding.genlayer_transaction_id,
            genlayer_contract_address=binding.genlayer_contract_address,
            sla_violated=True,
            settled_at=self.clock(),
        )

    def _safe_read(self, label: str, operation: Callable[[], Any]) -> Any:
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                return operation()
            except Exception as exc:
                if not is_transient_transport_error(exc):
                    raise SettlementPreflightError(f"{label} failed: {exc}") from exc
                if attempt == self.retry_policy.max_attempts:
                    raise SettlementPreflightError(
                        f"{label} transport failed after {attempt} attempts"
                    ) from exc
                self.sleep(
                    self.retry_policy.delay_before_attempt(
                        attempt + 1,
                        self.jitter(),
                    )
                )
        raise RuntimeError("unreachable retry state")

    def _transition(
        self,
        record: SettlementRecord,
        *,
        status: SettlementStatus,
        attempt_count: Optional[int] = None,
        arc_transaction_hash: Optional[str] = None,
        result: Optional[AssuranceSettlementResult] = None,
        error: Optional[str] = None,
    ) -> SettlementRecord:
        updated_at = max(self.clock(), record.updated_at + 0.000001)
        validated = SettlementRecord(
            binding=record.binding,
            dispute_id=record.dispute_id,
            payment_id_hash=record.payment_id_hash,
            binding_hash=record.binding_hash,
            status=status,
            attempt_count=(record.attempt_count if attempt_count is None else attempt_count),
            arc_transaction_hash=arc_transaction_hash,
            result=result,
            error=error,
            created_at=record.created_at,
            updated_at=updated_at,
        )
        return self.store.update_settlement(validated)


__all__ = [
    "AssuranceSettlementBinding",
    "AssuranceSettlementRelay",
    "AssuranceSettlementResult",
    "FinalizedGenLayerAdjudication",
    "SettlementRecord",
    "SettlementStatus",
]
