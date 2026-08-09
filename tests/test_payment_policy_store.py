"""PaymentPolicy and payment-journal tests; no network or wallet required."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest

from arc_agent_pay import (
    InMemoryPaymentStore,
    PaymentPolicy,
    PaymentPolicyError,
    SqlitePaymentStore,
)
from arc_agent_pay.models import Chain, Payment, PaymentStatus


def payment(
    payment_id: str,
    *,
    amount: str = "0.01",
    url: str = "https://api.example.com/data",
    network: str = "eip155:5042002",
    asset: str = "0x" + "a" * 40,
    pay_to: str = "0x" + "b" * 40,
) -> Payment:
    return Payment(
        payment_id=payment_id,
        request_key=f"GET {url}|body",
        service_url=url,
        amount_usdc=amount,
        amount_atomic=str(int(Decimal(amount) * 1_000_000)),
        chain=Chain.ARC_TESTNET,
        network=network,
        asset=asset,
        pay_to=pay_to,
    )


def test_static_policy_checks_the_complete_quote():
    policy = PaymentPolicy(
        max_payment_usdc="0.02",
        allowed_hosts={"api.example.com"},
        allowed_networks={"eip155:5042002"},
        allowed_assets={"0x" + "a" * 40},
        allowed_pay_to={"0x" + "b" * 40},
    )
    policy.check_static(payment("pay_static_quote_0001"))

    with pytest.raises(PaymentPolicyError, match="per-payment maximum"):
        policy.check_static(payment("pay_static_quote_0002", amount="0.03"))
    with pytest.raises(PaymentPolicyError, match="not allowlisted"):
        policy.check_static(
            payment("pay_static_quote_0003", url="https://untrusted.example/data")
        )


def test_daily_cap_is_prospective_not_threshold_only():
    store = InMemoryPaymentStore()
    policy = PaymentPolicy(daily_cap_usdc="0.10")
    store.reserve(payment("pay_daily_cap_000001", amount="0.09"), policy)

    with pytest.raises(PaymentPolicyError, match=r"0.09 committed \+ 0.02 requested"):
        store.reserve(payment("pay_daily_cap_000002", amount="0.02"), policy)


def test_failed_payment_releases_reservation_but_unknown_does_not():
    store = InMemoryPaymentStore()
    policy = PaymentPolicy(daily_cap_usdc="0.01")
    first = store.reserve(payment("pay_release_cap_0001"), policy).payment
    first.status = PaymentStatus.FAILED
    store.update(first)
    second = store.reserve(payment("pay_release_cap_0002"), policy).payment
    second.status = PaymentStatus.UNKNOWN
    store.update(second)

    with pytest.raises(PaymentPolicyError, match="daily cap"):
        store.reserve(payment("pay_release_cap_0003"), policy)


def test_provider_cap_groups_paths_by_hostname():
    store = InMemoryPaymentStore()
    policy = PaymentPolicy(provider_daily_cap_usdc="0.015")
    store.reserve(
        payment("pay_provider_cap_001", amount="0.01", url="https://api.example.com/a"),
        policy,
    )
    with pytest.raises(PaymentPolicyError, match="provider daily cap"):
        store.reserve(
            payment("pay_provider_cap_002", amount="0.01", url="https://api.example.com/b"),
            policy,
        )

    # A different provider still has its own allowance.
    store.reserve(
        payment("pay_provider_cap_003", amount="0.01", url="https://other.example/b"),
        policy,
    )


def test_sqlite_store_persists_and_resumes_same_logical_payment(tmp_path: Path):
    path = str(tmp_path / "payments.db")
    policy = PaymentPolicy(daily_cap_usdc="1")
    original = payment("pay_durable_resume_001")
    assert SqlitePaymentStore(path).reserve(original, policy).is_new is True

    reopened = SqlitePaymentStore(path)
    resumed = reopened.reserve(original.model_copy(deep=True), policy)
    assert resumed.is_new is False
    assert reopened.get(original.payment_id).service_url == original.service_url

    changed = original.model_copy(update={"amount_usdc": "0.02"})
    with pytest.raises(PaymentPolicyError, match="different terms"):
        reopened.reserve(changed, policy)


def test_failed_payment_id_cannot_be_reopened(tmp_path: Path):
    store = SqlitePaymentStore(str(tmp_path / "payments.db"))
    policy = PaymentPolicy()
    closed = store.reserve(payment("pay_closed_payment_01"), policy).payment
    closed.status = PaymentStatus.FAILED
    store.update(closed)
    with pytest.raises(PaymentPolicyError, match="conclusively closed"):
        store.reserve(payment("pay_closed_payment_01"), policy)


def test_sqlite_cap_reservation_is_atomic_across_store_instances(tmp_path: Path):
    path = str(tmp_path / "payments.db")
    SqlitePaymentStore(path)  # initialize before the workers race
    policy = PaymentPolicy(daily_cap_usdc="0.01")
    barrier = Barrier(2)

    def reserve(index: int) -> str:
        store = SqlitePaymentStore(path)
        barrier.wait()
        try:
            store.reserve(payment(f"pay_concurrent_{index:04d}"), policy)
            return "accepted"
        except PaymentPolicyError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (1, 2)))
    assert sorted(results) == ["accepted", "blocked"]
