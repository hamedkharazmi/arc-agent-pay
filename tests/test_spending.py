"""Tests for cross-run spending caps: ledgers, SpendCaps policy, gate + agent wiring."""

import time
from decimal import Decimal

import pytest

from arc_agent_pay.models import Service
from arc_agent_pay.spending import (
    DAY_SECONDS,
    HOUR_SECONDS,
    InMemorySpendLedger,
    SpendCaps,
    SqliteSpendLedger,
    counterparty_key,
)
from arc_agent_pay.agent import ResearchAgent
from arc_agent_pay.agent.trust import ReputationGate

NOW = time.time()


def svc(url: str = "https://api.example.com/prices", provider_agent_id=None) -> Service:
    return Service(name="svc", url=url, provider_agent_id=provider_agent_id)


# ---------------------------------------------------------------------------
# counterparty_key
# ---------------------------------------------------------------------------

def test_counterparty_key_uses_host():
    assert counterparty_key("https://API.Example.com/prices?x=1") == "api.example.com"


def test_counterparty_key_variants_collapse():
    a = counterparty_key("https://api.example.com/prices")
    b = counterparty_key("https://api.example.com/news?category=tech")
    assert a == b


def test_counterparty_key_plain_name_passthrough():
    assert counterparty_key("Whale Feed") == "whale feed"


# ---------------------------------------------------------------------------
# Ledgers (same contract for both implementations)
# ---------------------------------------------------------------------------

@pytest.fixture(params=["memory", "sqlite"])
def ledger(request, tmp_path):
    if request.param == "memory":
        return InMemorySpendLedger()
    return SqliteSpendLedger(str(tmp_path / "spend.db"))


def test_ledger_totals_and_counts_in_window(ledger):
    ledger.record("https://a.example.com/x", "0.010", at=NOW - 100)
    ledger.record("https://a.example.com/y", "0.005", at=NOW - 50)
    ledger.record("https://b.example.com/z", "0.020", at=NOW - 50)

    assert ledger.total_since(NOW - DAY_SECONDS) == Decimal("0.035")
    assert ledger.count_since(NOW - DAY_SECONDS) == 3
    assert ledger.total_for_since("https://a.example.com/other", NOW - DAY_SECONDS) == Decimal(
        "0.015"
    )


def test_ledger_window_excludes_old_rows(ledger):
    ledger.record("https://a.example.com/x", "1.0", at=NOW - DAY_SECONDS - 60)
    ledger.record("https://a.example.com/x", "0.01", at=NOW - 60)

    assert ledger.total_since(NOW - DAY_SECONDS) == Decimal("0.01")
    assert ledger.count_since(NOW - DAY_SECONDS) == 1


def test_sqlite_ledger_persists_across_instances(tmp_path):
    path = str(tmp_path / "spend.db")
    SqliteSpendLedger(path).record("https://a.example.com/x", "0.010", at=NOW - 10)

    reopened = SqliteSpendLedger(path)
    assert reopened.total_since(NOW - DAY_SECONDS) == Decimal("0.010")


def test_sqlite_ledger_prunes_ancient_rows(tmp_path):
    path = str(tmp_path / "spend.db")
    ledger = SqliteSpendLedger(path)
    ledger.record("https://a.example.com/x", "1.0", at=NOW - 30 * DAY_SECONDS)
    ledger.record("https://a.example.com/x", "0.01", at=NOW)  # triggers pruning

    # The 30-day-old row is beyond retention and gone even for huge windows.
    assert ledger.total_since(NOW - 365 * DAY_SECONDS) == Decimal("0.01")


def test_ledger_rejects_bad_amounts(ledger):
    with pytest.raises(Exception):
        ledger.record("https://a.example.com/x", "not-a-number")


# ---------------------------------------------------------------------------
# SpendCaps policy
# ---------------------------------------------------------------------------

def test_caps_inactive_allows_everything():
    caps = SpendCaps()
    assert not caps.active
    assert caps.check(InMemorySpendLedger(), "https://a.example.com") is None


def test_daily_cap_blocks_when_reached():
    ledger = InMemorySpendLedger()
    ledger.record("https://a.example.com", "0.06", at=NOW - 10)
    caps = SpendCaps(daily_cap_usdc="0.05")

    reason = caps.check(ledger, "https://b.example.com", now=NOW)
    assert reason is not None and "daily cap" in reason


def test_daily_cap_allows_under_cap():
    ledger = InMemorySpendLedger()
    ledger.record("https://a.example.com", "0.01", at=NOW - 10)
    caps = SpendCaps(daily_cap_usdc="0.05")

    assert caps.check(ledger, "https://a.example.com", now=NOW) is None


def test_daily_cap_window_rolls():
    ledger = InMemorySpendLedger()
    ledger.record("https://a.example.com", "0.06", at=NOW - DAY_SECONDS - 60)
    caps = SpendCaps(daily_cap_usdc="0.05")

    assert caps.check(ledger, "https://a.example.com", now=NOW) is None


def test_velocity_cap_blocks_when_reached():
    ledger = InMemorySpendLedger()
    for i in range(3):
        ledger.record("https://a.example.com", "0.001", at=NOW - 10 - i)
    caps = SpendCaps(max_payments_per_hour=3)

    reason = caps.check(ledger, "https://a.example.com", now=NOW)
    assert reason is not None and "velocity" in reason


def test_velocity_cap_ignores_old_payments():
    ledger = InMemorySpendLedger()
    for i in range(3):
        ledger.record("https://a.example.com", "0.001", at=NOW - HOUR_SECONDS - 60 - i)
    caps = SpendCaps(max_payments_per_hour=3)

    assert caps.check(ledger, "https://a.example.com", now=NOW) is None


def test_provider_cap_blocks_only_that_provider():
    ledger = InMemorySpendLedger()
    ledger.record("https://greedy.example.com/data", "0.03", at=NOW - 10)
    caps = SpendCaps(provider_daily_cap_usdc="0.02")

    blocked = caps.check(ledger, "https://greedy.example.com/other", now=NOW)
    assert blocked is not None and "counterparty daily cap" in blocked
    assert caps.check(ledger, "https://other.example.com/data", now=NOW) is None


def test_caps_fail_open_on_ledger_error():
    class BrokenLedger:
        def total_since(self, since):
            raise RuntimeError("db on fire")

    caps = SpendCaps(daily_cap_usdc="0.05")
    assert caps.check(BrokenLedger(), "https://a.example.com", now=NOW) is None


# ---------------------------------------------------------------------------
# Gate integration
# ---------------------------------------------------------------------------

def test_gate_with_caps_only_is_active():
    gate = ReputationGate(
        caps=SpendCaps(daily_cap_usdc="0.05"), spend_ledger=InMemorySpendLedger()
    )
    assert gate.active


def test_gate_blocks_capped_payment_even_without_provider_identity():
    ledger = InMemorySpendLedger()
    ledger.record("https://a.example.com", "0.06", at=NOW - 10)
    gate = ReputationGate(caps=SpendCaps(daily_cap_usdc="0.05"), spend_ledger=ledger)

    decision = gate.evaluate(svc(provider_agent_id=None))
    assert not decision.allowed
    assert "daily cap" in decision.reason


def test_gate_allows_under_caps():
    gate = ReputationGate(
        caps=SpendCaps(daily_cap_usdc="0.05"), spend_ledger=InMemorySpendLedger()
    )
    assert gate.evaluate(svc()).allowed


def test_gate_caps_dropped_without_ledger():
    gate = ReputationGate(caps=SpendCaps(daily_cap_usdc="0.05"), spend_ledger=None)
    assert gate.caps is None
    assert not gate.active  # no other policy set


def test_gate_denylist_still_beats_caps():
    gate = ReputationGate(
        denylist={7},
        caps=SpendCaps(daily_cap_usdc="100"),
        spend_ledger=InMemorySpendLedger(),
    )
    decision = gate.evaluate(svc(provider_agent_id=7))
    assert not decision.allowed
    assert "denylisted" in decision.reason


# ---------------------------------------------------------------------------
# ResearchAgent wiring
# ---------------------------------------------------------------------------

def test_agent_builds_active_gate_from_cap_kwargs():
    agent = ResearchAgent(
        payment_signer=object(),
        daily_cap_usdc="0.50",
        spend_ledger=InMemorySpendLedger(),
    )
    assert agent._gate is not None and agent._gate.active
    assert agent._gate.caps is not None


def test_agent_without_caps_has_no_ledger_or_gate():
    agent = ResearchAgent(payment_signer=object())
    assert agent._spend_ledger is None
    assert agent._gate is None


def test_agent_records_settled_payments_into_ledger():
    ledger = InMemorySpendLedger()
    agent = ResearchAgent(
        payment_signer=object(),
        daily_cap_usdc="0.50",
        spend_ledger=ledger,
    )
    seen = []
    wrapped = agent._wrap_on_event(lambda e, p: seen.append((e, p)))

    wrapped("payment_settled", {
        "service": "https://api.example.com/prices?x=1",
        "amount_usdc": "0.001",
        "tx_hash": "0xabc",
    })
    wrapped("fetch_completed", {"service": "svc", "status": 200})

    assert ledger.total_since(time.time() - 60) == Decimal("0.001")
    assert ledger.count_since(time.time() - 60) == 1
    # Both events forwarded to the caller untouched.
    assert [e for e, _ in seen] == ["payment_settled", "fetch_completed"]


def test_agent_wrap_is_passthrough_without_ledger():
    agent = ResearchAgent(payment_signer=object())
    cb = lambda e, p: None  # noqa: E731
    assert agent._wrap_on_event(cb) is cb


def test_agent_recording_failure_does_not_break_events():
    class BrokenLedger(InMemorySpendLedger):
        def record(self, *a, **k):
            raise RuntimeError("disk full")

    agent = ResearchAgent(
        payment_signer=object(),
        daily_cap_usdc="0.50",
        spend_ledger=BrokenLedger(),
    )
    seen = []
    wrapped = agent._wrap_on_event(lambda e, p: seen.append(e))
    wrapped("payment_settled", {"service": "https://a.example.com", "amount_usdc": "0.001"})
    assert seen == ["payment_settled"]
