"""
Tests for models.py and budget.py.
These run entirely in-process — no CLI, no network.
"""

import pytest
from decimal import Decimal

from arc_agent_pay.models import (
    Budget, Chain, PaymentRequest,
    Service, Wallet, WalletType,
)
from arc_agent_pay.budget import BudgetGuard
from arc_agent_pay.exceptions import InsufficientFundsError


# ---------------------------------------------------------------------------
# Wallet model
# ---------------------------------------------------------------------------

class TestWallet:
    def test_valid_address_lowercased(self):
        w = Wallet(address="0xABCDEF1234567890ABCDef1234567890abcdef12", chain=Chain.ARC_TESTNET)
        assert w.address == "0xabcdef1234567890abcdef1234567890abcdef12"

    def test_invalid_address_raises(self):
        with pytest.raises(ValueError, match="Invalid EVM address"):
            Wallet(address="not-an-address", chain=Chain.ARC_TESTNET)

    def test_defaults(self):
        w = Wallet(address="0x" + "a" * 40, chain=Chain.BASE)
        assert w.wallet_type == WalletType.AGENT
        assert w.testnet is True
        assert w.usdc_balance is None


# ---------------------------------------------------------------------------
# Service model
# ---------------------------------------------------------------------------

class TestService:
    def test_price_decimal_conversion(self):
        s = Service(name="Weather API", url="https://api.example.com/weather", price_usdc="0.001")
        assert s.price_decimal == Decimal("0.001")

    def test_defaults(self):
        s = Service(name="Test", url="https://example.com")
        assert s.price_usdc == "0.001"
        assert s.is_builtin is False
        assert s.chain == Chain.ARC_TESTNET


# ---------------------------------------------------------------------------
# PaymentRequest
# ---------------------------------------------------------------------------

class TestPaymentRequest:
    def test_amount_usdc_conversion(self):
        # 1000 raw units = 0.001 USDC (6 decimals)
        pr = PaymentRequest(
            max_amount_required="1000",
            resource="https://api.example.com/data",
            pay_to="0x" + "b" * 40,
            asset="0x" + "c" * 40,
        )
        assert pr.amount_usdc == Decimal("0.001")

    def test_one_usdc_in_raw_units(self):
        pr = PaymentRequest(
            max_amount_required="1000000",
            resource="https://api.example.com/data",
            pay_to="0x" + "b" * 40,
            asset="0x" + "c" * 40,
        )
        assert pr.amount_usdc == Decimal("1.000000")


# ---------------------------------------------------------------------------
# Budget model
# ---------------------------------------------------------------------------

class TestBudget:
    def test_remaining_calculation(self):
        b = Budget(total_usdc="1.00", spent_usdc="0.30")
        assert b.remaining == Decimal("0.70")

    def test_can_afford_true(self):
        b = Budget(total_usdc="1.00", spent_usdc="0.50")
        assert b.can_afford("0.50") is True

    def test_can_afford_exact(self):
        b = Budget(total_usdc="1.00", spent_usdc="0.50")
        assert b.can_afford("0.50") is True

    def test_can_afford_false(self):
        b = Budget(total_usdc="1.00", spent_usdc="0.50")
        assert b.can_afford("0.51") is False

    def test_is_exhausted(self):
        b = Budget(total_usdc="1.00", spent_usdc="1.00")
        assert b.is_exhausted is True

    def test_not_exhausted(self):
        b = Budget(total_usdc="1.00", spent_usdc="0.99")
        assert b.is_exhausted is False


# ---------------------------------------------------------------------------
# BudgetGuard
# ---------------------------------------------------------------------------

class TestBudgetGuard:
    def test_check_passes_when_affordable(self):
        guard = BudgetGuard("1.00")
        guard.check("0.50")  # should not raise

    def test_check_raises_when_over_budget(self):
        guard = BudgetGuard("0.10")
        with pytest.raises(InsufficientFundsError) as exc_info:
            guard.check("0.11")
        assert exc_info.value.required == "0.11"
        assert exc_info.value.remaining == "0.10"

    def test_record_reduces_remaining(self):
        guard = BudgetGuard("1.00")
        guard.record("0.30")
        assert guard.remaining == "0.70"

    def test_multiple_records_accumulate(self):
        guard = BudgetGuard("1.00")
        guard.record("0.10")
        guard.record("0.20")
        guard.record("0.30")
        assert guard.remaining == "0.40"

    def test_check_and_record_atomic(self):
        guard = BudgetGuard("0.20")
        guard.check_and_record("0.20")
        assert guard.is_exhausted is True

    def test_check_and_record_raises_if_over(self):
        guard = BudgetGuard("0.10")
        with pytest.raises(InsufficientFundsError):
            guard.check_and_record("0.11")
        # Budget must not have changed
        assert guard.remaining == "0.10"

    def test_refund_restores_budget(self):
        guard = BudgetGuard("1.00")
        guard.check_and_record("0.50")
        guard.refund("0.50")
        assert guard.remaining == "1.00"

    def test_refund_never_goes_below_zero(self):
        guard = BudgetGuard("1.00")
        guard.refund("999.00")  # refunding more than ever spent
        assert Decimal(guard.remaining) <= Decimal("1.00")

    def test_summary(self):
        guard = BudgetGuard("2.00")
        guard.record("0.75")
        s = guard.summary()
        assert s["total_usdc"] == "2.00"
        assert s["spent_usdc"] == "0.75"
        assert s["remaining_usdc"] == "1.25"
        assert s["exhausted"] is False