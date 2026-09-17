"""Focused Phase 1 tests for Assurance terms, evidence, storage, and observation."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

from arc_agent_pay import AssuranceTerms, PaymentClient, PaymentExchange, Service
from arc_agent_pay.assurance import (
    AssuranceEvidence,
    AssuranceStore,
    AssuranceStoreError,
    Dispute,
    DisputeStatus,
    canonical_json,
    hash_assurance_terms,
    sanitize_headers,
)
from arc_agent_pay.interceptor import _BudgetEnforcingTransport
from arc_agent_pay.models import PaymentStatus

PROVIDER = "0x" + "a" * 40
BOND = "0x" + "b" * 40
ASSET = "0x" + "c" * 40
RETRY_KEY = _BudgetEnforcingTransport._RETRY_KEY


def _terms(**changes) -> AssuranceTerms:
    values = {
        "provider_address": PROVIDER,
        "warranty_bond_address": BOND,
        "acceptance_criteria": ["Response contains a current USD price"],
        "max_response_time_ms": 2_000,
        "dispute_window_seconds": 3_600,
    }
    values.update(changes)
    return AssuranceTerms(**values)


def _service(**changes) -> Service:
    values = {
        "name": "Price API",
        "url": "https://api.example.test/price",
        "description": "Returns an asset price",
        "price_usdc": "0.01",
        "tags": ["prices"],
        "assurance": _terms(),
    }
    values.update(changes)
    return Service(**values)


def _exchange(**changes) -> PaymentExchange:
    values = {
        "payment_id": "payment_assurance_0001",
        "service_url": "https://api.example.test/price",
        "amount_usdc": "0.01",
        "amount_atomic": "10000",
        "chain": "ARC-TESTNET",
        "network": "eip155:5042002",
        "asset": ASSET,
        "pay_to": PROVIDER,
        "scheme": "exact",
        "payment_status": "success",
        "request_method": "POST",
        "request_url": "https://api.example.test/price?asset=ETH",
        "request_body": b'{"asset":"ETH","api_key":"do-not-store"}',
        "request_headers": {
            "content-type": "application/json",
            "x-request-id": "request-1",
        },
        "parsed_payment_required": {"accepts": [{"amount": "10000"}], "x402Version": 2},
        "selected_requirement": {
            "amount": "10000",
            "asset": ASSET,
            "network": "eip155:5042002",
            "payTo": PROVIDER,
            "scheme": "exact",
        },
        "original_payment_required": "original-payment-required",
        "payment_payload_hash": "0x" + "1" * 64,
        "request_started_at": 1.0,
        "payment_required_at": 2.0,
        "payment_authorized_at": 3.0,
        "paid_response_received_at": 4.0,
        "paid_response_status": 200,
        "paid_response_body": b'{"price":"2500","token":"do-not-store"}',
        "paid_response_headers": {
            "content-type": "application/json",
            "etag": "result-v1",
        },
        "raw_payment_response": "original-payment-response",
        "settlement_response": {"success": True, "transaction": "0xabc"},
        "transaction_reference": "0xabc",
    }
    values.update(changes)
    return PaymentExchange(**values)


def _evidence(**exchange_changes) -> AssuranceEvidence:
    return AssuranceEvidence.from_exchange(_exchange(**exchange_changes), service=_service())


def test_service_without_assurance_remains_backward_compatible():
    old_json = {
        "name": "Legacy API",
        "url": "https://legacy.example.test/data",
        "price_usdc": "0.001",
        "tags": ["legacy"],
    }
    service = Service.model_validate(old_json)
    assert service.assurance is None
    assert service.name == old_json["name"]
    assert Service.model_validate_json(service.model_dump_json()).assurance is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"acceptance_criteria": []}, "at least 1"),
        ({"acceptance_criteria": ["  "]}, "empty entries"),
        ({"provider_address": "not-an-address"}, "EVM address"),
        ({"warranty_bond_address": "0x" + "0" * 40}, "zero address"),
        ({"refund_bps": 5_000}, "10000"),
        ({"max_response_time_ms": 0}, "greater than 0"),
        ({"dispute_window_seconds": 0}, "greater than 0"),
    ],
)
def test_assurance_terms_validation(changes, message):
    with pytest.raises(ValidationError, match=message):
        _terms(**changes)


def test_terms_and_evidence_hashes_are_deterministic():
    assert hash_assurance_terms(_terms()) == hash_assurance_terms(_terms())
    first = _evidence()
    second = _evidence()
    assert first.evidence_hash == second.evidence_hash
    assert first.evidence_id == first.evidence_hash

    changed = _evidence(paid_response_body=b'{"price":"2501"}')
    assert changed.evidence_hash != first.evidence_hash
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'


def test_headers_and_json_secrets_are_redacted():
    headers = sanitize_headers(
        {
            "Authorization": "Bearer secret",
            "Cookie": "session=secret",
            "X-API-Key": "secret",
            "PAYMENT-SIGNATURE": "signed-secret",
            "Content-Type": "application/json",
            "X-Request-ID": "request-1",
        },
        allowlist={
            "authorization",
            "cookie",
            "x-api-key",
            "payment-signature",
            "content-type",
            "x-request-id",
        },
    )
    assert [(item.name, item.value) for item in headers] == [
        ("content-type", "application/json"),
        ("x-request-id", "request-1"),
    ]

    evidence = _evidence(
        request_url="https://api.example.test/price?api_key=url-secret&asset=ETH",
        settlement_response={"transaction": "0xabc", "token": "settlement-secret"},
        raw_payment_response=_encoded({"token": "raw-secret"}),
    )
    assert "do-not-store" not in evidence.request_body.content
    assert "do-not-store" not in evidence.response_body.content
    assert "[REDACTED]" in evidence.request_body.content
    assert "url-secret" not in evidence.request_url
    assert "settlement-secret" not in evidence.decoded_settlement_response
    assert evidence.raw_payment_response.startswith("sha256:")


def test_response_body_limit_stores_only_full_body_hash():
    evidence = AssuranceEvidence.from_exchange(
        _exchange(paid_response_body=b"0123456789"),
        service=_service(),
        max_response_body_bytes=8,
    )
    assert evidence.response_body_max_bytes == 8
    assert evidence.response_body.original_size == 10
    assert evidence.response_body.truncated is True
    assert evidence.response_body.representation == "sha256"
    assert evidence.response_body.content is None


def test_sqlite_persistence_reload_and_idempotent_duplicate(tmp_path):
    path = tmp_path / "assurance.db"
    evidence = _evidence()
    first = AssuranceStore(path)
    assert first.put_evidence(evidence) == evidence
    assert first.put_evidence(evidence) == evidence

    reopened = AssuranceStore(path)
    assert reopened.get_evidence(evidence.evidence_id) == evidence
    assert reopened.get_evidence_by_payment(evidence.payment_id) == evidence


def test_store_allows_only_one_dispute_per_payment(tmp_path):
    store = AssuranceStore(tmp_path / "assurance.db")
    evidence = _evidence()
    store.put_evidence(evidence)
    first = Dispute(
        dispute_id="dispute_0001",
        payment_id=evidence.payment_id,
        evidence_id=evidence.evidence_id,
        evidence_hash=evidence.evidence_hash,
        reason="Returned price was stale",
        opened_at=5.0,
        updated_at=5.0,
    )
    assert store.create_dispute(first) == first
    assert store.create_dispute(first) == first
    assert store.get_dispute_by_payment(evidence.payment_id) == first

    adjudicating = first.model_copy(
        update={"status": DisputeStatus.ADJUDICATING, "updated_at": 6.0}
    )
    store.update_dispute(adjudicating)
    assert AssuranceStore(store.path).get_dispute(first.dispute_id) == adjudicating

    replay = first.model_copy(update={"dispute_id": "dispute_0002"})
    with pytest.raises(AssuranceStoreError, match="one dispute"):
        store.create_dispute(replay)


def _encoded(value: dict) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


class _ObserverUnderlying:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    async def handle_async_request(self, request):
        paid = bool(request.extensions.get(RETRY_KEY))
        self.calls.append(paid)
        if not paid:
            return httpx.Response(
                402,
                headers={"PAYMENT-REQUIRED": _encoded({"original": True})},
            )
        return httpx.Response(
            200,
            headers={
                "PAYMENT-RESPONSE": _encoded(
                    {"success": True, "transaction": "0xobserver-tx"}
                ),
                "Content-Type": "application/json",
                "Set-Cookie": "secret-cookie",
                "X-Request-ID": "response-1",
            },
            json={"price": "2500"},
        )


class _ObserverX402Client:
    def __init__(self) -> None:
        self.sign_count = 0

    async def create_payment_payload(self, _required):
        self.sign_count += 1
        return {"signature": "secret-payment-signature"}


class _ObserverHelper:
    def get_payment_required_response(self, _getter, _body):
        return {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:5042002",
                    "amount": "10000",
                    "payTo": PROVIDER,
                    "asset": ASSET,
                }
            ],
        }

    def encode_payment_signature_header(self, _payload):
        return {"PAYMENT-SIGNATURE": "secret-payment-signature"}


def _transport(observer=None):
    client = PaymentClient(
        account=MagicMock(),
        budget_usdc="1.00",
        retry_backoff=0,
        exchange_observer=observer,
    )
    underlying = _ObserverUnderlying()
    x402_client = _ObserverX402Client()
    transport = object.__new__(_BudgetEnforcingTransport)
    transport._x402_client = x402_client
    transport._http_helper = _ObserverHelper()
    transport._payment_client = client
    transport._transport = underlying
    transport._emit = client._on_event
    return client, transport, underlying, x402_client


async def test_exchange_observer_receives_sanitized_lifecycle_snapshot():
    observations = []
    client, transport, underlying, signer = _transport(observations.append)
    response = await transport.handle_async_request(
        httpx.Request(
            "POST",
            "https://api.example.test/price",
            headers={
                "Authorization": "Bearer secret",
                "Cookie": "session=secret",
                "X-API-Key": "secret",
                "Content-Type": "application/json",
                "X-Request-ID": "request-1",
            },
            content=b'{"asset":"ETH"}',
        )
    )

    assert response.status_code == 200
    assert underlying.calls == [False, True]
    assert signer.sign_count == 1
    assert len(observations) == 1
    exchange = observations[0]
    assert exchange.payment_status == PaymentStatus.SUCCESS.value
    assert exchange.selected_requirement["amount"] == "10000"
    assert exchange.settlement_response["transaction"] == "0xobserver-tx"
    assert exchange.transaction_reference == "0xobserver-tx"
    assert exchange.request_headers == {
        "content-type": "application/json",
        "x-request-id": "request-1",
    }
    assert exchange.paid_response_headers == {
        "content-length": "16",
        "content-type": "application/json",
        "x-request-id": "response-1",
    }
    assert exchange.payment_payload_hash.startswith("0x")
    assert len(exchange.payment_payload_hash) == 66
    assert "secret-payment-signature" not in exchange.payment_payload_hash
    assert exchange.request_started_at <= exchange.payment_required_at
    assert exchange.payment_required_at <= exchange.payment_authorized_at
    assert exchange.payment_authorized_at <= exchange.paid_response_received_at
    assert client.payments[0].status == PaymentStatus.SUCCESS


async def test_no_observer_path_does_not_enter_capture_hook():
    _client, transport, underlying, signer = _transport()
    transport._notify_exchange_observer = AsyncMock(
        side_effect=AssertionError("capture hook should not be entered")
    )
    response = await transport.handle_async_request(
        httpx.Request("GET", "https://api.example.test/price")
    )
    assert response.status_code == 200
    assert transport._notify_exchange_observer.await_count == 0
    assert underlying.calls == [False, True]
    assert signer.sign_count == 1


async def test_observer_failure_cannot_duplicate_paid_request():
    def failing_observer(_exchange):
        raise RuntimeError("storage unavailable")

    client, transport, underlying, signer = _transport(failing_observer)
    response = await transport.handle_async_request(
        httpx.Request("GET", "https://api.example.test/price")
    )
    assert response.status_code == 200
    assert client.payments[0].status == PaymentStatus.SUCCESS
    assert underlying.calls == [False, True]
    assert signer.sign_count == 1
