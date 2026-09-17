"""Strict, immutable data commitments for AgentPay Assurance."""

from __future__ import annotations

import base64
from enum import Enum
import hashlib
import json
import re
from typing import Any, Iterable, Literal, Mapping, Optional, Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ..models import AssuranceTerms, Service
from ..workflow.models import hash_content

DEFAULT_MAX_REQUEST_BODY_BYTES = 64 * 1024
DEFAULT_MAX_RESPONSE_BODY_BYTES = 64 * 1024
MAX_PROTOCOL_VALUE_BYTES = 64 * 1024

DEFAULT_REQUEST_HEADER_ALLOWLIST = frozenset(
    {"accept", "content-type", "traceparent", "tracestate", "user-agent", "x-request-id"}
)
DEFAULT_RESPONSE_HEADER_ALLOWLIST = frozenset(
    {
        "cache-control",
        "content-length",
        "content-type",
        "date",
        "etag",
        "last-modified",
        "traceparent",
        "x-request-id",
    }
)
_SECRET_HEADER_NAMES = frozenset(
    {
        "api-key",
        "authorization",
        "cookie",
        "payment-signature",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
        "x-payment",
    }
)
_SENSITIVE_JSON_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "auth",
        "clientsecret",
        "cookie",
        "password",
        "paymentsignature",
        "privatekey",
        "refreshtoken",
        "secret",
        "signature",
        "token",
        "xpayment",
    }
)
_BYTES32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"value of type {type(value).__name__} is not canonical JSON")


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value with stable ordering and no whitespace."""
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(value: Any) -> str:
    """Keccak-256 of the canonical UTF-8 JSON representation."""
    return hash_content(canonical_json(value))


def hash_assurance_terms(terms: AssuranceTerms) -> str:
    """Stable commitment to exactly one advertised Assurance terms snapshot."""
    return canonical_hash(terms.model_dump(mode="json"))


def _normalize_bytes32(value: str) -> str:
    if not isinstance(value, str) or not _BYTES32_RE.fullmatch(value):
        raise ValueError("must be a 32-byte 0x-prefixed hex value")
    return value.lower()


def _sensitive_json_key(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).lower())
    return normalized in _SENSITIVE_JSON_KEYS


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _sensitive_json_key(key) else _redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return value


def _sanitize_url(value: str) -> str:
    """Redact credentials and sensitive query values while preserving routing."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        query = urlencode(
            [
                (key, "[REDACTED]" if _sensitive_json_key(key) else item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ],
            doseq=True,
        )
        return urlunsplit((parsed.scheme, host, parsed.path, query, parsed.fragment))
    except (TypeError, ValueError):
        return value.split("?", 1)[0]


def _sha256_text(value: str) -> str:
    return "0x" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class _FrozenStrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class HeaderSnapshot(_FrozenStrictModel):
    name: str
    value: str


class BodySnapshot(_FrozenStrictModel):
    """Bounded body representation plus a hash of the exact original bytes."""

    representation: Literal["canonical-json", "utf-8", "base64", "sha256"]
    content: Optional[str]
    sha256: str
    original_size: int = Field(ge=0)
    truncated: bool = False

    _hash = field_validator("sha256", mode="before")(_normalize_bytes32)


def sanitize_headers(
    headers: Mapping[str, Any] | Iterable[tuple[str, Any]],
    *,
    allowlist: Iterable[str],
) -> tuple[HeaderSnapshot, ...]:
    """Create a sorted allowlisted header snapshot; secret names are never accepted."""
    allowed = {str(name).lower() for name in allowlist} - _SECRET_HEADER_NAMES
    items = headers.items() if isinstance(headers, Mapping) else headers
    captured: dict[str, str] = {}
    for raw_name, raw_value in items:
        name = str(raw_name).lower()
        if name in allowed:
            captured[name] = str(raw_value)
    return tuple(
        HeaderSnapshot(name=name, value=value)
        for name, value in sorted(captured.items())
    )


def capture_body(
    body: bytes,
    *,
    content_type: str = "",
    max_bytes: int,
    request: bool = False,
) -> BodySnapshot:
    """Capture a bounded body, redacting JSON and hashing the exact input.

    Request bodies that are not declared JSON are hash-only because arbitrary
    text/form/binary formats cannot be reliably scrubbed for credentials.
    Oversized bodies are also hash-only rather than retaining a potentially
    sensitive or misleading prefix.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    raw = bytes(body)
    digest = "0x" + hashlib.sha256(raw).hexdigest()
    oversized = len(raw) > max_bytes
    if oversized:
        return BodySnapshot(
            representation="sha256",
            content=None,
            sha256=digest,
            original_size=len(raw),
            truncated=True,
        )

    is_json = "json" in content_type.lower()
    if is_json:
        try:
            parsed = json.loads(raw.decode("utf-8"))
            content = canonical_json(_redact_json(parsed))
            if len(content.encode("utf-8")) <= max_bytes:
                return BodySnapshot(
                    representation="canonical-json",
                    content=content,
                    sha256=digest,
                    original_size=len(raw),
                )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            pass

    if request:
        return BodySnapshot(
            representation="sha256",
            content=None,
            sha256=digest,
            original_size=len(raw),
        )

    try:
        content = raw.decode("utf-8")
        representation: Literal["utf-8", "base64"] = "utf-8"
    except UnicodeDecodeError:
        content = base64.b64encode(raw).decode("ascii")
        representation = "base64"
    return BodySnapshot(
        representation=representation,
        content=content,
        sha256=digest,
        original_size=len(raw),
    )


def _header_value(headers: tuple[HeaderSnapshot, ...], name: str) -> str:
    target = name.lower()
    return next((header.value for header in headers if header.name == target), "")


def _safe_protocol_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    raw = value.encode("utf-8")
    if len(raw) <= MAX_PROTOCOL_VALUE_BYTES:
        candidates = [value]
        try:
            candidates.append(base64.b64decode(value + "==").decode("utf-8"))
        except Exception:
            pass
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            if canonical_json(_redact_json(parsed)) != canonical_json(parsed):
                return "sha256:" + hashlib.sha256(raw).hexdigest()
        return value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class AssuranceEvidence(_FrozenStrictModel):
    """Self-contained, deterministic snapshot of one assured x402 exchange."""

    schema_version: Literal["1"] = "1"
    evidence_id: str
    evidence_hash: str
    payment_id: str = Field(min_length=1, max_length=128)
    service_snapshot: str = Field(min_length=2)
    terms_hash: str
    request_method: str = Field(min_length=1)
    request_url: str = Field(min_length=1)
    request_url_hash: str
    request_body: BodySnapshot
    request_headers: tuple[HeaderSnapshot, ...] = ()
    parsed_payment_required: str = Field(min_length=2)
    selected_x402_requirement: str = Field(min_length=2)
    original_payment_required: Optional[str] = None
    payment_payload_hash: str
    request_started_at: float = Field(gt=0)
    payment_required_at: float = Field(gt=0)
    payment_authorized_at: float = Field(gt=0)
    paid_response_received_at: float = Field(gt=0)
    paid_response_status: Optional[int] = Field(default=None, ge=100, le=599)
    response_body: BodySnapshot
    response_body_max_bytes: int = Field(ge=0)
    response_headers: tuple[HeaderSnapshot, ...] = ()
    raw_payment_response: Optional[str] = None
    decoded_settlement_response: str = Field(min_length=2)
    arc_transaction_reference: Optional[str] = None
    payment_status: str = Field(min_length=1)
    transport_error: Optional[str] = None

    _hashes = field_validator(
        "evidence_id",
        "evidence_hash",
        "terms_hash",
        "request_url_hash",
        "payment_payload_hash",
        mode="before",
    )(_normalize_bytes32)

    @model_validator(mode="after")
    def validate_commitments(self) -> Self:
        if not (
            self.request_started_at
            <= self.payment_required_at
            <= self.payment_authorized_at
            <= self.paid_response_received_at
        ):
            raise ValueError("evidence timestamps must follow the payment lifecycle")
        try:
            service = json.loads(self.service_snapshot)
            advertised_terms = service["assurance"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError("service_snapshot must contain Assurance terms") from exc
        if canonical_json(service) != self.service_snapshot:
            raise ValueError("service_snapshot must be canonical JSON")
        if canonical_hash(advertised_terms) != self.terms_hash:
            raise ValueError("terms_hash does not match the service snapshot")
        for value, label in (
            (self.parsed_payment_required, "parsed_payment_required"),
            (self.selected_x402_requirement, "selected_x402_requirement"),
            (self.decoded_settlement_response, "decoded_settlement_response"),
        ):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{label} must be canonical JSON") from exc
            if canonical_json(parsed) != value:
                raise ValueError(f"{label} must be canonical JSON")
        if self.paid_response_status is None and self.transport_error is None:
            raise ValueError("missing paid response requires a transport_error")
        if self.evidence_id != self.evidence_hash:
            raise ValueError("evidence_id must match evidence_hash in Phase 1")
        if canonical_hash(self._hash_payload()) != self.evidence_hash:
            raise ValueError("evidence_hash does not match the evidence contents")
        return self

    def _hash_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"evidence_hash", "evidence_id"},
        )

    @classmethod
    def from_exchange(
        cls,
        exchange: Any,
        *,
        service: Service,
        max_response_body_bytes: int = DEFAULT_MAX_RESPONSE_BODY_BYTES,
        max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
        request_header_allowlist: Iterable[str] = DEFAULT_REQUEST_HEADER_ALLOWLIST,
        response_header_allowlist: Iterable[str] = DEFAULT_RESPONSE_HEADER_ALLOWLIST,
    ) -> Self:
        """Build evidence from the generic interceptor observation."""
        if service.assurance is None:
            raise ValueError("service does not advertise Assurance terms")
        request_headers = sanitize_headers(
            exchange.request_headers, allowlist=request_header_allowlist
        )
        response_headers = sanitize_headers(
            exchange.paid_response_headers, allowlist=response_header_allowlist
        )
        request_content_type = _header_value(request_headers, "content-type")
        response_content_type = _header_value(response_headers, "content-type")
        service_data = service.model_dump(mode="json")
        service_data["url"] = _sanitize_url(service_data["url"])
        service_snapshot = canonical_json(service_data)
        payload = {
            "schema_version": "1",
            "payment_id": exchange.payment_id,
            "service_snapshot": service_snapshot,
            "terms_hash": hash_assurance_terms(service.assurance),
            "request_method": exchange.request_method,
            "request_url": _sanitize_url(exchange.request_url),
            "request_url_hash": _sha256_text(exchange.request_url),
            "request_body": capture_body(
                exchange.request_body,
                content_type=request_content_type,
                max_bytes=max_request_body_bytes,
                request=True,
            ),
            "request_headers": request_headers,
            "parsed_payment_required": canonical_json(
                _redact_json(exchange.parsed_payment_required)
            ),
            "selected_x402_requirement": canonical_json(
                _redact_json(exchange.selected_requirement)
            ),
            "original_payment_required": _safe_protocol_value(
                exchange.original_payment_required
            ),
            "payment_payload_hash": exchange.payment_payload_hash,
            "request_started_at": exchange.request_started_at,
            "payment_required_at": exchange.payment_required_at,
            "payment_authorized_at": exchange.payment_authorized_at,
            "paid_response_received_at": exchange.paid_response_received_at,
            "paid_response_status": exchange.paid_response_status,
            "response_body": capture_body(
                exchange.paid_response_body,
                content_type=response_content_type,
                max_bytes=max_response_body_bytes,
            ),
            "response_body_max_bytes": max_response_body_bytes,
            "response_headers": response_headers,
            "raw_payment_response": _safe_protocol_value(exchange.raw_payment_response),
            "decoded_settlement_response": canonical_json(
                _redact_json(exchange.settlement_response)
            ),
            "arc_transaction_reference": exchange.transaction_reference,
            "payment_status": exchange.payment_status,
            "transport_error": (
                "paid request transport failed" if exchange.transport_error else None
            ),
        }
        evidence_hash = canonical_hash(payload)
        return cls(evidence_id=evidence_hash, evidence_hash=evidence_hash, **payload)


class DisputeStatus(str, Enum):
    OPEN = "open"
    ADJUDICATING = "adjudicating"
    UPHELD = "upheld"
    REJECTED = "rejected"
    ERROR = "error"


class AssuranceVerdict(_FrozenStrictModel):
    """Adjudication result model only; Phase 1 does not produce verdicts."""

    dispute_id: str = Field(min_length=1, max_length=128)
    evidence_hash: str
    sla_violated: bool
    reason: str = Field(min_length=1, max_length=4000)
    decided_at: float = Field(gt=0)
    adjudicator_reference: Optional[str] = Field(default=None, max_length=512)

    _evidence_hash = field_validator("evidence_hash", mode="before")(_normalize_bytes32)

    @property
    def verdict_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


class Dispute(_FrozenStrictModel):
    """Durable local dispute state, bound to one payment and evidence snapshot."""

    dispute_id: str = Field(min_length=1, max_length=128)
    payment_id: str = Field(min_length=1, max_length=128)
    evidence_id: str
    evidence_hash: str
    reason: str = Field(min_length=1, max_length=4000)
    status: DisputeStatus = DisputeStatus.OPEN
    opened_at: float = Field(gt=0)
    updated_at: float = Field(gt=0)
    verdict: Optional[AssuranceVerdict] = None

    _hashes = field_validator("evidence_id", "evidence_hash", mode="before")(
        _normalize_bytes32
    )

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.evidence_id != self.evidence_hash:
            raise ValueError("evidence_id must match evidence_hash in Phase 1")
        if self.updated_at < self.opened_at:
            raise ValueError("updated_at must not precede opened_at")
        terminal = self.status in {DisputeStatus.UPHELD, DisputeStatus.REJECTED}
        if terminal != (self.verdict is not None):
            raise ValueError("terminal dispute states require exactly one verdict")
        if self.verdict is not None:
            if self.verdict.dispute_id != self.dispute_id:
                raise ValueError("verdict dispute_id does not match the dispute")
            if self.verdict.evidence_hash != self.evidence_hash:
                raise ValueError("verdict evidence_hash does not match the dispute")
            expected = DisputeStatus.UPHELD if self.verdict.sla_violated else DisputeStatus.REJECTED
            if self.status != expected:
                raise ValueError("dispute status does not match the verdict")
        return self
