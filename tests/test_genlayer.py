"""AgentPay-side payload and finalized GenLayer adapter tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from arc_agent_pay import AssuranceTerms, Chain, PaymentExchange, Service
from arc_agent_pay.assurance import AssuranceEvidence
from arc_agent_pay.assurance.genlayer import (
    AdjudicationPayload,
    GenLayerAdjudicationClient,
    GenLayerStoredVerdict,
    MAX_ADJUDICATION_PAYLOAD_BYTES,
    adjudication_input_hash,
)
from arc_agent_pay.exceptions import (
    AdjudicationPayloadTooLarge,
    GenLayerExecutionFailed,
    GenLayerTransactionNotFinal,
    GenLayerVerdictBindingMismatch,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "assurance_market_brief.json").read_text()
)
PROVIDER = "0x" + "11" * 20
BOND = "0x" + "22" * 20
CONTRACT = "0x" + "33" * 20
TX_ID = "0x" + "44" * 32
PAYMENT_ID = "market-brief-payment-1"


def make_evidence(
    response: str | None = None,
    *,
    criteria: list[str] | None = None,
    request_url: str = "https://brief.example.test/generate",
) -> AssuranceEvidence:
    service = Service(
        name=FIXTURE["service_name"],
        url="https://brief.example.test/generate",
        description=FIXTURE["service_description"],
        price_usdc="0.25",
        chain=Chain.ARC_TESTNET,
        assurance=AssuranceTerms(
            provider_address=PROVIDER,
            warranty_bond_address=BOND,
            acceptance_criteria=criteria or FIXTURE["acceptance_criteria"],
            max_response_time_ms=2_000,
            dispute_window_seconds=3_600,
        ),
    )
    exchange = PaymentExchange(
        payment_id=PAYMENT_ID,
        service_url=service.url,
        amount_usdc="0.25",
        amount_atomic="250000",
        chain="ARC-TESTNET",
        network="eip155:5042002",
        asset="0x3600000000000000000000000000000000000000",
        pay_to=PROVIDER,
        scheme="exact",
        payment_status="success",
        request_method="POST",
        request_url=request_url,
        request_body=json.dumps({"prompt": FIXTURE["request"]}).encode(),
        request_headers={"content-type": "application/json"},
        parsed_payment_required={"x402Version": 2},
        selected_requirement={
            "scheme": "exact",
            "network": "eip155:5042002",
            "asset": "0x3600000000000000000000000000000000000000",
            "payTo": PROVIDER,
            "amount": "250000",
        },
        original_payment_required="original",
        payment_payload_hash="0x" + "55" * 32,
        request_started_at=1_800_000_000.0,
        payment_required_at=1_800_000_000.1,
        payment_authorized_at=1_800_000_000.2,
        paid_response_received_at=1_800_000_001.0,
        paid_response_status=200,
        paid_response_body=(response or FIXTURE["good_response"]).encode(),
        paid_response_headers={"content-type": "text/plain"},
        raw_payment_response="settled",
        settlement_response={"transaction": "0x" + "66" * 32},
        transaction_reference="0x" + "66" * 32,
    )
    return AssuranceEvidence.from_exchange(exchange, service=service)


def make_payload(response: str | None = None, *, allegation: str = "The SLA was breached"):
    return AdjudicationPayload.from_evidence(
        make_evidence(response),
        buyer_dispute_reason=allegation,
    )


def test_payload_is_deterministic_canonical_and_compact():
    first = make_payload()
    second = make_payload()

    assert first == second
    assert first.canonical == second.canonical
    assert first.size_bytes <= MAX_ADJUDICATION_PAYLOAD_BYTES
    assert json.dumps(
        json.loads(first.canonical),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ) == first.canonical


def test_payload_hash_is_sha256_of_exact_canonical_utf8():
    payload = make_payload()
    expected = "0x" + hashlib.sha256(payload.canonical.encode("utf-8")).hexdigest()
    assert adjudication_input_hash(payload) == expected
    assert adjudication_input_hash(payload.canonical) == expected
    assert payload.input_hash == expected


def test_payload_contains_only_adjudication_data_and_redacts_secrets():
    evidence = make_evidence(
        "Authorization: Bearer provider-secret token=another-secret",
        request_url="https://user:pass@brief.example.test/generate?api_key=secret&topic=btc",
    )
    payload = AdjudicationPayload.from_evidence(
        evidence,
        buyer_dispute_reason="cookie=session-secret; response omitted assets",
    )
    serialized = payload.canonical.lower()

    assert "provider-secret" not in serialized
    assert "another-secret" not in serialized
    assert "session-secret" not in serialized
    assert "user:pass" not in serialized
    assert "api_key=secret" not in serialized
    assert "payment-signature" not in serialized
    assert "transaction_reference" not in serialized
    assert "selected_x402" not in serialized
    assert "[redacted]" in serialized


def test_oversized_payload_is_rejected():
    criteria = [f"criterion-{index}: " + "x" * 800 for index in range(20)]
    with pytest.raises(AdjudicationPayloadTooLarge):
        AdjudicationPayload.from_evidence(
            make_evidence(criteria=criteria),
            buyer_dispute_reason="breach",
        )


class FakeGenLayerClient:
    def __init__(self, *, transaction=None, verdict=None):
        self.transaction = transaction
        self.verdict = verdict
        self.write_calls = []
        self.get_calls = []
        self.wait_calls = []
        self.read_calls = []

    def write_contract(self, **kwargs):
        self.write_calls.append(kwargs)
        return bytes.fromhex(TX_ID[2:])

    def get_transaction(self, **kwargs):
        self.get_calls.append(kwargs)
        return self.transaction

    def wait_for_finalization(self, **kwargs):
        self.wait_calls.append(kwargs)
        return self.transaction

    def read_contract(self, **kwargs):
        self.read_calls.append(kwargs)
        return self.verdict


def successful_transaction():
    return {
        "lifecycle": {"state": "finalized", "outcome": "accepted"},
        "txExecutionResult": 1,
        "txExecutionResultName": "FINISHED_WITH_RETURN",
    }


def stored_verdict(payload: AdjudicationPayload, **overrides) -> str:
    values = {
        "dispute_id": payload.dispute_id,
        "payment_id_hash": payload.payment_id_hash,
        "evidence_hash": payload.evidence_hash,
        "terms_hash": payload.terms_hash,
        "adjudication_input_hash": payload.input_hash,
        "sla_violated": True,
        "reason": "The response omitted every required asset.",
    }
    values.update(overrides)
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


def make_client(fake: FakeGenLayerClient) -> GenLayerAdjudicationClient:
    return GenLayerAdjudicationClient(contract_address=CONTRACT, client=fake)


def test_adapter_submits_canonical_payload_and_returns_transaction_identifier():
    payload = make_payload()
    fake = FakeGenLayerClient()
    submission = make_client(fake).submit_adjudication(payload)

    assert submission.transaction_id == TX_ID
    assert submission.dispute_id == payload.dispute_id
    assert submission.adjudication_input_hash == payload.input_hash
    assert fake.write_calls == [
        {
            "address": CONTRACT,
            "function_name": "adjudicate",
            "args": [payload.canonical],
        }
    ]


def test_adapter_passes_checksummed_contract_address_to_client():
    payload = make_payload()
    checksum_contract = "0xAD9B0B4EDa94329c72837263979b0fD3Be4BCD18"
    fake = FakeGenLayerClient()

    make_adapter = GenLayerAdjudicationClient(
        contract_address=checksum_contract,
        client=fake,
    )
    make_adapter.submit_adjudication(payload)

    assert fake.write_calls[0]["address"] == checksum_contract


def test_adapter_exposes_transaction_lifecycle_without_a_verdict():
    fake = FakeGenLayerClient(
        transaction={"lifecycle": {"state": "processing", "phase": "proposing"}}
    )
    status = make_client(fake).get_transaction_status(TX_ID)

    assert status.state == "processing"
    assert status.finalized is False
    assert fake.read_calls == []


def test_non_final_transaction_cannot_return_usable_verdict():
    payload = make_payload()
    fake = FakeGenLayerClient(
        transaction={"lifecycle": {"state": "decided", "outcome": "accepted"}},
        verdict=stored_verdict(payload),
    )
    client = make_client(fake)
    submission = client.submit_adjudication(payload)

    with pytest.raises(GenLayerTransactionNotFinal):
        client.wait_for_finalized_verdict(submission, payload)
    assert fake.read_calls == []


def test_failed_finalized_execution_is_rejected():
    payload = make_payload()
    fake = FakeGenLayerClient(
        transaction={
            "lifecycle": {"state": "finalized", "outcome": "accepted"},
            "txExecutionResult": 2,
            "txExecutionResultName": "FINISHED_WITH_ERROR",
        },
        verdict=stored_verdict(payload),
    )
    client = make_client(fake)
    submission = client.submit_adjudication(payload)

    with pytest.raises(GenLayerExecutionFailed):
        client.wait_for_finalized_verdict(submission, payload)
    assert fake.read_calls == []


def test_finalized_success_reads_and_validates_stored_verdict():
    payload = make_payload(FIXTURE["bad_response"])
    fake = FakeGenLayerClient(
        transaction=successful_transaction(),
        verdict=stored_verdict(payload),
    )
    client = make_client(fake)
    submission = client.submit_adjudication(payload)
    verdict = client.wait_for_finalized_verdict(submission, payload, interval=5, retries=7)

    assert isinstance(verdict, GenLayerStoredVerdict)
    assert verdict.sla_violated is True
    assert fake.wait_calls[0]["interval"] == 5
    assert fake.wait_calls[0]["retries"] == 7
    assert fake.read_calls[0]["function_name"] == "get_verdict"


@pytest.mark.parametrize(
    "field",
    [
        "dispute_id",
        "payment_id_hash",
        "evidence_hash",
        "terms_hash",
        "adjudication_input_hash",
    ],
)
def test_any_stored_verdict_binding_mismatch_is_rejected(field):
    payload = make_payload()
    fake = FakeGenLayerClient(
        transaction=successful_transaction(),
        verdict=stored_verdict(payload, **{field: "0x" + "99" * 32}),
    )
    client = make_client(fake)
    submission = client.submit_adjudication(payload)

    with pytest.raises(GenLayerVerdictBindingMismatch, match=field):
        client.wait_for_finalized_verdict(submission, payload)
