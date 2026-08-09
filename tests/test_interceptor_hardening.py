"""
Tests for the production-hardening of the payment transport:

  - correct payment attribution under concurrent in-flight payments (no pending[-1])
  - transient-error retries (connection errors + 5xx) on the pre-payment request
  - opt-in idempotency dedup cache (replay a paid response without paying again)

The x402 signing internals and the network are faked, so no chain/wallet/x402
SDK calls happen.
"""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import MagicMock

import httpx
import pytest

from arc_agent_pay import PaymentPolicy, PaymentPolicyError
from arc_agent_pay.exceptions import PaymentTimeoutError
from arc_agent_pay.interceptor import PaymentClient, _BudgetEnforcingTransport
from arc_agent_pay.models import PaymentStatus

RETRY_KEY = _BudgetEnforcingTransport._RETRY_KEY


def _402_header(raw_amount: str) -> str:
    data = {"x402Version": 2, "accepts": [{"scheme": "exact", "maxAmountRequired": raw_amount}]}
    return base64.b64encode(json.dumps(data).encode()).decode()


def _tx_header(tx: str) -> str:
    return base64.b64encode(json.dumps({"transaction": tx}).encode()).decode()


class FakeUnderlying:
    """Returns 402 for the initial request and 200 for the paid retry."""

    def __init__(self, amount_by_path: dict[str, str]):
        self.amount_by_path = amount_by_path
        self.calls: list[tuple[str, bool]] = []

    async def handle_async_request(self, request):
        is_retry = bool(request.extensions.get(RETRY_KEY))
        path = request.url.path
        self.calls.append((path, is_retry))
        if is_retry:
            return httpx.Response(
                200,
                headers={"PAYMENT-RESPONSE": _tx_header("0xtx" + path.strip("/"))},
                json={"ok": path},
            )
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": _402_header(self.amount_by_path[path])})


class _FakeX402Client:
    async def create_payment_payload(self, _req):
        await asyncio.sleep(0)  # force a scheduling yield for real interleaving
        return {"payload": True}


class _FakeHelper:
    def get_payment_required_response(self, _getter, _body):
        return {"req": True}

    def encode_payment_signature_header(self, _payload):
        return {"X-PAYMENT": "sig"}


def _build_transport(client: PaymentClient, underlying) -> _BudgetEnforcingTransport:
    """Construct the transport with faked x402 internals (skips __init__'s imports)."""
    t = object.__new__(_BudgetEnforcingTransport)
    t._x402_client = _FakeX402Client()
    t._http_helper = _FakeHelper()
    t._payment_client = client
    t._transport = underlying
    t._emit = client._on_event
    return t


def _client(**kwargs) -> PaymentClient:
    return PaymentClient(account=MagicMock(), budget_usdc="1.00", retry_backoff=0.0, **kwargs)


# ---------------------------------------------------------------------------
# Item 2: concurrent attribution
# ---------------------------------------------------------------------------

async def test_concurrent_payments_attributed_correctly():
    client = _client()
    t = _build_transport(client, FakeUnderlying({"/a": "10000", "/b": "5000"}))

    reqs = [httpx.Request("GET", "https://x/a"), httpx.Request("GET", "https://x/b")]
    await asyncio.gather(*(t.handle_async_request(r) for r in reqs))

    by_url = {p.service_url: p for p in client.payments}
    assert by_url["https://x/a"].amount_usdc == "0.01"
    assert by_url["https://x/a"].status == PaymentStatus.SUCCESS
    assert by_url["https://x/a"].tx_reference == "0xtxa"
    assert by_url["https://x/b"].amount_usdc == "0.005"
    assert by_url["https://x/b"].tx_reference == "0xtxb"


# ---------------------------------------------------------------------------
# Item 3: transient retries
# ---------------------------------------------------------------------------

class _FlakyUnderlying:
    def __init__(self, fails, mode):
        self.fails = fails  # number of failures before success
        self.mode = mode    # "5xx" or "exc"
        self.count = 0

    async def handle_async_request(self, request):
        self.count += 1
        if self.count <= self.fails:
            if self.mode == "exc":
                raise httpx.ConnectError("boom")
            return httpx.Response(503, json={"err": True})
        return httpx.Response(200, json={"ok": True})


async def test_transient_retry_on_5xx_then_success():
    client = _client()  # max_retries=2 → 3 attempts
    t = _build_transport(client, _FlakyUnderlying(fails=2, mode="5xx"))
    resp = await t._send_with_transient_retries(httpx.Request("GET", "https://x/data"))
    assert resp.status_code == 200
    assert t._transport.count == 3


async def test_transient_retry_on_connection_error_then_success():
    client = _client()
    t = _build_transport(client, _FlakyUnderlying(fails=1, mode="exc"))
    resp = await t._send_with_transient_retries(httpx.Request("GET", "https://x/data"))
    assert resp.status_code == 200
    assert t._transport.count == 2


async def test_transient_retry_gives_up_and_raises():
    client = _client(max_retries=1)  # 2 attempts, both fail
    t = _build_transport(client, _FlakyUnderlying(fails=5, mode="exc"))
    with pytest.raises(httpx.TransportError):
        await t._send_with_transient_retries(httpx.Request("GET", "https://x/data"))
    assert t._transport.count == 2


# ---------------------------------------------------------------------------
# Item 1: idempotency cache
# ---------------------------------------------------------------------------

def test_idem_put_get_roundtrip_and_ttl():
    client = _client(idempotency_ttl=60)
    client._idem_put("sig", 200, httpx.Headers({"a": "b"}), b'{"x":1}')
    cached = client._idem_get("sig")
    assert cached is not None and cached.status_code == 200 and cached.json() == {"x": 1}

    # force expiry by pushing the stored deadline into the past
    expiry, status, headers, content = client._idem_cache["sig"]
    client._idem_cache["sig"] = (expiry - 10_000, status, headers, content)
    assert client._idem_get("sig") is None
    assert "sig" not in client._idem_cache  # expired entries are evicted


async def test_idempotent_request_not_paid_twice():
    client = _client(idempotency_ttl=60)
    t = _build_transport(client, FakeUnderlying({"/a": "10000"}))

    r1 = await t.handle_async_request(httpx.Request("GET", "https://x/a"))
    r2 = await t.handle_async_request(httpx.Request("GET", "https://x/a"))

    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json() == {"ok": "/a"}
    # Paid exactly once; second call served from the dedup cache.
    assert len(client.payments) == 1
    assert sum(1 for _p, is_retry in t._transport.calls if is_retry) == 1


# ---------------------------------------------------------------------------
# v0.4 lifecycle hardening
# ---------------------------------------------------------------------------

class _PaidStatusUnderlying(FakeUnderlying):
    def __init__(self, paid_status: int):
        super().__init__({"/a": "10000"})
        self.paid_status = paid_status

    async def handle_async_request(self, request):
        is_retry = bool(request.extensions.get(RETRY_KEY))
        self.calls.append((request.url.path, is_retry))
        if is_retry:
            return httpx.Response(self.paid_status, json={"status": self.paid_status})
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": _402_header("10000")})


async def test_every_2xx_response_is_a_success():
    client = _client()
    transport = _build_transport(client, _PaidStatusUnderlying(201))
    response = await transport.handle_async_request(httpx.Request("POST", "https://x/a"))
    assert response.status_code == 201
    assert client.payments[0].status == PaymentStatus.SUCCESS
    assert client.payments[0].response_status == 201


async def test_core_payment_client_enforces_policy_before_signing():
    client = _client(policy=PaymentPolicy(max_payment_usdc="0.005"))
    underlying = FakeUnderlying({"/a": "10000"})
    transport = _build_transport(client, underlying)
    with pytest.raises(PaymentPolicyError, match="per-payment maximum"):
        await transport.handle_async_request(httpx.Request("GET", "https://x/a"))
    assert all(not is_retry for _path, is_retry in underlying.calls)
    assert client.budget_guard.remaining == "1.00"


async def test_paid_5xx_is_unknown_and_keeps_budget_reserved():
    client = _client()
    transport = _build_transport(client, _PaidStatusUnderlying(503))
    response = await transport.handle_async_request(httpx.Request("GET", "https://x/a"))
    assert response.status_code == 503
    assert client.payments[0].status == PaymentStatus.UNKNOWN
    assert client.budget_guard.remaining == "0.99"


async def test_generic_paid_4xx_is_unknown_without_settlement_proof():
    client = _client()
    transport = _build_transport(client, _PaidStatusUnderlying(422))
    await transport.handle_async_request(httpx.Request("GET", "https://x/a"))
    assert client.payments[0].status == PaymentStatus.UNKNOWN
    assert client.budget_guard.remaining == "0.99"


class _RejectedPaymentUnderlying(FakeUnderlying):
    async def handle_async_request(self, request):
        if request.extensions.get(RETRY_KEY):
            header = base64.b64encode(json.dumps({"success": False}).encode()).decode()
            return httpx.Response(402, headers={"PAYMENT-RESPONSE": header})
        return await super().handle_async_request(request)


async def test_explicit_settlement_rejection_releases_budget():
    client = _client()
    transport = _build_transport(client, _RejectedPaymentUnderlying({"/a": "10000"}))
    await transport.handle_async_request(httpx.Request("GET", "https://x/a"))
    assert client.payments[0].status == PaymentStatus.FAILED
    assert client.budget_guard.remaining == "1.00"


class _PaidTimeoutUnderlying(FakeUnderlying):
    async def handle_async_request(self, request):
        if request.extensions.get(RETRY_KEY):
            raise httpx.ReadTimeout("response lost")
        return await super().handle_async_request(request)


async def test_paid_transport_failure_is_unknown_and_exposes_resume_id():
    client = _client()
    transport = _build_transport(client, _PaidTimeoutUnderlying({"/a": "10000"}))
    with pytest.raises(PaymentTimeoutError, match="reuse this payment ID") as exc_info:
        await transport.handle_async_request(httpx.Request("GET", "https://x/a"))
    record = client.payments[0]
    assert exc_info.value.payment_id == record.payment_id
    assert record.status == PaymentStatus.UNKNOWN
    assert record.payment_id
    assert client.payment_store.get(record.payment_id).status == PaymentStatus.UNKNOWN


class _PaymentIdX402Client:
    def __init__(self):
        self.extensions = None

    async def create_payment_payload(self, _required, resource=None, extensions=None):
        self.extensions = extensions
        return {"payload": True}


class _PaymentIdHelper(_FakeHelper):
    def get_payment_required_response(self, _getter, _body):
        return {
            "x402Version": 2,
            "accepts": [{
                "scheme": "exact",
                "network": "eip155:5042002",
                "asset": "0x" + "a" * 40,
                "amount": "10000",
                "payTo": "0x" + "b" * 40,
            }],
            "extensions": {
                "payment-identifier": {
                    "info": {"required": False},
                    "schema": {"type": "object"},
                }
            },
        }


async def test_explicit_payment_id_is_added_to_standard_x402_extension():
    payment_id = "order_customer_request_0001"
    client = _client()
    transport = _build_transport(client, FakeUnderlying({"/a": "10000"}))
    x402_client = _PaymentIdX402Client()
    transport._x402_client = x402_client
    transport._http_helper = _PaymentIdHelper()
    request = httpx.Request(
        "GET",
        "https://x/a",
        extensions={_BudgetEnforcingTransport._PAYMENT_ID_KEY: payment_id},
    )

    await transport.handle_async_request(request)

    info = x402_client.extensions["payment-identifier"]["info"]
    assert info["id"] == payment_id
    assert client.payments[0].payment_id == payment_id


async def test_payment_id_resume_refuses_seller_without_idempotency_extension():
    payment_id = "order_non_idempotent_0001"
    client = _client()
    underlying = FakeUnderlying({"/a": "10000"})
    transport = _build_transport(client, underlying)

    def request():
        return httpx.Request(
            "GET",
            "https://x/a",
            extensions={_BudgetEnforcingTransport._PAYMENT_ID_KEY: payment_id},
        )

    await transport.handle_async_request(request())
    with pytest.raises(PaymentPolicyError, match="cannot be resumed safely"):
        await transport.handle_async_request(request())
    assert sum(1 for _path, is_retry in underlying.calls if is_retry) == 1
