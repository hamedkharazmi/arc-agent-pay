"""Validation-gated workflow models, hashing, EIP-712 signing, and policy checks."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from eth_account import Account
from pydantic import ValidationError

from arc_agent_pay.exceptions import InvalidVerdictError
from arc_agent_pay.workflow import (
    DeliveryEvidence,
    SignedValidationVerdict,
    ValidationVerdict,
    Verifier,
    WorkOrder,
    hash_content,
    recover_verdict_signer,
    sign_verdict,
    verify_signed_verdict,
    verdict_domain,
)


VALIDATOR_KEY = "0x" + "11" * 32
OTHER_KEY = "0x" + "22" * 32
VALIDATOR = Account.from_key(VALIDATOR_KEY).address.lower()
OTHER_VALIDATOR = Account.from_key(OTHER_KEY).address.lower()

ESCROW = "0x" + "aa" * 20
OTHER_ESCROW = "0x" + "ab" * 20
PAYER = "0x" + "bb" * 20
PROVIDER = "0x" + "cc" * 20
ASSET = "0x" + "36" + "00" * 19

TASK_HASH = hash_content("Produce a sourced report about agent payments")
EVIDENCE_HASH = hash_content(b"# Delivered report\nEvidence")
NONCE = "0x" + "01" * 32

DELIVERED_AT = 1_000
NOW = 1_100
DELIVERY_DEADLINE = 1_500
REFUND_AFTER = 2_000


def _order(**overrides) -> WorkOrder:
    values = {
        "escrow": ESCROW,
        "payer": PAYER,
        "provider": PROVIDER,
        "validator": VALIDATOR,
        "asset": ASSET,
        "amount": 100_000,
        "chain_id": 5_042_002,
        "delivery_deadline": DELIVERY_DEADLINE,
        "refund_after": REFUND_AFTER,
        "task_hash": TASK_HASH,
        "nonce": NONCE,
    }
    values.update(overrides)
    return WorkOrder(**values)


def _delivery(order: WorkOrder | None = None, **overrides) -> DeliveryEvidence:
    order = order or _order()
    values = {
        "order_hash": order.order_hash,
        "evidence_hash": EVIDENCE_HASH,
        "evidence_uri": "ipfs://bafy-report",
        "delivered_at": DELIVERED_AT,
    }
    values.update(overrides)
    return DeliveryEvidence(**values)


def _verdict(delivery: DeliveryEvidence | None = None, **overrides) -> ValidationVerdict:
    delivery = delivery or _delivery()
    values = {
        "order_hash": delivery.order_hash,
        "evidence_hash": delivery.evidence_hash,
        "delivery_hash": delivery.delivery_hash,
        "approved": True,
        "score": 95,
        "reason_hash": hash_content("Meets the acceptance criteria"),
        "issued_at": 1_050,
        "valid_until": 1_800,
    }
    values.update(overrides)
    return ValidationVerdict(**values)


def _signed(
    *,
    order: WorkOrder | None = None,
    delivery: DeliveryEvidence | None = None,
    verdict: ValidationVerdict | None = None,
) -> SignedValidationVerdict:
    order = order or _order()
    delivery = delivery or _delivery(order)
    verdict = verdict or _verdict(delivery)
    return sign_verdict(verdict, private_key=VALIDATOR_KEY, order=order)


# ---------------------------------------------------------------------------
# Models and deterministic hashing
# ---------------------------------------------------------------------------

def test_hash_content_binds_exact_bytes():
    assert hash_content("hello") == hash_content(b"hello")
    assert hash_content("hello") != hash_content("Hello")
    with pytest.raises(TypeError):
        hash_content(123)  # type: ignore[arg-type]


def test_order_normalizes_addresses_and_hashes_deterministically():
    first = _order()
    mixed_case = _order(
        escrow="0x" + ESCROW[2:].upper(),
        task_hash="0x" + TASK_HASH[2:].upper(),
    )
    assert first == mixed_case
    assert first.order_hash == mixed_case.order_hash
    assert len(first.order_hash) == 66


def test_hash_vectors_are_stable_for_contract_implementations():
    order = _order()
    delivery = _delivery(order)
    assert order.order_hash == "0x01923193cef5f2c4dbf19b419cd54b6ecfb38f13d0e4705b686d9764e1975655"
    assert delivery.delivery_hash == (
        "0xb8742ff18843928d781f3645a21562453b47d03b8c5b1a34d741cf8c1b8a57fd"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("escrow", OTHER_ESCROW),
        ("payer", "0x" + "bc" * 20),
        ("provider", "0x" + "cd" * 20),
        ("validator", OTHER_VALIDATOR),
        ("asset", "0x" + "37" + "00" * 19),
        ("amount", 100_001),
        ("chain_id", 5_042_003),
        ("delivery_deadline", DELIVERY_DEADLINE + 1),
        ("refund_after", REFUND_AFTER + 1),
        ("task_hash", hash_content("different task")),
        ("nonce", "0x" + "02" * 32),
    ],
)
def test_every_order_field_changes_order_hash(field, value):
    assert _order(**{field: value}).order_hash != _order().order_hash


def test_order_rejects_unsafe_or_ambiguous_values():
    with pytest.raises(ValidationError, match="zero address"):
        _order(payer="0x" + "00" * 20)
    with pytest.raises(ValidationError, match="20-byte"):
        _order(payer="not-an-address")
    with pytest.raises(ValidationError, match="32-byte"):
        _order(task_hash="0x1234")
    with pytest.raises(ValidationError, match="zero bytes32"):
        _order(nonce="0x" + "00" * 32)
    with pytest.raises(ValidationError, match="greater than delivery_deadline"):
        _order(refund_after=DELIVERY_DEADLINE)
    with pytest.raises(ValidationError):
        _order(amount="100000")
    with pytest.raises(ValidationError):
        WorkOrder(**_order().model_dump(), unexpected=True)


def test_workflow_models_are_immutable():
    order = _order()
    with pytest.raises((ValidationError, FrozenInstanceError)):
        order.amount = 1  # type: ignore[misc]


def test_delivery_hash_binds_content_uri_and_time():
    delivery = _delivery()
    assert len(delivery.delivery_hash) == 66
    assert _delivery(evidence_hash=hash_content("other")).delivery_hash != delivery.delivery_hash
    assert _delivery(evidence_uri="ipfs://other").delivery_hash != delivery.delivery_hash
    assert _delivery(delivered_at=DELIVERED_AT + 1).delivery_hash != delivery.delivery_hash


def test_verdict_factory_binds_delivery_and_reason():
    delivery = _delivery()
    verdict = ValidationVerdict.for_delivery(
        delivery,
        approved=False,
        score=20,
        reason="Insufficient evidence",
        issued_at=1_050,
        valid_until=1_800,
    )
    assert verdict.order_hash == delivery.order_hash
    assert verdict.evidence_hash == delivery.evidence_hash
    assert verdict.delivery_hash == delivery.delivery_hash
    assert verdict.reason_hash == hash_content("Insufficient evidence")
    assert verdict.approved is False


def test_verdict_rejects_bad_score_and_window():
    with pytest.raises(ValidationError):
        _verdict(score=101)
    with pytest.raises(ValidationError, match="greater than issued_at"):
        _verdict(valid_until=1_050)


def test_models_round_trip_through_json():
    signed = _signed()
    assert SignedValidationVerdict.model_validate_json(signed.model_dump_json()) == signed


# ---------------------------------------------------------------------------
# Verifier protocol
# ---------------------------------------------------------------------------

def test_verifier_protocol_is_runtime_checkable():
    class ExampleVerifier:
        name = "example"

        async def verify(self, order, delivery):
            return _signed(order=order, delivery=delivery)

    class MissingVerify:
        name = "missing"

    assert isinstance(ExampleVerifier(), Verifier)
    assert not isinstance(MissingVerify(), Verifier)


# ---------------------------------------------------------------------------
# EIP-712 signing and strict verification
# ---------------------------------------------------------------------------

def test_sign_and_verify_verdict_round_trip():
    order = _order()
    delivery = _delivery(order)
    signed = _signed(order=order, delivery=delivery)

    assert signed.validator == VALIDATOR
    assert recover_verdict_signer(
        signed,
        chain_id=order.chain_id,
        verifying_contract=order.escrow,
    ) == VALIDATOR
    assert verify_signed_verdict(
        signed,
        order=order,
        delivery=delivery,
        now=NOW,
        require_approval=True,
    ) == VALIDATOR


def test_signing_refuses_wrong_validator_key():
    order = _order()
    with pytest.raises(InvalidVerdictError, match="signing key"):
        sign_verdict(_verdict(), private_key=OTHER_KEY, order=order)


def test_signing_refuses_mismatched_order_hash():
    other_order = _order(nonce="0x" + "03" * 32)
    with pytest.raises(InvalidVerdictError, match="order_hash"):
        sign_verdict(_verdict(), private_key=VALIDATOR_KEY, order=other_order)


def test_eip712_domain_separates_chain_and_contract():
    order = _order()
    signed = _signed(order=order)
    wrong_chain = recover_verdict_signer(
        signed,
        chain_id=order.chain_id + 1,
        verifying_contract=order.escrow,
    )
    wrong_contract = recover_verdict_signer(
        signed,
        chain_id=order.chain_id,
        verifying_contract=OTHER_ESCROW,
    )
    assert wrong_chain != VALIDATOR
    assert wrong_contract != VALIDATOR


def test_domain_rejects_invalid_values():
    with pytest.raises(ValueError, match="chain_id"):
        verdict_domain(chain_id=0, verifying_contract=ESCROW)
    with pytest.raises(ValueError, match="integer"):
        verdict_domain(chain_id=True, verifying_contract=ESCROW)
    with pytest.raises(ValueError, match="20-byte"):
        verdict_domain(chain_id=1, verifying_contract="bad")


def test_verification_rejects_delivery_for_another_order():
    order = _order()
    other = _order(nonce="0x" + "04" * 32)
    signed = _signed(order=order)
    with pytest.raises(InvalidVerdictError, match="delivery order_hash"):
        verify_signed_verdict(signed, order=order, delivery=_delivery(other), now=NOW)


def test_verification_rejects_different_evidence():
    order = _order()
    signed = _signed(order=order)
    changed = _delivery(order, evidence_hash=hash_content("tampered evidence"))
    with pytest.raises(InvalidVerdictError, match="evidence_hash"):
        verify_signed_verdict(signed, order=order, delivery=changed, now=NOW)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence_uri", "ipfs://tampered-location"),
        ("delivered_at", DELIVERED_AT + 1),
    ],
)
def test_verification_rejects_tampered_delivery_metadata(field, value):
    order = _order()
    signed = _signed(order=order)
    changed = _delivery(order, **{field: value})
    with pytest.raises(InvalidVerdictError, match="delivery_hash"):
        verify_signed_verdict(signed, order=order, delivery=changed, now=NOW)


def test_verification_rejects_late_delivery():
    order = _order()
    delivery = _delivery(order, delivered_at=DELIVERY_DEADLINE + 1)
    verdict = _verdict(delivery, issued_at=DELIVERY_DEADLINE + 2, valid_until=1_900)
    signed = _signed(order=order, delivery=delivery, verdict=verdict)
    with pytest.raises(InvalidVerdictError, match="after the order deadline"):
        verify_signed_verdict(signed, order=order, delivery=delivery, now=1_600)


def test_verification_rejects_verdict_before_delivery():
    order = _order()
    delivery = _delivery(order)
    verdict = _verdict(delivery, issued_at=999, valid_until=1_800)
    signed = _signed(order=order, delivery=delivery, verdict=verdict)
    with pytest.raises(InvalidVerdictError, match="before the delivery"):
        verify_signed_verdict(signed, order=order, delivery=delivery, now=NOW)


def test_verification_rejects_future_or_expired_verdict():
    order = _order()
    delivery = _delivery(order)

    future = _verdict(delivery, issued_at=NOW + 31, valid_until=1_800)
    with pytest.raises(InvalidVerdictError, match="future"):
        verify_signed_verdict(
            _signed(order=order, delivery=delivery, verdict=future),
            order=order,
            delivery=delivery,
            now=NOW,
        )

    expired = _verdict(delivery, valid_until=NOW)
    with pytest.raises(InvalidVerdictError, match="expired"):
        verify_signed_verdict(
            _signed(order=order, delivery=delivery, verdict=expired),
            order=order,
            delivery=delivery,
            now=NOW,
        )


def test_verification_rejects_validity_past_refund_deadline():
    order = _order()
    delivery = _delivery(order)
    verdict = _verdict(delivery, valid_until=REFUND_AFTER + 1)
    with pytest.raises(InvalidVerdictError, match="refund deadline"):
        verify_signed_verdict(
            _signed(order=order, delivery=delivery, verdict=verdict),
            order=order,
            delivery=delivery,
            now=NOW,
        )


def test_rejected_verdict_is_valid_but_cannot_authorize_capture():
    order = _order()
    delivery = _delivery(order)
    verdict = _verdict(delivery, approved=False, score=10)
    signed = _signed(order=order, delivery=delivery, verdict=verdict)
    assert verify_signed_verdict(signed, order=order, delivery=delivery, now=NOW) == VALIDATOR
    with pytest.raises(InvalidVerdictError, match="rejected"):
        verify_signed_verdict(
            signed,
            order=order,
            delivery=delivery,
            now=NOW,
            require_approval=True,
        )


def test_verification_rejects_false_validator_identity():
    order = _order()
    delivery = _delivery(order)
    signed = _signed(order=order, delivery=delivery)
    falsely_labeled = SignedValidationVerdict(
        verdict=signed.verdict,
        validator=OTHER_VALIDATOR,
        signature=signed.signature,
    )
    with pytest.raises(InvalidVerdictError, match="declared validator"):
        verify_signed_verdict(falsely_labeled, order=order, delivery=delivery, now=NOW)


def test_verification_rejects_payload_tampering_after_signature():
    order = _order()
    delivery = _delivery(order)
    signed = _signed(order=order, delivery=delivery)
    tampered = SignedValidationVerdict(
        verdict=ValidationVerdict(**{**signed.verdict.model_dump(), "score": 1}),
        validator=signed.validator,
        signature=signed.signature,
    )
    with pytest.raises(InvalidVerdictError, match="validator"):
        verify_signed_verdict(tampered, order=order, delivery=delivery, now=NOW)


def test_verification_rejects_noncanonical_high_s_signature():
    order = _order()
    delivery = _delivery(order)
    signed = _signed(order=order, delivery=delivery)
    raw = bytearray.fromhex(signed.signature[2:])
    curve_n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    s = int.from_bytes(raw[32:64], "big")
    raw[32:64] = (curve_n - s).to_bytes(32, "big")
    raw[64] = 27 if raw[64] == 28 else 28
    malleable = SignedValidationVerdict(
        verdict=signed.verdict,
        validator=signed.validator,
        signature="0x" + raw.hex(),
    )
    with pytest.raises(InvalidVerdictError, match="canonical"):
        verify_signed_verdict(malleable, order=order, delivery=delivery, now=NOW)


def test_signed_verdict_rejects_malformed_signature():
    with pytest.raises(ValidationError, match="65-byte"):
        SignedValidationVerdict(
            verdict=_verdict(),
            validator=VALIDATOR,
            signature="0x1234",
        )


def test_negative_clock_skew_is_rejected():
    order = _order()
    delivery = _delivery(order)
    with pytest.raises(ValueError, match="non-negative"):
        verify_signed_verdict(
            _signed(order=order, delivery=delivery),
            order=order,
            delivery=delivery,
            now=NOW,
            max_clock_skew=-1,
        )
