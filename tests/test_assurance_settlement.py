"""Phase 4 cross-chain binding, persistence, and trusted relay tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from requests import exceptions as requests_exceptions

from arc_agent_pay import AssuranceTerms, Chain, PaymentExchange, Service
from arc_agent_pay.assurance import (
    AdjudicationPayload,
    AssuranceEvidence,
    AssuranceSettlementBinding,
    AssuranceSettlementRelay,
    AssuranceStore,
    AssuranceVerdict,
    BondTransactionResult,
    Dispute,
    DisputeStatus,
    FinalizedGenLayerAdjudication,
    GenLayerStoredVerdict,
    GenLayerSubmission,
    GenLayerTransactionStatus,
    SettlementRecord,
    SettlementStatus,
    VerifiedArcPayment,
    payment_id_hash,
)
from arc_agent_pay.assurance.arc_payment import (
    ARC_TESTNET_CHAIN_ID,
    ARC_TESTNET_NETWORK,
    ARC_TESTNET_USDC,
)
from arc_agent_pay.exceptions import (
    SettlementAlreadyProcessed,
    SettlementBindingError,
    SettlementIneligible,
    SettlementPending,
    SettlementPreflightError,
    SettlementSubmissionOutcomeUnknown,
)

PROVIDER = "0x" + "11" * 20
BUYER = "0x" + "22" * 20
BOND = "0x" + "33" * 20
OPERATOR = "0x" + "44" * 20
GENLAYER_CONTRACT = "0x41c5de73bda3379351a2d77648cb77389841dc53"
GENLAYER_TX = "0x" + "55" * 32
ARC_PAYMENT_TX = "0x" + "66" * 32
ARC_REFUND_TX = "0x" + "77" * 32
AMOUNT = 250_000


def make_evidence(*, payment_id: str = "assured-payment-0001") -> AssuranceEvidence:
    service = Service(
        name="AI Market Brief",
        url="https://api.example.test/brief",
        description="Produces a concise market brief",
        price_usdc="0.25",
        chain=Chain.ARC_TESTNET,
        assurance=AssuranceTerms(
            provider_address=PROVIDER,
            warranty_bond_address=BOND,
            acceptance_criteria=["Cover BTC, ETH, and SOL with material risks"],
            dispute_window_seconds=3_600,
        ),
    )
    exchange = PaymentExchange(
        payment_id=payment_id,
        service_url=service.url,
        amount_usdc="0.25",
        amount_atomic=str(AMOUNT),
        chain="ARC-TESTNET",
        network=ARC_TESTNET_NETWORK,
        asset=ARC_TESTNET_USDC,
        pay_to=PROVIDER,
        scheme="exact",
        payment_status="success",
        request_method="POST",
        request_url=service.url,
        request_body=b'{"request":"market brief"}',
        request_headers={"content-type": "application/json"},
        parsed_payment_required={"x402Version": 2},
        selected_requirement={
            "scheme": "exact",
            "network": ARC_TESTNET_NETWORK,
            "asset": ARC_TESTNET_USDC,
            "payTo": PROVIDER,
            "amount": str(AMOUNT),
        },
        original_payment_required="real-payment-required",
        payment_payload_hash="0x" + "88" * 32,
        request_started_at=1_900_000_000.0,
        payment_required_at=1_900_000_000.1,
        payment_authorized_at=1_900_000_000.2,
        paid_response_received_at=1_900_000_001.0,
        paid_response_status=200,
        paid_response_body=b"BTC only; no risks discussed.",
        paid_response_headers={"content-type": "text/plain"},
        raw_payment_response="real-settlement-response",
        settlement_response={"transaction": ARC_PAYMENT_TX},
        transaction_reference=ARC_PAYMENT_TX,
    )
    return AssuranceEvidence.from_exchange(exchange, service=service)


def make_sources(
    *,
    violated: bool = True,
    transaction_state: str = "finalized",
    execution_result: str = "FINISHED_WITH_RETURN",
    contract_address: str = GENLAYER_CONTRACT,
    payment_id: str = "assured-payment-0001",
):
    evidence = make_evidence(payment_id=payment_id)
    payload = AdjudicationPayload.from_evidence(
        evidence,
        buyer_dispute_reason="The response omitted required assets and risks.",
    )
    verified = VerifiedArcPayment(
        payment_id=evidence.payment_id,
        payment_id_hash=payment_id_hash(evidence.payment_id),
        transaction_hash=ARC_PAYMENT_TX,
        block_number=100,
        block_timestamp=1_900_000_002,
        confirmations=2,
        buyer=BUYER,
        provider=PROVIDER,
        asset=ARC_TESTNET_USDC,
        amount_atomic=AMOUNT,
        chain_id=ARC_TESTNET_CHAIN_ID,
        evidence_hash=evidence.evidence_hash,
    )
    stored = GenLayerStoredVerdict(
        dispute_id=payload.dispute_id,
        payment_id_hash=payload.payment_id_hash,
        evidence_hash=payload.evidence_hash,
        terms_hash=payload.terms_hash,
        adjudication_input_hash=payload.input_hash,
        sla_violated=violated,
        reason="Material requirements were omitted." if violated else "All criteria met.",
    )
    submission = GenLayerSubmission(
        transaction_id=GENLAYER_TX,
        dispute_id=payload.dispute_id,
        adjudication_input_hash=payload.input_hash,
    )
    status = GenLayerTransactionStatus(
        transaction_id=GENLAYER_TX,
        state=transaction_state,
        outcome="accepted",
        execution_result=execution_result,
    )
    final = FinalizedGenLayerAdjudication(
        contract_address=contract_address,
        submission=submission,
        transaction_status=status,
        verdict=stored,
    )
    local_verdict = AssuranceVerdict(
        dispute_id=payload.dispute_id,
        evidence_hash=evidence.evidence_hash,
        sla_violated=violated,
        reason=stored.reason,
        decided_at=1_900_000_010.0,
        adjudicator_reference=GENLAYER_TX,
    )
    dispute = Dispute(
        dispute_id=payload.dispute_id,
        payment_id=evidence.payment_id,
        evidence_id=evidence.evidence_id,
        evidence_hash=evidence.evidence_hash,
        reason="Response breached the frozen SLA.",
        status=DisputeStatus.UPHELD if violated else DisputeStatus.REJECTED,
        opened_at=1_900_000_003.0,
        updated_at=1_900_000_010.0,
        verdict=local_verdict,
    )
    return evidence, dispute, verified, payload, final


def make_binding(**changes) -> AssuranceSettlementBinding:
    evidence, dispute, verified, payload, final = make_sources(**changes)
    return AssuranceSettlementBinding.from_sources(
        evidence=evidence,
        dispute=dispute,
        verified_payment=verified,
        adjudication_payload=payload,
        adjudication=final,
        configured_genlayer_contract=GENLAYER_CONTRACT,
    )


class FakeBond:
    def __init__(self):
        self.address = BOND
        self.account = SimpleNamespace(address=OPERATOR)
        self.balance = AMOUNT
        self.processed = False
        self.refunded = False
        self.submit_calls = []
        self.wait_calls = []
        self.submit_error = None
        self.wait_error = None

    def chain_id(self):
        return ARC_TESTNET_CHAIN_ID

    def resolver(self):
        return OPERATOR

    def usdc(self):
        return ARC_TESTNET_USDC

    def dispute_processed(self, _dispute_id):
        return self.processed

    def payment_refunded(self, _payment_id_hash):
        return self.refunded

    def bond_balance(self, _provider):
        return self.balance

    def submit_refund(self, *args):
        self.submit_calls.append(args)
        if self.submit_error:
            raise self.submit_error
        return ARC_REFUND_TX

    def wait_for_refund(self, tx_hash):
        self.wait_calls.append(tx_hash)
        if self.wait_error:
            raise self.wait_error
        return BondTransactionResult(
            tx_hash=tx_hash,
            block_number=222,
            gas_used=54_321,
            receipt={"status": 1},
        )


class FakePaymentVerifier:
    def __init__(self, verified):
        self.verified = verified
        self.calls = []

    def verify(self, evidence):
        self.calls.append(evidence)
        return self.verified


def persisted_store(tmp_path, binding):
    store = AssuranceStore(tmp_path / "assurance.db")
    store.put_evidence(binding.evidence)
    store.create_dispute(binding.dispute)
    return store


def relay(tmp_path, binding, bond=None, verifier=None):
    bond = bond or FakeBond()
    verifier = verifier or FakePaymentVerifier(binding.verified_payment)
    return (
        AssuranceSettlementRelay(
            store=persisted_store(tmp_path, binding),
            bond_client=bond,
            payment_verifier=verifier,
            operator_address=OPERATOR,
            genlayer_contract_address=GENLAYER_CONTRACT,
            clock=lambda: 1_900_000_020.0,
            sleep=lambda _delay: None,
        ),
        bond,
    )


def test_bad_finalized_verdict_and_verified_arc_payment_is_eligible_and_settles(tmp_path):
    binding = make_binding(violated=True)
    binding.ensure_eligible()
    settlement_relay, bond = relay(tmp_path, binding)

    result = settlement_relay.settle(binding)

    assert result.status == "confirmed"
    assert result.sla_violated is True
    assert result.buyer == BUYER
    assert result.provider == PROVIDER
    assert result.amount_atomic == AMOUNT
    assert bond.submit_calls == [
        (binding.dispute_id, binding.payment_id_hash, PROVIDER, BUYER, AMOUNT)
    ]


def test_good_finalized_verdict_is_explicitly_not_settleable(tmp_path):
    binding = make_binding(violated=False)
    settlement_relay, bond = relay(tmp_path, binding)
    with pytest.raises(SettlementIneligible, match="fulfilled SLA"):
        settlement_relay.settle(binding)
    assert bond.submit_calls == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"transaction_state": "decided"}, "not protocol-finalized"),
        ({"execution_result": "FINISHED_WITH_ERROR"}, "did not succeed"),
    ],
)
def test_nonfinal_or_failed_genlayer_adjudication_is_rejected(changes, message):
    with pytest.raises(ValidationError, match=message):
        make_sources(**changes)


def test_missing_finalized_verdict_is_rejected():
    evidence, dispute, verified, payload, final = make_sources()
    with pytest.raises(ValidationError):
        FinalizedGenLayerAdjudication(
            contract_address=GENLAYER_CONTRACT,
            submission=final.submission,
            transaction_status=final.transaction_status,
            verdict=None,
        )


def test_wrong_genlayer_contract_is_rejected():
    evidence, dispute, verified, payload, final = make_sources(contract_address="0x" + "99" * 20)
    with pytest.raises(SettlementBindingError, match="configured contract"):
        AssuranceSettlementBinding.from_sources(
            evidence=evidence,
            dispute=dispute,
            verified_payment=verified,
            adjudication_payload=payload,
            adjudication=final,
            configured_genlayer_contract=GENLAYER_CONTRACT,
        )


@pytest.mark.parametrize(
    ("target", "field", "value", "message"),
    [
        ("verified", "payment_id_hash", "0x" + "01" * 32, "payment_id_hash"),
        ("verified", "evidence_hash", "0x" + "02" * 32, "evidence_hash"),
        ("dispute", "dispute_id", "0x" + "03" * 32, "dispute_id"),
        ("payload", "terms_hash", "0x" + "04" * 32, "terms_hash"),
        ("submission", "adjudication_input_hash", "0x" + "05" * 32, "input hash"),
        ("verified", "provider", "0x" + "06" * 20, "provider"),
        ("verified", "amount_atomic", AMOUNT + 1, "amount"),
        ("verified", "asset", "0x" + "07" * 20, "USDC"),
        ("verified", "chain_id", 1, "5042002"),
    ],
)
def test_binding_mismatches_are_rejected(target, field, value, message):
    evidence, dispute, verified, payload, final = make_sources()
    if target == "verified":
        verified = verified.model_copy(update={field: value})
    elif target == "dispute":
        dispute = dispute.model_copy(update={field: value})
    elif target == "payload":
        payload = payload.model_copy(update={field: value})
    elif target == "submission":
        submission = final.submission.model_copy(update={field: value})
        final = final.model_copy(update={"submission": submission})
    with pytest.raises(SettlementBindingError, match=message):
        AssuranceSettlementBinding.from_sources(
            evidence=evidence,
            dispute=dispute,
            verified_payment=verified,
            adjudication_payload=payload,
            adjudication=final,
            configured_genlayer_contract=GENLAYER_CONTRACT,
        )


def test_flat_buyer_override_is_rejected_and_relay_has_no_override_arguments():
    binding = make_binding()
    tampered = json.loads(binding.model_dump_json())
    tampered["buyer"] = "0x" + "aa" * 20
    with pytest.raises(ValidationError, match="buyer"):
        AssuranceSettlementBinding.model_validate_json(json.dumps(tampered))
    with pytest.raises(TypeError):
        AssuranceSettlementRelay.settle(None, binding, buyer="0x" + "aa" * 20)


def test_non_verifier_result_is_rejected():
    evidence, dispute, verified, payload, final = make_sources()
    with pytest.raises(SettlementBindingError, match="ArcPaymentVerifier"):
        AssuranceSettlementBinding.from_sources(
            evidence=evidence,
            dispute=dispute,
            verified_payment=verified.model_dump(mode="json"),
            adjudication_payload=payload,
            adjudication=final,
            configured_genlayer_contract=GENLAYER_CONTRACT,
        )


def test_relay_revalidates_finality_on_a_tampered_binding_before_submission(tmp_path):
    binding = make_binding()
    tampered_status = binding.adjudication.transaction_status.model_copy(
        update={"state": "decided"}
    )
    tampered_adjudication = binding.adjudication.model_copy(
        update={"transaction_status": tampered_status}
    )
    tampered = binding.model_copy(update={"adjudication": tampered_adjudication})
    bond = FakeBond()
    settlement_relay, _ = relay(tmp_path, binding, bond)

    with pytest.raises(SettlementBindingError, match="finalized"):
        settlement_relay.settle(tampered)
    assert bond.submit_calls == []


def test_synthetic_smoke_evidence_is_never_eligible():
    binding = make_binding(payment_id="synthetic-genlayer-smoke-bad-v1")
    with pytest.raises(SettlementIneligible, match="synthetic"):
        binding.ensure_eligible()


def test_settlement_binding_is_deterministic_immutable_and_serializable():
    first = make_binding()
    second = make_binding()
    assert first.binding_hash == second.binding_hash
    assert AssuranceSettlementBinding.model_validate_json(first.model_dump_json()) == first
    with pytest.raises(ValidationError):
        first.buyer = "0x" + "aa" * 20


def test_local_settlement_persistence_and_duplicate_ids_are_idempotent(tmp_path):
    binding = make_binding()
    store = persisted_store(tmp_path, binding)
    record = SettlementRecord.eligible(binding, now=1_900_000_020.0)
    assert store.create_settlement(record) == record
    assert store.create_settlement(record) == record
    reopened = AssuranceStore(store.path)
    assert reopened.get_settlement(binding.dispute_id) == record
    assert reopened.get_settlement_by_payment_hash(binding.payment_id_hash) == record


@pytest.mark.parametrize("replay_field", ["processed", "refunded"])
def test_warranty_bond_replay_flags_prevent_submission(tmp_path, replay_field):
    binding = make_binding()
    bond = FakeBond()
    setattr(bond, replay_field, True)
    settlement_relay, _ = relay(tmp_path, binding, bond)
    with pytest.raises(SettlementAlreadyProcessed, match="already processed"):
        settlement_relay.settle(binding)
    assert bond.submit_calls == []
    assert settlement_relay.store.get_settlement(binding.dispute_id).status == (
        SettlementStatus.ALREADY_SETTLED
    )


def test_insufficient_provider_bond_rejected_before_submission(tmp_path):
    binding = make_binding()
    bond = FakeBond()
    bond.balance = AMOUNT - 1
    settlement_relay, _ = relay(tmp_path, binding, bond)
    with pytest.raises(SettlementPreflightError, match="refund requires"):
        settlement_relay.settle(binding)
    assert bond.submit_calls == []
    assert settlement_relay.store.get_settlement(binding.dispute_id).status == (
        SettlementStatus.FAILED_BEFORE_SUBMISSION
    )


def test_arc_submission_called_once_and_confirmed_duplicate_is_idempotent(tmp_path):
    binding = make_binding()
    settlement_relay, bond = relay(tmp_path, binding)
    first = settlement_relay.settle(binding)
    second = settlement_relay.settle(binding)
    assert second == first
    assert len(bond.submit_calls) == 1


def test_tx_hash_is_persisted_when_receipt_polling_fails_and_never_resubmitted(tmp_path):
    binding = make_binding()
    bond = FakeBond()
    bond.wait_error = requests_exceptions.ConnectionError("Arc RPC unavailable")
    settlement_relay, _ = relay(tmp_path, binding, bond)

    with pytest.raises(SettlementPending) as raised:
        settlement_relay.settle(binding)

    assert raised.value.transaction_hash == ARC_REFUND_TX
    record = settlement_relay.store.get_settlement(binding.dispute_id)
    assert record.status == SettlementStatus.PENDING
    assert record.arc_transaction_hash == ARC_REFUND_TX
    assert len(bond.submit_calls) == 1

    bond.wait_error = None
    restarted = AssuranceSettlementRelay(
        store=AssuranceStore(settlement_relay.store.path),
        bond_client=bond,
        payment_verifier=FakePaymentVerifier(binding.verified_payment),
        operator_address=OPERATOR,
        genlayer_contract_address=GENLAYER_CONTRACT,
        clock=lambda: 1_900_000_030.0,
    )
    result = restarted.settle(binding)
    assert result.arc_refund_transaction_hash == ARC_REFUND_TX
    assert len(bond.submit_calls) == 1
    assert bond.wait_calls == [ARC_REFUND_TX, ARC_REFUND_TX]


def test_ambiguous_arc_submission_is_not_retried(tmp_path):
    binding = make_binding()
    bond = FakeBond()
    bond.submit_error = SettlementSubmissionOutcomeUnknown("connection lost during Arc broadcast")
    settlement_relay, _ = relay(tmp_path, binding, bond)

    with pytest.raises(SettlementSubmissionOutcomeUnknown):
        settlement_relay.settle(binding)
    assert len(bond.submit_calls) == 1
    assert settlement_relay.store.get_settlement(binding.dispute_id).status == (
        SettlementStatus.AMBIGUOUS_SUBMISSION
    )

    bond.submit_error = None
    with pytest.raises(SettlementSubmissionOutcomeUnknown):
        settlement_relay.settle(binding)
    assert len(bond.submit_calls) == 1


def test_warranty_bond_configuration_invariants_are_rechecked(tmp_path):
    binding = make_binding()
    bond = FakeBond()
    bond.account.address = BUYER
    settlement_relay, _ = relay(tmp_path, binding, bond)
    with pytest.raises(SettlementPreflightError, match="signer"):
        settlement_relay.settle(binding)
    assert bond.submit_calls == []


def test_fresh_arc_reverification_mismatch_rejects_before_submission(tmp_path):
    binding = make_binding()
    mismatched = binding.verified_payment.model_copy(update={"buyer": "0x" + "aa" * 20})
    verifier = FakePaymentVerifier(mismatched)
    settlement_relay, bond = relay(tmp_path, binding, verifier=verifier)

    with pytest.raises(SettlementBindingError, match="fresh ArcPaymentVerifier"):
        settlement_relay.settle(binding)
    assert verifier.calls == [binding.evidence]
    assert bond.submit_calls == []


def test_fresh_arc_reverification_accepts_increased_confirmations(tmp_path):
    binding = make_binding()
    reverified = binding.verified_payment.model_copy(
        update={"confirmations": binding.verified_payment.confirmations + 100}
    )
    verifier = FakePaymentVerifier(reverified)
    settlement_relay, bond = relay(tmp_path, binding, verifier=verifier)

    result = settlement_relay.settle(binding)

    assert result.status == "confirmed"
    assert verifier.calls == [binding.evidence]
    assert len(bond.submit_calls) == 1


def test_fresh_arc_reverification_rejects_confirmation_regression(tmp_path):
    binding = make_binding()
    reverified = binding.verified_payment.model_copy(update={"confirmations": 1})
    verifier = FakePaymentVerifier(reverified)
    settlement_relay, bond = relay(tmp_path, binding, verifier=verifier)

    with pytest.raises(SettlementBindingError, match="fresh ArcPaymentVerifier"):
        settlement_relay.settle(binding)

    assert bond.submit_calls == []
