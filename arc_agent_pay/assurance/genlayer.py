"""Compact GenLayer SLA adjudication payloads and finalized-verdict adapter.

GenLayer judges only whether the frozen delivery materially violated its SLA.
Arc payment validity, buyer/provider financial identity, refund amount, and bond
solvency remain outside this module and must be cross-bound by a later relay.

The current Consensus v0.6 preview requires ``genlayer-py==0.19.0rc2`` and
Python 3.12+. The dependency is lazy so core AgentPay installations and mocked
unit tests do not need the preview SDK.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import re
import sys
from typing import Any, Literal, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..exceptions import (
    AdjudicationPayloadTooLarge,
    GenLayerAdjudicationError,
    GenLayerExecutionFailed,
    GenLayerFinalityTimeout,
    GenLayerTransactionNotFinal,
    GenLayerSubmissionOutcomeUnknown,
    GenLayerTransportRetryExhausted,
    GenLayerVerdictBindingMismatch,
    GenLayerVerdictNotFound,
)
from ..models import AssuranceTerms
from ..workflow.models import _normalize_address, _normalize_nonzero_bytes32
from .arc_payment import dispute_id as derive_dispute_id
from .arc_payment import payment_id_hash as derive_payment_id_hash
from .models import AssuranceEvidence, BodySnapshot, canonical_json
from .genlayer_transport import (
    begin_submission,
    finish_submission,
    retry_exhausted_error,
    submission_error,
)

GENLAYER_PY_VERSION = "0.19.0rc2"
MAX_ADJUDICATION_PAYLOAD_BYTES = 16 * 1024
MAX_BODY_CONTENT_BYTES = 4 * 1024
MAX_SERVICE_DESCRIPTION_BYTES = 2 * 1024
MAX_BUYER_ALLEGATION_BYTES = 2 * 1024
MAX_REASON_CHARS = 512

_ZERO_ADDRESS = "0x" + "0" * 40
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api-key",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "key",
        "password",
        "secret",
        "signature",
        "token",
        "x-api-key",
    }
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(authorization|cookie|x-api-key|api[_-]?key|password|private[_-]?key|"
    r"secret|token)\b(\s*[:=]\s*)([^\s,;]{4,})",
    re.IGNORECASE,
)


class _FrozenStrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _normalize_bytes32(value: str) -> str:
    return _normalize_nonzero_bytes32(value)


def _checksum_address(value: str) -> str:
    """Keep Web3/genlayer-py contract arguments checksum-valid."""
    normalized = _normalize_address(value)
    try:
        from eth_utils import to_checksum_address
    except ImportError:
        return value
    return to_checksum_address(normalized)


def _byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def _sanitize_untrusted_text(value: str) -> str:
    """Best-effort secret redaction for bounded free text sent to GenLayer."""
    redacted = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", value)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    return _SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", redacted)


def _bounded_text(value: str, *, maximum: int, label: str) -> str:
    sanitized = _sanitize_untrusted_text(str(value))
    if _byte_length(sanitized) > maximum:
        raise ValueError(f"{label} exceeds {maximum} UTF-8 bytes")
    return sanitized


def _sanitize_payload_url(value: str) -> str:
    """Remove userinfo and redact common secret-bearing query parameters."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        query = urlencode(
            [
                (
                    key,
                    "[REDACTED]"
                    if key.lower() in _SENSITIVE_QUERY_KEYS
                    else _sanitize_untrusted_text(item),
                )
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ],
            doseq=True,
        )
        sanitized = urlunsplit((parsed.scheme, host, parsed.path, query, parsed.fragment))
    except (TypeError, ValueError):
        sanitized = str(value).split("?", 1)[0]
    if _byte_length(sanitized) > 2048:
        digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"
    return sanitized


class AdjudicationSLA(_FrozenStrictModel):
    """Only SLA fields relevant to judging delivery quality or timeliness."""

    version: str = Field(min_length=1, max_length=32)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1, max_length=32)
    max_response_time_ms: Optional[int] = Field(default=None, gt=0)

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_criteria(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() or _byte_length(value) > 1024 for value in values):
            raise ValueError("acceptance criteria must be non-empty and at most 1024 bytes")
        return tuple(value.strip() for value in values)


class AdjudicationBody(_FrozenStrictModel):
    """Sanitized bounded content plus the exact Phase 1 body commitment."""

    representation: Literal["canonical-json", "utf-8", "base64", "sha256"]
    content: Optional[str] = None
    sha256: str
    original_size: int = Field(ge=0)
    content_included: bool

    _hash = field_validator("sha256", mode="before")(_normalize_bytes32)

    @model_validator(mode="after")
    def validate_content_flag(self) -> "AdjudicationBody":
        if self.content_included != (self.content is not None):
            raise ValueError("content_included must match content presence")
        if self.content is not None and _byte_length(self.content) > MAX_BODY_CONTENT_BYTES:
            raise ValueError("adjudication body content exceeds its byte limit")
        return self


class AdjudicationTiming(_FrozenStrictModel):
    payment_authorized_at_ms: int = Field(gt=0)
    paid_response_received_at_ms: int = Field(gt=0)
    delivery_elapsed_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_elapsed(self) -> "AdjudicationTiming":
        expected = self.paid_response_received_at_ms - self.payment_authorized_at_ms
        if expected < 0 or self.delivery_elapsed_ms != expected:
            raise ValueError("delivery timing is inconsistent")
        return self


class AdjudicationPayload(_FrozenStrictModel):
    """Canonical, compact, adjudication-only projection of Assurance evidence."""

    schema_version: Literal["1"] = "1"
    dispute_id: str
    payment_id_hash: str
    evidence_hash: str
    terms_hash: str
    sla: AdjudicationSLA
    service_description: str = Field(max_length=2048)
    request_method: str = Field(min_length=1, max_length=32)
    request_url: str = Field(min_length=1, max_length=2048)
    request_body: AdjudicationBody
    paid_response_status: Optional[int] = Field(default=None, ge=100, le=599)
    response_body: AdjudicationBody
    timing: AdjudicationTiming
    buyer_allegation: str = Field(min_length=1, max_length=2048)

    _hashes = field_validator(
        "dispute_id",
        "payment_id_hash",
        "evidence_hash",
        "terms_hash",
        mode="before",
    )(_normalize_bytes32)

    @property
    def canonical(self) -> str:
        return canonical_json(self.model_dump(mode="json"))

    @property
    def input_hash(self) -> str:
        return adjudication_input_hash(self.canonical)

    @property
    def size_bytes(self) -> int:
        return _byte_length(self.canonical)

    @classmethod
    def from_evidence(
        cls,
        evidence: AssuranceEvidence,
        *,
        buyer_dispute_reason: str,
    ) -> "AdjudicationPayload":
        try:
            service = json.loads(evidence.service_snapshot)
            terms = AssuranceTerms.model_validate(service["assurance"])
        except Exception as exc:
            raise ValueError("evidence has no valid snapshotted Assurance terms") from exc

        payment_hash = derive_payment_id_hash(evidence.payment_id)
        payload = cls(
            dispute_id=derive_dispute_id(payment_hash, evidence.evidence_hash),
            payment_id_hash=payment_hash,
            evidence_hash=evidence.evidence_hash,
            terms_hash=evidence.terms_hash,
            sla=AdjudicationSLA(
                version=terms.version,
                acceptance_criteria=tuple(terms.acceptance_criteria),
                max_response_time_ms=terms.max_response_time_ms,
            ),
            service_description=_bounded_text(
                str(service.get("description", "")),
                maximum=MAX_SERVICE_DESCRIPTION_BYTES,
                label="service description",
            ),
            request_method=evidence.request_method,
            request_url=_sanitize_payload_url(evidence.request_url),
            request_body=_compact_body(evidence.request_body),
            paid_response_status=evidence.paid_response_status,
            response_body=_compact_body(evidence.response_body),
            timing=_timing(evidence),
            buyer_allegation=_bounded_text(
                buyer_dispute_reason,
                maximum=MAX_BUYER_ALLEGATION_BYTES,
                label="buyer dispute reason",
            ),
        )
        _ensure_payload_size(payload)
        return payload


class GenLayerSubmission(_FrozenStrictModel):
    transaction_id: str
    dispute_id: str
    adjudication_input_hash: str

    _hashes = field_validator(
        "transaction_id", "dispute_id", "adjudication_input_hash", mode="before"
    )(_normalize_bytes32)


class GenLayerTransactionStatus(_FrozenStrictModel):
    transaction_id: str
    state: str = Field(min_length=1)
    outcome: Optional[str] = None
    execution_result: Optional[str] = None

    _transaction_id = field_validator("transaction_id", mode="before")(
        _normalize_bytes32
    )

    @property
    def finalized(self) -> bool:
        return self.state == "finalized"


class GenLayerStoredVerdict(_FrozenStrictModel):
    """Finalized GenLayer verdict with every settlement-relevant binding."""

    dispute_id: str
    payment_id_hash: str
    evidence_hash: str
    terms_hash: str
    adjudication_input_hash: str
    sla_violated: bool
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)

    _hashes = field_validator(
        "dispute_id",
        "payment_id_hash",
        "evidence_hash",
        "terms_hash",
        "adjudication_input_hash",
        mode="before",
    )(_normalize_bytes32)


def _compact_body(snapshot: BodySnapshot) -> AdjudicationBody:
    content = snapshot.content
    if content is not None:
        content = _sanitize_untrusted_text(content)
    included = content is not None and _byte_length(content) <= MAX_BODY_CONTENT_BYTES
    return AdjudicationBody(
        representation=snapshot.representation if included else "sha256",
        content=content if included else None,
        sha256=snapshot.sha256,
        original_size=snapshot.original_size,
        content_included=included,
    )


def _timestamp_ms(value: float) -> int:
    return int(Decimal(str(value)) * 1000)


def _timing(evidence: AssuranceEvidence) -> AdjudicationTiming:
    authorized = _timestamp_ms(evidence.payment_authorized_at)
    received = _timestamp_ms(evidence.paid_response_received_at)
    return AdjudicationTiming(
        payment_authorized_at_ms=authorized,
        paid_response_received_at_ms=received,
        delivery_elapsed_ms=received - authorized,
    )


def adjudication_input_hash(payload: AdjudicationPayload | str) -> str:
    """SHA-256 commitment to the exact canonical UTF-8 adjudication payload."""
    serialized = payload.canonical if isinstance(payload, AdjudicationPayload) else payload
    if not isinstance(serialized, str):
        raise TypeError("adjudication payload must be canonical JSON text")
    return "0x" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _ensure_payload_size(payload: AdjudicationPayload) -> None:
    if payload.size_bytes > MAX_ADJUDICATION_PAYLOAD_BYTES:
        raise AdjudicationPayloadTooLarge(
            f"canonical adjudication payload is {payload.size_bytes} bytes; "
            f"maximum is {MAX_ADJUDICATION_PAYLOAD_BYTES}"
        )


def _transaction_id(value: Any) -> str:
    if isinstance(value, bytes):
        value = "0x" + bytes(value).hex()
    elif hasattr(value, "hex") and not isinstance(value, str):
        value = value.hex()
        if not str(value).startswith("0x"):
            value = "0x" + str(value)
    return _normalize_bytes32(value)


def _enum_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    return str(raw)


def _transaction_execution_result(transaction: Mapping[str, Any]) -> Optional[str]:
    value = next(
        (
            transaction[key]
            for key in (
                "txExecutionResultName",
                "tx_execution_result_name",
                "txExecutionResult",
                "tx_execution_result",
            )
            if transaction.get(key) is not None
        ),
        None,
    )
    raw = getattr(value, "value", value)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return {
            0: "NOT_VOTED",
            1: "FINISHED_WITH_RETURN",
            2: "FINISHED_WITH_ERROR",
            3: "TIMEOUT",
            4: "NONDET_DISAGREE",
            5: "DETERMINISTIC_VIOLATION",
        }.get(raw)
    return _enum_text(raw)


def _status_from_transaction(transaction_id: str, transaction: Mapping[str, Any]) -> GenLayerTransactionStatus:
    lifecycle = transaction.get("lifecycle")
    if not isinstance(lifecycle, Mapping) or not isinstance(lifecycle.get("state"), str):
        raise GenLayerAdjudicationError("GenLayer transaction has no valid lifecycle")
    return GenLayerTransactionStatus(
        transaction_id=transaction_id,
        state=lifecycle["state"],
        outcome=_enum_text(lifecycle.get("outcome")),
        execution_result=_transaction_execution_result(transaction),
    )


def _is_successful_finalization(transaction: Mapping[str, Any]) -> bool:
    lifecycle = transaction.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        return False
    if lifecycle.get("state") != "finalized":
        return False
    if _enum_text(lifecycle.get("outcome")) not in (None, "accepted"):
        return False
    execution = _transaction_execution_result(transaction)
    if execution == "FINISHED_WITH_RETURN":
        return True
    consensus_data = transaction.get("consensus_data")
    if isinstance(consensus_data, Mapping):
        leader_receipt = consensus_data.get("leader_receipt")
        if isinstance(leader_receipt, list):
            leader_receipt = leader_receipt[0] if leader_receipt else None
        return isinstance(leader_receipt, Mapping) and leader_receipt.get(
            "execution_result"
        ) == "SUCCESS"
    return False


class GenLayerAdjudicationClient:
    """Minimal adapter around the pinned GenLayerPY finality lifecycle.

    ``chain`` is deliberately required when constructing the underlying SDK
    client: this adapter does not silently select Studionet, Studio-dev, or a
    public testnet. A preconfigured/injected client is also accepted for tests.
    """

    def __init__(
        self,
        *,
        contract_address: str,
        client: Any = None,
        chain: Any = None,
        rpc_url: Optional[str] = None,
        account: Any = None,
        fees: Optional[Mapping[str, Any]] = None,
    ) -> None:
        normalized_contract = _checksum_address(contract_address)
        if normalized_contract == _ZERO_ADDRESS:
            raise ValueError("GenLayer contract address must be non-zero")
        if client is None:
            if chain is None:
                raise ValueError("an explicit GenLayer chain configuration is required")
            if sys.version_info < (3, 12):
                raise ImportError("GenLayerPY 0.19.0rc2 requires Python 3.12+")
            try:
                installed = package_version("genlayer-py")
            except PackageNotFoundError as exc:
                raise ImportError(
                    'GenLayer support requires: pip install "arc-agent-pay[genlayer]"'
                ) from exc
            if installed != GENLAYER_PY_VERSION:
                raise ImportError(
                    f"GenLayer support requires genlayer-py=={GENLAYER_PY_VERSION}; "
                    f"found {installed}"
                )
            from genlayer_py import create_client

            client = create_client(chain=chain, endpoint=rpc_url, account=account)
        self.client = client
        self.contract_address = normalized_contract
        self.fees = None if fees is None else dict(fees)

    def submit_adjudication(self, payload: AdjudicationPayload) -> GenLayerSubmission:
        _ensure_payload_size(payload)
        kwargs: dict[str, Any] = {
            "address": self.contract_address,
            "function_name": "adjudicate",
            "args": [payload.canonical],
        }
        if self.fees is not None:
            kwargs["fees"] = self.fees
        begin_submission(self.client)
        try:
            raw_transaction_id = self.client.write_contract(**kwargs)
            transaction_id = _transaction_id(raw_transaction_id)
        except Exception as exc:
            classified = submission_error(exc, self.client)
            if isinstance(classified, GenLayerSubmissionOutcomeUnknown):
                raise classified
            if isinstance(classified, GenLayerTransportRetryExhausted):
                raise classified
            raise GenLayerAdjudicationError(
                "could not submit the GenLayer adjudication"
            ) from exc
        finish_submission(self.client)
        return GenLayerSubmission(
            transaction_id=transaction_id,
            dispute_id=payload.dispute_id,
            adjudication_input_hash=payload.input_hash,
        )

    def get_transaction_status(self, transaction_id: str) -> GenLayerTransactionStatus:
        normalized = _transaction_id(transaction_id)
        try:
            transaction = self.client.get_transaction(transaction_hash=normalized)
        except Exception as exc:
            exhausted = retry_exhausted_error(exc)
            if exhausted is not None:
                raise exhausted
            raise GenLayerAdjudicationError(
                "could not query the GenLayer transaction"
            ) from exc
        if not isinstance(transaction, Mapping):
            raise GenLayerAdjudicationError("GenLayer transaction was not found")
        return _status_from_transaction(normalized, transaction)

    def wait_for_finalized_verdict(
        self,
        submission: GenLayerSubmission,
        payload: AdjudicationPayload,
        *,
        interval: int = 3000,
        retries: int = 10,
    ) -> GenLayerStoredVerdict:
        if (
            submission.dispute_id != payload.dispute_id
            or submission.adjudication_input_hash != payload.input_hash
        ):
            raise GenLayerVerdictBindingMismatch(
                "submission does not match the expected adjudication payload"
            )
        try:
            transaction = self.client.wait_for_finalization(
                transaction_hash=submission.transaction_id,
                interval=interval,
                retries=retries,
                full_transaction=False,
            )
        except Exception as exc:
            state = None
            latest = None
            try:
                latest = self.client.get_transaction(
                    transaction_hash=submission.transaction_id
                )
                if isinstance(latest, Mapping):
                    lifecycle = latest.get("lifecycle")
                    if isinstance(lifecycle, Mapping):
                        state = lifecycle.get("state")
            except Exception:
                pass
            if state == "finalized":
                transaction = latest
            elif state == "canceled":
                raise GenLayerExecutionFailed(
                    f"GenLayer adjudication {submission.transaction_id} was canceled"
                ) from exc
            else:
                raise GenLayerFinalityTimeout(
                    submission.transaction_id, state
                ) from exc
        if not isinstance(transaction, Mapping):
            raise GenLayerTransactionNotFinal(
                "GenLayer finality response has no transaction lifecycle"
            )
        status = _status_from_transaction(submission.transaction_id, transaction)
        if not status.finalized:
            raise GenLayerTransactionNotFinal(
                f"GenLayer transaction is {status.state!r}, not finalized"
            )
        if not _is_successful_finalization(transaction):
            raise GenLayerExecutionFailed(
                "finalized GenLayer adjudication did not finish with a return"
            )

        read_kwargs: dict[str, Any] = {
            "address": self.contract_address,
            "function_name": "get_verdict",
            "args": [payload.dispute_id],
        }
        try:
            from genlayer_py.types import TransactionHashVariant

            read_kwargs["transaction_hash_variant"] = (
                TransactionHashVariant.LATEST_FINAL
            )
        except ImportError:
            # Mocked-client unit tests intentionally do not install GenLayerPY.
            pass
        try:
            raw_verdict = self.client.read_contract(**read_kwargs)
        except Exception as exc:
            exhausted = retry_exhausted_error(exc)
            if exhausted is not None:
                raise exhausted
            raise GenLayerAdjudicationError(
                "could not read the finalized GenLayer verdict"
            ) from exc
        if not raw_verdict:
            raise GenLayerVerdictNotFound("finalized adjudication stored no verdict")
        try:
            decoded = json.loads(raw_verdict) if isinstance(raw_verdict, str) else raw_verdict
            verdict = GenLayerStoredVerdict.model_validate(decoded)
        except Exception as exc:
            raise GenLayerVerdictNotFound(
                "GenLayer returned a malformed stored verdict"
            ) from exc

        expected = {
            "dispute_id": payload.dispute_id,
            "payment_id_hash": payload.payment_id_hash,
            "evidence_hash": payload.evidence_hash,
            "terms_hash": payload.terms_hash,
            "adjudication_input_hash": payload.input_hash,
        }
        for field, value in expected.items():
            if getattr(verdict, field) != value:
                raise GenLayerVerdictBindingMismatch(
                    f"finalized GenLayer verdict has the wrong {field}"
                )
        return verdict


__all__ = [
    "AdjudicationBody",
    "AdjudicationPayload",
    "AdjudicationSLA",
    "AdjudicationTiming",
    "GENLAYER_PY_VERSION",
    "GenLayerAdjudicationClient",
    "GenLayerStoredVerdict",
    "GenLayerSubmission",
    "GenLayerTransactionStatus",
    "MAX_ADJUDICATION_PAYLOAD_BYTES",
    "MAX_REASON_CHARS",
    "adjudication_input_hash",
]
