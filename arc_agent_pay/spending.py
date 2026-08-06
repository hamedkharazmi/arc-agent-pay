"""
spending.py — cross-run spending caps for agent payments.

BudgetGuard bounds a single session; this module bounds an agent's spending
*across* sessions: a rolling daily cap, a payment-velocity cap, and a
per-counterparty daily cap. Together they answer "what if my agent runs in a
loop / gets restarted / hammers one provider?" — the caps hold no matter how
many runs are involved.

Two pieces:

  * SpendLedger — an append-only record of settled payments. InMemorySpendLedger
    for tests/single-process use; SqliteSpendLedger (stdlib sqlite3, no extra
    dependency) for persistence across runs.
  * SpendCaps — the policy. `check()` consults a ledger and returns a refusal
    reason, or None to allow.

Enforcement lives in ReputationGate (agent/trust.py), which already hosts the
rest of the spending policy; recording is wired by ResearchAgent, which folds
every `payment_settled` event into the ledger. Both caps and recording are
best-effort and fail-open: a broken ledger never blocks a run, it only logs.

Caps are threshold checks: a payment is refused once the window total has
*reached* the cap, so the final payment before refusal may overshoot the cap
by at most one service price. This keeps the check independent of per-service
pricing (which the 402 response, not the registry, ultimately decides).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DAY_SECONDS = 24 * 60 * 60
HOUR_SECONDS = 60 * 60

# Ledger rows older than this are pruned opportunistically on record().
_RETENTION_SECONDS = 7 * DAY_SECONDS


def counterparty_key(url_or_name: str) -> str:
    """Stable key identifying a provider: the URL host, else the raw string.

    The same key must come out at gate time (from Service.url) and at record
    time (from the payment event's service URL) — host does that; path/query
    variants of one provider collapse together.
    """
    raw = (url_or_name or "").strip()
    host = urlparse(raw).netloc
    return (host or raw).lower()


# ---------------------------------------------------------------------------
# Ledgers
# ---------------------------------------------------------------------------

class InMemorySpendLedger:
    """Process-local ledger — resets on restart. Useful for tests and for
    velocity caps inside one long-lived process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[tuple[float, str, Decimal]] = []  # (ts, counterparty, amount)

    def record(
        self,
        counterparty: str,
        amount_usdc: str,
        *,
        tx_reference: Optional[str] = None,
        at: Optional[float] = None,
    ) -> None:
        ts = time.time() if at is None else float(at)
        amount = Decimal(amount_usdc)
        with self._lock:
            self._rows.append((ts, counterparty_key(counterparty), amount))

    def total_since(self, since: float) -> Decimal:
        with self._lock:
            return sum((a for ts, _c, a in self._rows if ts >= since), Decimal("0"))

    def count_since(self, since: float) -> int:
        with self._lock:
            return sum(1 for ts, _c, _a in self._rows if ts >= since)

    def total_for_since(self, counterparty: str, since: float) -> Decimal:
        key = counterparty_key(counterparty)
        with self._lock:
            return sum(
                (a for ts, c, a in self._rows if ts >= since and c == key), Decimal("0")
            )


class SqliteSpendLedger:
    """Durable ledger on stdlib sqlite3 — survives restarts, needs no server.

    A connection is opened per operation: individual ops are tiny and rare
    (one write per payment), and this keeps the ledger safe to share across
    threads and event-loop callbacks without a connection pool.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS spends (
                    ts           REAL NOT NULL,
                    counterparty TEXT NOT NULL,
                    amount_usdc  TEXT NOT NULL,
                    tx_reference TEXT
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_spends_ts ON spends (ts)")
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5)

    def record(
        self,
        counterparty: str,
        amount_usdc: str,
        *,
        tx_reference: Optional[str] = None,
        at: Optional[float] = None,
    ) -> None:
        ts = time.time() if at is None else float(at)
        Decimal(amount_usdc)  # validate before writing
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO spends (ts, counterparty, amount_usdc, tx_reference)"
                " VALUES (?, ?, ?, ?)",
                (ts, counterparty_key(counterparty), amount_usdc, tx_reference),
            )
            db.execute("DELETE FROM spends WHERE ts < ?", (ts - _RETENTION_SECONDS,))
            db.commit()

    def _sum(self, where: str, params: tuple) -> Decimal:
        with self._lock, self._connect() as db:
            rows = db.execute(f"SELECT amount_usdc FROM spends WHERE {where}", params)
            total = Decimal("0")
            for (amount,) in rows:
                try:
                    total += Decimal(amount)
                except InvalidOperation:  # pragma: no cover - guarded on write
                    logger.warning("spend ledger: unparseable amount %r ignored", amount)
            return total

    def total_since(self, since: float) -> Decimal:
        return self._sum("ts >= ?", (since,))

    def count_since(self, since: float) -> int:
        with self._lock, self._connect() as db:
            (n,) = db.execute(
                "SELECT COUNT(*) FROM spends WHERE ts >= ?", (since,)
            ).fetchone()
            return int(n)

    def total_for_since(self, counterparty: str, since: float) -> Decimal:
        return self._sum(
            "ts >= ? AND counterparty = ?", (since, counterparty_key(counterparty))
        )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

class SpendCaps:
    """Cross-run spending limits, checked against a SpendLedger.

    Args:
        daily_cap_usdc: max total spend over a rolling 24 h window.
        max_payments_per_hour: max number of payments over a rolling 1 h
            window (velocity brake for runaway loops).
        provider_daily_cap_usdc: max spend to any single counterparty over a
            rolling 24 h window.

    All default to None (off). `check()` returns a human-readable refusal
    reason, or None to allow.
    """

    def __init__(
        self,
        *,
        daily_cap_usdc: Optional[str] = None,
        max_payments_per_hour: Optional[int] = None,
        provider_daily_cap_usdc: Optional[str] = None,
    ) -> None:
        self.daily_cap = None if daily_cap_usdc is None else Decimal(daily_cap_usdc)
        self.max_payments_per_hour = (
            None if max_payments_per_hour is None else int(max_payments_per_hour)
        )
        self.provider_daily_cap = (
            None if provider_daily_cap_usdc is None else Decimal(provider_daily_cap_usdc)
        )

    @property
    def active(self) -> bool:
        return (
            self.daily_cap is not None
            or self.max_payments_per_hour is not None
            or self.provider_daily_cap is not None
        )

    def check(
        self,
        ledger,
        counterparty: str,
        *,
        now: Optional[float] = None,
    ) -> Optional[str]:
        """Return a refusal reason if any cap has been reached, else None.

        Fail-open: a ledger error logs a warning and allows the payment — a
        broken ledger must not take the agent down. (BudgetGuard still bounds
        the session either way.)
        """
        if not self.active:
            return None
        ts = time.time() if now is None else float(now)
        try:
            if self.daily_cap is not None:
                spent = ledger.total_since(ts - DAY_SECONDS)
                if spent >= self.daily_cap:
                    return (
                        f"daily cap {self.daily_cap} USDC reached"
                        f" (spent {spent} in the last 24h)"
                    )
            if self.max_payments_per_hour is not None:
                count = ledger.count_since(ts - HOUR_SECONDS)
                if count >= self.max_payments_per_hour:
                    return (
                        f"velocity cap {self.max_payments_per_hour} payments/hour"
                        f" reached ({count} in the last hour)"
                    )
            if self.provider_daily_cap is not None:
                spent = ledger.total_for_since(counterparty, ts - DAY_SECONDS)
                if spent >= self.provider_daily_cap:
                    return (
                        f"counterparty daily cap {self.provider_daily_cap} USDC"
                        f" reached for {counterparty_key(counterparty)}"
                        f" (spent {spent} in the last 24h)"
                    )
        except Exception as e:  # noqa: BLE001 - caps are best-effort
            logger.warning("spend cap check failed (allowing payment): %s", e)
        return None


def default_ledger_path() -> str:
    """Default location for the durable ledger: $ARC_AGENT_PAY_SPEND_DB, else
    ~/.arc-agent-pay/spend.db."""
    env = os.environ.get("ARC_AGENT_PAY_SPEND_DB", "").strip()
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".arc-agent-pay", "spend.db")
