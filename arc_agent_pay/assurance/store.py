"""Separate SQLite persistence for Assurance evidence and dispute state."""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import TYPE_CHECKING, Optional

from .models import AssuranceEvidence, Dispute, DisputeStatus, canonical_json

if TYPE_CHECKING:
    from .settlement import SettlementRecord

SCHEMA_VERSION = 2


class AssuranceStoreError(RuntimeError):
    """The Assurance journal is unavailable, inconsistent, or conflicts with a replay."""


def _encode_evidence(evidence: AssuranceEvidence) -> str:
    return canonical_json(evidence.model_dump(mode="json"))


def _decode_evidence(raw: str) -> AssuranceEvidence:
    try:
        return AssuranceEvidence.model_validate_json(raw)
    except Exception as exc:
        raise AssuranceStoreError("Assurance journal contains invalid evidence") from exc


def _encode_dispute(dispute: Dispute) -> str:
    return canonical_json(dispute.model_dump(mode="json"))


def _decode_dispute(raw: str) -> Dispute:
    try:
        return Dispute.model_validate_json(raw)
    except Exception as exc:
        raise AssuranceStoreError("Assurance journal contains an invalid dispute") from exc


def _encode_settlement(record: "SettlementRecord") -> str:
    return canonical_json(record.model_dump(mode="json"))


def _decode_settlement(raw: str) -> "SettlementRecord":
    from .settlement import SettlementRecord

    try:
        return SettlementRecord.model_validate_json(raw)
    except Exception as exc:
        raise AssuranceStoreError(
            "Assurance journal contains an invalid settlement"
        ) from exc


class AssuranceStore:
    """Cross-process SQLite journal for immutable evidence and mutable dispute state.

    The path is always explicit. Inserts are replay-safe: submitting the exact
    same record is idempotent, while reusing a payment/evidence/dispute identifier
    for different content fails closed.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        raw_path = os.fspath(path)
        if not raw_path.strip():
            raise ValueError("an explicit Assurance database path is required")
        if raw_path == ":memory:":
            raise ValueError("AssuranceStore requires a durable file path, not :memory:")
        self.path = raw_path
        self._lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(raw_path))
        os.makedirs(directory, exist_ok=True)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _migrate(self) -> None:
        with self._lock, self._connect() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise AssuranceStoreError(
                    f"Assurance database schema {version} is newer than supported "
                    f"schema {SCHEMA_VERSION}"
                )
            if version == 0:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS assurance_evidence (
                        evidence_id   TEXT PRIMARY KEY,
                        payment_id    TEXT NOT NULL UNIQUE,
                        evidence_hash TEXT NOT NULL UNIQUE,
                        captured_at   REAL NOT NULL,
                        record_json   TEXT NOT NULL
                    )
                    """
                )
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS assurance_disputes (
                        dispute_id  TEXT PRIMARY KEY,
                        payment_id  TEXT NOT NULL UNIQUE,
                        evidence_id TEXT NOT NULL UNIQUE,
                        status      TEXT NOT NULL,
                        updated_at  REAL NOT NULL,
                        record_json TEXT NOT NULL,
                        FOREIGN KEY (evidence_id)
                            REFERENCES assurance_evidence (evidence_id)
                    )
                    """
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_assurance_disputes_status "
                    "ON assurance_disputes (status, updated_at)"
                )
                db.execute("PRAGMA user_version = 1")
                version = 1
            if version == 1:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS assurance_settlements (
                        dispute_id          TEXT PRIMARY KEY,
                        payment_id_hash     TEXT NOT NULL UNIQUE,
                        binding_hash        TEXT NOT NULL UNIQUE,
                        status              TEXT NOT NULL,
                        arc_transaction_hash TEXT,
                        updated_at          REAL NOT NULL,
                        record_json         TEXT NOT NULL,
                        FOREIGN KEY (dispute_id)
                            REFERENCES assurance_disputes (dispute_id)
                    )
                    """
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_assurance_settlements_status "
                    "ON assurance_settlements (status, updated_at)"
                )
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            db.commit()

    def put_evidence(self, evidence: AssuranceEvidence) -> AssuranceEvidence:
        encoded = _encode_evidence(evidence)
        with self._lock, self._connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    """
                    SELECT record_json FROM assurance_evidence
                    WHERE evidence_id = ? OR payment_id = ?
                    """,
                    (evidence.evidence_id, evidence.payment_id),
                ).fetchone()
                if row is not None:
                    if row[0] != encoded:
                        raise AssuranceStoreError(
                            "payment or evidence identifier was already used for different evidence"
                        )
                    db.commit()
                    return _decode_evidence(row[0])
                db.execute(
                    """
                    INSERT INTO assurance_evidence (
                        evidence_id, payment_id, evidence_hash, captured_at, record_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.evidence_id,
                        evidence.payment_id,
                        evidence.evidence_hash,
                        evidence.paid_response_received_at,
                        encoded,
                    ),
                )
                db.commit()
                return evidence
            except AssuranceStoreError:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                raise AssuranceStoreError(f"could not persist Assurance evidence: {exc}") from exc

    def get_evidence(self, evidence_id: str) -> Optional[AssuranceEvidence]:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT record_json FROM assurance_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            return None if row is None else _decode_evidence(row[0])

    def get_evidence_by_payment(self, payment_id: str) -> Optional[AssuranceEvidence]:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT record_json FROM assurance_evidence WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
            return None if row is None else _decode_evidence(row[0])

    def create_dispute(self, dispute: Dispute) -> Dispute:
        encoded = _encode_dispute(dispute)
        with self._lock, self._connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                evidence_row = db.execute(
                    """
                    SELECT payment_id, evidence_hash FROM assurance_evidence
                    WHERE evidence_id = ?
                    """,
                    (dispute.evidence_id,),
                ).fetchone()
                if evidence_row is None:
                    raise AssuranceStoreError("cannot dispute unknown evidence")
                if evidence_row != (dispute.payment_id, dispute.evidence_hash):
                    raise AssuranceStoreError("dispute does not match its persisted evidence")
                row = db.execute(
                    """
                    SELECT record_json FROM assurance_disputes
                    WHERE dispute_id = ? OR payment_id = ? OR evidence_id = ?
                    """,
                    (dispute.dispute_id, dispute.payment_id, dispute.evidence_id),
                ).fetchone()
                if row is not None:
                    if row[0] != encoded:
                        raise AssuranceStoreError(
                            "one dispute already exists for this payment or evidence"
                        )
                    db.commit()
                    return _decode_dispute(row[0])
                db.execute(
                    """
                    INSERT INTO assurance_disputes (
                        dispute_id, payment_id, evidence_id, status, updated_at, record_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dispute.dispute_id,
                        dispute.payment_id,
                        dispute.evidence_id,
                        dispute.status.value,
                        dispute.updated_at,
                        encoded,
                    ),
                )
                db.commit()
                return dispute
            except AssuranceStoreError:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                raise AssuranceStoreError(f"could not create Assurance dispute: {exc}") from exc

    def update_dispute(self, dispute: Dispute) -> Dispute:
        encoded = _encode_dispute(dispute)
        with self._lock, self._connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    """
                    SELECT payment_id, evidence_id, status, updated_at, record_json
                    FROM assurance_disputes WHERE dispute_id = ?
                    """,
                    (dispute.dispute_id,),
                ).fetchone()
                if row is None:
                    raise AssuranceStoreError(f"unknown dispute ID {dispute.dispute_id!r}")
                payment_id, evidence_id, old_status_raw, old_updated_at, old_json = row
                old_dispute = _decode_dispute(old_json)
                if (
                    payment_id != dispute.payment_id
                    or evidence_id != dispute.evidence_id
                    or old_dispute.evidence_hash != dispute.evidence_hash
                    or old_dispute.reason != dispute.reason
                    or old_dispute.opened_at != dispute.opened_at
                ):
                    raise AssuranceStoreError("dispute's immutable fields cannot be changed")
                if old_json == encoded:
                    db.commit()
                    return _decode_dispute(old_json)
                if dispute.updated_at <= old_updated_at:
                    raise AssuranceStoreError("stale dispute update")
                old_status = DisputeStatus(old_status_raw)
                allowed = {
                    DisputeStatus.OPEN: {
                        DisputeStatus.ADJUDICATING,
                        DisputeStatus.UPHELD,
                        DisputeStatus.REJECTED,
                        DisputeStatus.ERROR,
                    },
                    DisputeStatus.ADJUDICATING: {
                        DisputeStatus.UPHELD,
                        DisputeStatus.REJECTED,
                        DisputeStatus.ERROR,
                    },
                    DisputeStatus.ERROR: {DisputeStatus.ADJUDICATING},
                    DisputeStatus.UPHELD: set(),
                    DisputeStatus.REJECTED: set(),
                }
                if dispute.status not in allowed[old_status]:
                    raise AssuranceStoreError(
                        f"invalid dispute transition {old_status.value} -> {dispute.status.value}"
                    )
                db.execute(
                    """
                    UPDATE assurance_disputes
                    SET status = ?, updated_at = ?, record_json = ?
                    WHERE dispute_id = ?
                    """,
                    (dispute.status.value, dispute.updated_at, encoded, dispute.dispute_id),
                )
                db.commit()
                return dispute
            except AssuranceStoreError:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                raise AssuranceStoreError(f"could not update Assurance dispute: {exc}") from exc

    def get_dispute(self, dispute_id: str) -> Optional[Dispute]:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT record_json FROM assurance_disputes WHERE dispute_id = ?",
                (dispute_id,),
            ).fetchone()
            return None if row is None else _decode_dispute(row[0])

    def get_dispute_by_payment(self, payment_id: str) -> Optional[Dispute]:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT record_json FROM assurance_disputes WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
            return None if row is None else _decode_dispute(row[0])

    def create_settlement(self, record: "SettlementRecord") -> "SettlementRecord":
        encoded = _encode_settlement(record)
        with self._lock, self._connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                dispute = db.execute(
                    "SELECT record_json FROM assurance_disputes WHERE dispute_id = ?",
                    (record.dispute_id,),
                ).fetchone()
                if dispute is None:
                    raise AssuranceStoreError("cannot settle an unknown dispute")
                if _decode_dispute(dispute[0]) != record.binding.dispute:
                    raise AssuranceStoreError(
                        "settlement binding does not match the persisted dispute"
                    )
                evidence = db.execute(
                    "SELECT record_json FROM assurance_evidence WHERE evidence_id = ?",
                    (record.binding.evidence.evidence_id,),
                ).fetchone()
                if evidence is None or _decode_evidence(evidence[0]) != record.binding.evidence:
                    raise AssuranceStoreError(
                        "settlement binding does not match the persisted evidence"
                    )
                row = db.execute(
                    """
                    SELECT record_json FROM assurance_settlements
                    WHERE dispute_id = ? OR payment_id_hash = ? OR binding_hash = ?
                    """,
                    (
                        record.dispute_id,
                        record.payment_id_hash,
                        record.binding_hash,
                    ),
                ).fetchone()
                if row is not None:
                    existing = _decode_settlement(row[0])
                    if existing.binding_hash != record.binding_hash:
                        raise AssuranceStoreError(
                            "dispute or payment already has another settlement binding"
                        )
                    db.commit()
                    return existing
                db.execute(
                    """
                    INSERT INTO assurance_settlements (
                        dispute_id, payment_id_hash, binding_hash, status,
                        arc_transaction_hash, updated_at, record_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.dispute_id,
                        record.payment_id_hash,
                        record.binding_hash,
                        record.status.value,
                        record.arc_transaction_hash,
                        record.updated_at,
                        encoded,
                    ),
                )
                db.commit()
                return record
            except AssuranceStoreError:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                raise AssuranceStoreError(
                    f"could not persist Assurance settlement: {exc}"
                ) from exc

    def update_settlement(self, record: "SettlementRecord") -> "SettlementRecord":
        from .settlement import SettlementStatus

        encoded = _encode_settlement(record)
        with self._lock, self._connect() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    """
                    SELECT binding_hash, status, updated_at, record_json
                    FROM assurance_settlements WHERE dispute_id = ?
                    """,
                    (record.dispute_id,),
                ).fetchone()
                if row is None:
                    raise AssuranceStoreError(
                        f"unknown settlement dispute ID {record.dispute_id!r}"
                    )
                binding_hash, old_status_raw, old_updated_at, old_json = row
                old = _decode_settlement(old_json)
                if (
                    binding_hash != record.binding_hash
                    or old.payment_id_hash != record.payment_id_hash
                    or old.binding != record.binding
                    or old.created_at != record.created_at
                ):
                    raise AssuranceStoreError(
                        "settlement's immutable fields cannot be changed"
                    )
                if old_json == encoded:
                    db.commit()
                    return old
                if record.updated_at <= old_updated_at:
                    raise AssuranceStoreError("stale settlement update")
                old_status = SettlementStatus(old_status_raw)
                allowed = {
                    SettlementStatus.ELIGIBLE: {
                        SettlementStatus.SUBMISSION_ATTEMPTED,
                        SettlementStatus.FAILED_BEFORE_SUBMISSION,
                        SettlementStatus.ALREADY_SETTLED,
                    },
                    SettlementStatus.SUBMISSION_ATTEMPTED: {
                        SettlementStatus.PENDING,
                        SettlementStatus.FAILED_BEFORE_SUBMISSION,
                        SettlementStatus.AMBIGUOUS_SUBMISSION,
                        SettlementStatus.ALREADY_SETTLED,
                    },
                    SettlementStatus.PENDING: {
                        SettlementStatus.CONFIRMED,
                        SettlementStatus.FAILED,
                    },
                    SettlementStatus.AMBIGUOUS_SUBMISSION: {
                        SettlementStatus.PENDING,
                        SettlementStatus.ALREADY_SETTLED,
                    },
                    SettlementStatus.CONFIRMED: set(),
                    SettlementStatus.FAILED_BEFORE_SUBMISSION: set(),
                    SettlementStatus.ALREADY_SETTLED: set(),
                    SettlementStatus.FAILED: set(),
                }
                if record.status not in allowed[old_status]:
                    raise AssuranceStoreError(
                        f"invalid settlement transition "
                        f"{old_status.value} -> {record.status.value}"
                    )
                db.execute(
                    """
                    UPDATE assurance_settlements
                    SET status = ?, arc_transaction_hash = ?, updated_at = ?,
                        record_json = ?
                    WHERE dispute_id = ?
                    """,
                    (
                        record.status.value,
                        record.arc_transaction_hash,
                        record.updated_at,
                        encoded,
                        record.dispute_id,
                    ),
                )
                db.commit()
                return record
            except AssuranceStoreError:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                raise AssuranceStoreError(
                    f"could not update Assurance settlement: {exc}"
                ) from exc

    def get_settlement(self, dispute_id: str) -> Optional["SettlementRecord"]:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT record_json FROM assurance_settlements WHERE dispute_id = ?",
                (dispute_id,),
            ).fetchone()
            return None if row is None else _decode_settlement(row[0])

    def get_settlement_by_payment_hash(
        self,
        payment_id_hash: str,
    ) -> Optional["SettlementRecord"]:
        with self._lock, self._connect() as db:
            row = db.execute(
                """
                SELECT record_json FROM assurance_settlements
                WHERE payment_id_hash = ?
                """,
                (payment_id_hash,),
            ).fetchone()
            return None if row is None else _decode_settlement(row[0])
