"""Durable lifecycle journal for x402 payments.

The store is also the serialization point for rolling policy checks.  A new
payment is checked and inserted while holding one process lock (memory backend)
or one ``BEGIN IMMEDIATE`` transaction (SQLite backend), preventing concurrent
workers from independently spending the same remaining cap.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from .exceptions import PaymentPolicyError, PaymentStoreError
from .models import Payment, PaymentStatus
from .policy import PaymentPolicy, PaymentTotals, payment_host

DAY_SECONDS = 24 * 60 * 60
HOUR_SECONDS = 60 * 60

# Ambiguous payments remain committed. Only a conclusive failure releases a
# rolling policy reservation.
COMMITTED_STATUSES = {
    PaymentStatus.PENDING,
    PaymentStatus.AUTHORIZED,
    PaymentStatus.SUCCESS,
    PaymentStatus.UNKNOWN,
}


@dataclass(frozen=True)
class PaymentReservation:
    payment: Payment
    is_new: bool


class PaymentStore(Protocol):
    """Backend contract used by :class:`PaymentClient`."""

    def reserve(self, payment: Payment, policy: PaymentPolicy) -> PaymentReservation: ...

    def update(self, payment: Payment) -> Payment: ...

    def get(self, payment_id: str) -> Payment | None: ...

    def list(self, *, limit: int = 100) -> list[Payment]: ...


def default_payment_store_path() -> str:
    configured = os.environ.get("ARC_AGENT_PAYMENTS_DB", "").strip()
    if configured:
        return configured
    return os.path.join(os.path.expanduser("~"), ".arc-agent-pay", "payments.db")


def _same_logical_payment(existing: Payment, proposed: Payment) -> bool:
    """A reused payment ID may only resume the exact same quoted request."""
    fields = (
        "request_key",
        "service_url",
        "method",
        "amount_usdc",
        "amount_atomic",
        "network",
        "asset",
        "pay_to",
        "scheme",
    )
    return all(getattr(existing, field) == getattr(proposed, field) for field in fields)


def _totals(records: list[Payment], payment: Payment, now: float) -> PaymentTotals:
    host = payment_host(payment.service_url)
    daily = Decimal("0")
    hourly_count = 0
    provider_daily = Decimal("0")
    for record in records:
        if record.status not in COMMITTED_STATUSES:
            continue
        age = now - record.created_at
        if age <= DAY_SECONDS:
            amount = Decimal(record.amount_usdc)
            daily += amount
            if payment_host(record.service_url) == host:
                provider_daily += amount
        if age <= HOUR_SECONDS:
            hourly_count += 1
    return PaymentTotals(
        daily_usdc=daily,
        hourly_count=hourly_count,
        provider_daily_usdc=provider_daily,
    )


class InMemoryPaymentStore:
    """Thread-safe process-local journal, primarily for tests and short runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, Payment] = {}

    def reserve(self, payment: Payment, policy: PaymentPolicy) -> PaymentReservation:
        if not payment.payment_id:
            raise PaymentStoreError("payment_id is required for journal reservations")
        policy.check_static(payment)
        with self._lock:
            existing = self._records.get(payment.payment_id)
            if existing is not None:
                if not _same_logical_payment(existing, payment):
                    raise PaymentPolicyError(
                        f"payment ID {payment.payment_id!r} was already used for different terms"
                    )
                if existing.status in {PaymentStatus.FAILED, PaymentStatus.SKIPPED}:
                    raise PaymentPolicyError(
                        f"payment ID {payment.payment_id!r} is conclusively closed; use a new ID"
                    )
                return PaymentReservation(existing.model_copy(deep=True), False)

            policy.check_totals(payment, _totals(list(self._records.values()), payment, time.time()))
            stored = payment.model_copy(deep=True)
            self._records[payment.payment_id] = stored
            return PaymentReservation(stored.model_copy(deep=True), True)

    def update(self, payment: Payment) -> Payment:
        if not payment.payment_id:
            raise PaymentStoreError("payment_id is required for journal updates")
        with self._lock:
            if payment.payment_id not in self._records:
                raise PaymentStoreError(f"unknown payment ID {payment.payment_id!r}")
            stored = payment.model_copy(update={"updated_at": time.time()}, deep=True)
            self._records[payment.payment_id] = stored
            return stored.model_copy(deep=True)

    def get(self, payment_id: str) -> Payment | None:
        with self._lock:
            record = self._records.get(payment_id)
            return None if record is None else record.model_copy(deep=True)

    def list(self, *, limit: int = 100) -> list[Payment]:
        with self._lock:
            records = sorted(self._records.values(), key=lambda item: item.created_at, reverse=True)
            return [record.model_copy(deep=True) for record in records[: max(0, limit)]]


class SqlitePaymentStore:
    """Cross-process durable payment journal using only stdlib SQLite."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id  TEXT PRIMARY KEY,
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL,
                    provider    TEXT NOT NULL,
                    amount_usdc TEXT NOT NULL,
                    status      TEXT NOT NULL,
                    record_json TEXT NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments (created_at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_payments_provider_created "
                "ON payments (provider, created_at)"
            )
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def _decode(raw: str) -> Payment:
        try:
            return Payment.model_validate_json(raw)
        except Exception as exc:
            raise PaymentStoreError("payment journal contains an invalid record") from exc

    def _records_for_totals(self, db: sqlite3.Connection, now: float) -> list[Payment]:
        rows = db.execute(
            "SELECT record_json FROM payments WHERE created_at >= ? AND status IN (?, ?, ?, ?)",
            (
                now - DAY_SECONDS,
                PaymentStatus.PENDING.value,
                PaymentStatus.AUTHORIZED.value,
                PaymentStatus.SUCCESS.value,
                PaymentStatus.UNKNOWN.value,
            ),
        ).fetchall()
        return [self._decode(raw) for (raw,) in rows]

    @staticmethod
    def _write(db: sqlite3.Connection, payment: Payment) -> None:
        db.execute(
            """
            INSERT INTO payments (
                payment_id, created_at, updated_at, provider,
                amount_usdc, status, record_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payment.payment_id,
                payment.created_at,
                payment.updated_at,
                payment_host(payment.service_url),
                payment.amount_usdc,
                payment.status.value,
                payment.model_dump_json(),
            ),
        )

    def reserve(self, payment: Payment, policy: PaymentPolicy) -> PaymentReservation:
        if not payment.payment_id:
            raise PaymentStoreError("payment_id is required for journal reservations")
        policy.check_static(payment)
        with self._lock, self._connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT record_json FROM payments WHERE payment_id = ?",
                    (payment.payment_id,),
                ).fetchone()
                if row is not None:
                    existing = self._decode(row[0])
                    if not _same_logical_payment(existing, payment):
                        raise PaymentPolicyError(
                            f"payment ID {payment.payment_id!r} was already used for different terms"
                        )
                    if existing.status in {PaymentStatus.FAILED, PaymentStatus.SKIPPED}:
                        raise PaymentPolicyError(
                            f"payment ID {payment.payment_id!r} is conclusively closed; use a new ID"
                        )
                    db.commit()
                    return PaymentReservation(existing, False)

                now = time.time()
                policy.check_totals(payment, _totals(self._records_for_totals(db, now), payment, now))
                self._write(db, payment)
                db.commit()
                return PaymentReservation(payment.model_copy(deep=True), True)
            except PaymentPolicyError:
                db.rollback()
                raise
            except PaymentStoreError:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                raise PaymentStoreError(f"could not reserve payment: {exc}") from exc

    def update(self, payment: Payment) -> Payment:
        if not payment.payment_id:
            raise PaymentStoreError("payment_id is required for journal updates")
        stored = payment.model_copy(update={"updated_at": time.time()}, deep=True)
        with self._lock, self._connect() as db:
            try:
                cursor = db.execute(
                    """
                    UPDATE payments SET updated_at = ?, provider = ?, amount_usdc = ?,
                        status = ?, record_json = ? WHERE payment_id = ?
                    """,
                    (
                        stored.updated_at,
                        payment_host(stored.service_url),
                        stored.amount_usdc,
                        stored.status.value,
                        stored.model_dump_json(),
                        stored.payment_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise PaymentStoreError(f"unknown payment ID {stored.payment_id!r}")
                db.commit()
                return stored
            except PaymentStoreError:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                raise PaymentStoreError(f"could not update payment: {exc}") from exc

    def get(self, payment_id: str) -> Payment | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT record_json FROM payments WHERE payment_id = ?", (payment_id,)
            ).fetchone()
            return None if row is None else self._decode(row[0])

    def list(self, *, limit: int = 100) -> list[Payment]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT record_json FROM payments ORDER BY created_at DESC LIMIT ?",
                (max(0, int(limit)),),
            ).fetchall()
            return [self._decode(raw) for (raw,) in rows]
