"""
Tests for interceptor.py.

These tests mock the HTTP layer with respx and the x402 SDK internals,
so no real network or wallet is needed.

Covers:
  - _parse_amount_from_402: base64 header, raw JSON header, body fallback
  - budget_guard integration: check_and_record before payment, refund on failure
  - Payment record creation and status tracking
  - _assert_open guard
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from x402.schemas.v1 import PaymentRequiredV1, PaymentRequirementsV1

from arc_agent_pay.interceptor import PaymentClient
from arc_agent_pay.models import Chain, PaymentStatus
from arc_agent_pay.exceptions import InsufficientFundsError, PaymentFailedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_mock_account(address: str = "0x" + "a" * 40) -> MagicMock:
    account = MagicMock()
    account.address = address
    return account


def make_402_header(amount_raw: str = "1000") -> str:
    """Build a base64-encoded x402 v2 X-PAYMENT-REQUIRED header value."""
    data = {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:78600",
                "maxAmountRequired": amount_raw,
                "resource": "https://api.example.com/data",
                "payTo": "0x" + "b" * 40,
                "asset": "0x" + "c" * 40,
                "maxTimeoutSeconds": 60,
            }
        ],
    }
    return base64.b64encode(json.dumps(data).encode()).decode()


def make_mock_response(status_code: int, headers: dict = None, json_body: dict = None):
    """Build a mock httpx-like response object."""
    response = MagicMock()
    response.status_code = status_code
    response.url = "https://api.example.com/data"
    response.headers = headers or {}
    if json_body is not None:
        response.json = MagicMock(return_value=json_body)
    else:
        response.json = MagicMock(side_effect=Exception("no body"))
    return response


# ---------------------------------------------------------------------------
# _parse_amount_from_402
# ---------------------------------------------------------------------------

class TestParseAmountFrom402:
    """Test the amount extraction logic — no network needed."""

    def _client(self) -> PaymentClient:
        return PaymentClient(account=make_mock_account(), budget_usdc="5.00")

    def test_base64_header_1000_raw_units(self):
        client = self._client()
        response = make_mock_response(
            402,
            headers={"X-PAYMENT-REQUIRED": make_402_header("1000")},
        )
        amount = client._parse_amount_from_402(response)
        assert Decimal(amount) == Decimal("0.001")

    def test_base64_header_1_usdc(self):
        client = self._client()
        response = make_mock_response(
            402,
            headers={"X-PAYMENT-REQUIRED": make_402_header("1000000")},
        )
        amount = client._parse_amount_from_402(response)
        assert Decimal(amount) == Decimal("1")

    def test_raw_json_header(self):
        client = self._client()
        data = {
            "x402Version": 2,
            "accepts": [{"maxAmountRequired": "500"}],
        }
        response = make_mock_response(
            402,
            headers={"X-PAYMENT-REQUIRED": json.dumps(data)},
        )
        amount = client._parse_amount_from_402(response)
        assert amount == str(Decimal("500") / Decimal("1000000"))

    def test_body_fallback_when_no_header(self):
        client = self._client()
        body = {
            "x402Version": 2,
            "accepts": [{"maxAmountRequired": "2000"}],
        }
        response = make_mock_response(402, headers={}, json_body=body)
        amount = client._parse_amount_from_402(response)
        assert amount == str(Decimal("2000") / Decimal("1000000"))

    def test_returns_none_on_missing_data(self):
        client = self._client()
        response = make_mock_response(402, headers={}, json_body={})
        amount = client._parse_amount_from_402(response)
        assert amount is None

    def test_returns_none_on_malformed_body(self):
        client = self._client()
        response = make_mock_response(402, headers={})
        # json() raises an exception
        response.json = MagicMock(side_effect=ValueError("bad json"))
        amount = client._parse_amount_from_402(response)
        assert amount is None

    def test_zero_amount(self):
        client = self._client()
        response = make_mock_response(
            402,
            headers={"X-PAYMENT-REQUIRED": make_402_header("0")},
        )
        amount = client._parse_amount_from_402(response)
        assert amount == "0"

    def test_sub_cent_amount(self):
        """$0.000001 = 1 raw unit"""
        client = self._client()
        response = make_mock_response(
            402,
            headers={"X-PAYMENT-REQUIRED": make_402_header("1")},
        )
        amount = client._parse_amount_from_402(response)
        assert Decimal(amount) == Decimal("0.000001")


class TestRequirementSelection:
    def test_v1_base_uses_legacy_network_name_and_records_selected_terms(self):
        client = PaymentClient(
            account=make_mock_account(), budget_usdc="5.00", chain=Chain.BASE
        )
        base_sepolia = PaymentRequirementsV1(
            scheme="exact",
            network="base-sepolia",
            max_amount_required="20000",
            resource="https://api.example.com/data",
            pay_to="0x" + "c" * 40,
            max_timeout_seconds=60,
            asset="0x" + "d" * 40,
        )
        base = base_sepolia.model_copy(
            update={"network": "base", "max_amount_required": "10000"}
        )
        required = PaymentRequiredV1(accepts=[base_sepolia, base])
        request = httpx.Request("GET", "https://api.example.com/data")

        record = client._payment_from_required(
            request=request,
            response=make_mock_response(402),
            payment_required=required,
            payment_id="pay_v1_base_selection_01",
            request_key="request-key",
        )

        assert record.network == "base"
        assert record.amount_atomic == "10000"
        assert record.amount_usdc == "0.01"
        assert record.pay_to == base.pay_to

    def test_v1_arc_is_rejected_before_signing(self):
        client = PaymentClient(
            account=make_mock_account(), budget_usdc="5.00", chain=Chain.ARC_TESTNET
        )
        requirement = PaymentRequirementsV1(
            scheme="exact",
            network="base",
            max_amount_required="10000",
            resource="https://api.example.com/data",
            pay_to="0x" + "c" * 40,
            max_timeout_seconds=60,
            asset="0x" + "d" * 40,
        )
        with pytest.raises(PaymentFailedError, match="v1 is not supported"):
            client._select_x402_requirement(1, [requirement])


# ---------------------------------------------------------------------------
# Budget guard integration
# ---------------------------------------------------------------------------

class TestBudgetIntegration:

    def test_budget_check_passes_affordable_amount(self):
        """Budget has 1.00 USDC, payment is 0.001 — should pass."""
        client = PaymentClient(account=make_mock_account(), budget_usdc="1.00")
        client.budget_guard.check("0.001")  # should not raise

    def test_budget_check_raises_when_over(self):
        """Budget has 0.001 USDC, payment is 0.002 — should raise."""
        client = PaymentClient(account=make_mock_account(), budget_usdc="0.001")
        with pytest.raises(InsufficientFundsError):
            client.budget_guard.check("0.002")

    def test_check_and_record_reduces_remaining(self):
        client = PaymentClient(account=make_mock_account(), budget_usdc="1.00")
        client.budget_guard.check_and_record("0.10")
        assert client.budget_guard.remaining == "0.90"

    def test_refund_restores_budget_on_failed_payment(self):
        client = PaymentClient(account=make_mock_account(), budget_usdc="1.00")
        client.budget_guard.check_and_record("0.50")
        client.budget_guard.refund("0.50")
        assert client.budget_guard.remaining == "1.00"

    def test_concurrent_check_and_record_is_atomic(self):
        """
        Simulates two payments totalling exactly the budget.
        Both should succeed; a third would fail.
        """
        client = PaymentClient(account=make_mock_account(), budget_usdc="0.20")
        client.budget_guard.check_and_record("0.10")
        client.budget_guard.check_and_record("0.10")
        assert client.budget_guard.is_exhausted is True

        with pytest.raises(InsufficientFundsError):
            client.budget_guard.check_and_record("0.001")


# ---------------------------------------------------------------------------
# assert_open guard
# ---------------------------------------------------------------------------

class TestAssertOpen:

    @pytest.mark.asyncio
    async def test_get_without_context_manager_raises(self):
        client = PaymentClient(account=make_mock_account())
        with pytest.raises(RuntimeError, match="async context manager"):
            await client.get("https://example.com")

    @pytest.mark.asyncio
    async def test_request_passes_validated_payment_id_as_transport_metadata(self):
        client = PaymentClient(account=make_mock_account())
        client._client = AsyncMock()
        client._client.request.return_value = make_mock_response(200)
        await client.post(
            "https://example.com/report",
            payment_id="order_public_request_0001",
            json={"topic": "Arc"},
        )
        _args, kwargs = client._client.request.await_args
        assert kwargs["extensions"]["arc_agent_pay.payment_id"] == "order_public_request_0001"

    @pytest.mark.asyncio
    async def test_request_rejects_invalid_payment_id_before_sending(self):
        client = PaymentClient(account=make_mock_account())
        client._client = AsyncMock()
        with pytest.raises(ValueError, match="payment_id"):
            await client.get("https://example.com", payment_id="short")
        client._client.request.assert_not_awaited()


# ---------------------------------------------------------------------------
# summary()
# ---------------------------------------------------------------------------

class TestSummary:

    def test_summary_initial_state(self):
        client = PaymentClient(account=make_mock_account(), budget_usdc="2.00")
        s = client.summary()
        assert s["budget"]["total_usdc"] == "2.00"
        assert s["payment_count"] == 0
        assert s["successful_payments"] == 0
        assert s["payments"] == []

    def test_summary_after_manual_payment_record(self):
        """Directly append payment records and verify summary reflects them."""
        from arc_agent_pay.models import Payment

        client = PaymentClient(account=make_mock_account(), budget_usdc="2.00")
        client.budget_guard.check_and_record("0.001")
        client._payments.append(
            Payment(
                service_url="https://api.example.com/data",
                amount_usdc="0.001",
                chain=Chain.ARC_TESTNET,
                status=PaymentStatus.SUCCESS,
            )
        )
        s = client.summary()
        assert s["payment_count"] == 1
        assert s["successful_payments"] == 1
        assert s["budget"]["spent_usdc"] == "0.001"

# ---------------------------------------------------------------------------
# extract_tx_hash — settlement tx hash from a paid 200 response
# ---------------------------------------------------------------------------

def _tx_header(tx: str) -> str:
    return base64.b64encode(json.dumps({"transaction": tx}).encode()).decode()


class TestExtractTxHash:
    TX = "63d989ad7e614b0913e7913a0e84d614bd1eea33075146034feec0950a214c09"

    def test_reads_payment_response_header(self):
        # x402 v2 uses the un-prefixed PAYMENT-RESPONSE header.
        headers = {"PAYMENT-RESPONSE": _tx_header(self.TX)}
        assert PaymentClient.extract_tx_hash(headers) == self.TX

    def test_reads_x_payment_response_header(self):
        # v1/compat servers use X-PAYMENT-RESPONSE.
        headers = {"X-PAYMENT-RESPONSE": _tx_header(self.TX)}
        assert PaymentClient.extract_tx_hash(headers) == self.TX

    def test_prefers_unprefixed_header(self):
        headers = {
            "PAYMENT-RESPONSE": _tx_header(self.TX),
            "X-PAYMENT-RESPONSE": _tx_header("deadbeef"),
        }
        assert PaymentClient.extract_tx_hash(headers) == self.TX

    def test_empty_when_absent(self):
        assert PaymentClient.extract_tx_hash({}) == ""

    def test_empty_when_malformed(self):
        assert PaymentClient.extract_tx_hash({"PAYMENT-RESPONSE": "not-base64-json"}) == ""
