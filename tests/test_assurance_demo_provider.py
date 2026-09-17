"""Local-only tests for the real-format Assurance x402 demo provider."""

from __future__ import annotations

import json
import time

import pytest
from eth_account import Account
from x402 import x402ClientSync
from x402.http.utils import (
    decode_payment_required_header,
    decode_payment_response_header,
    encode_payment_signature_header,
)
from x402.schemas import PaymentPayload, PaymentRequirements
from x402.mechanisms.evm.exact.register import register_exact_evm_client
from x402.mechanisms.evm.signers import EthAccountSigner

from arc_agent_pay.assurance.demo_provider import (
    ARC_TESTNET_NETWORK,
    ARC_TESTNET_USDC,
    BAD_RESPONSE_BYTES,
    PAYMENT_AMOUNT,
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    PROVIDER,
    AssuranceDemoProvider,
    DemoPaymentRejected,
    DemoSettlementOutcomeUnknown,
    DemoSettlementPending,
    DemoSubmissionStore,
    demo_service,
    payment_required,
    payment_requirements,
    validate_payment_payload,
)


BUYER = "0x1111111111111111111111111111111111111111"
TX_HASH = "0x" + "ab" * 32
SIGNATURE = "0x" + "12" * 65
NONCE = "0x" + "34" * 32
ENDPOINT = "http://127.0.0.1:8402/assurance-demo"


def make_payload(
    *,
    requirements: PaymentRequirements | None = None,
    authorization_changes: dict | None = None,
    signature: str = SIGNATURE,
) -> PaymentPayload:
    authorization = {
        "from": BUYER,
        "to": PROVIDER,
        "value": str(PAYMENT_AMOUNT),
        "validAfter": "0",
        "validBefore": str(int(time.time()) + 300),
        "nonce": NONCE,
    }
    authorization.update(authorization_changes or {})
    return PaymentPayload(
        x402Version=2,
        accepted=requirements or payment_requirements(),
        payload={"authorization": authorization, "signature": signature},
    )


class FakeSettler:
    def __init__(self):
        self.verify_calls = 0
        self.submit_calls = 0
        self.wait_calls = 0
        self.submit_error = None
        self.wait_error = None
        self.receipt = {"status": 1, "blockNumber": 123, "logs": []}
        self.transfer_ok = True

    def verify(self, payload):
        self.verify_calls += 1
        return BUYER

    def submit(self, payload):
        self.submit_calls += 1
        if self.submit_error:
            raise self.submit_error
        return TX_HASH

    def wait(self, transaction_hash):
        assert transaction_hash == TX_HASH
        self.wait_calls += 1
        if self.wait_error:
            raise self.wait_error
        return self.receipt

    def transfer_matches(self, receipt, payload):
        return self.transfer_ok


@pytest.fixture
def service(tmp_path):
    settler = FakeSettler()
    provider = AssuranceDemoProvider(
        ENDPOINT,
        settler,
        DemoSubmissionStore(tmp_path / "submissions.json"),
    )
    return provider, settler, tmp_path / "submissions.json"


def paid_header(payload: PaymentPayload | None = None) -> dict[str, str]:
    return {PAYMENT_SIGNATURE_HEADER: encode_payment_signature_header(payload or make_payload())}


def test_unpaid_request_returns_exact_x402_v2_requirements(service):
    provider, _, _ = service
    response = provider.handle({})

    assert response.status == 402
    required = decode_payment_required_header(response.headers[PAYMENT_REQUIRED_HEADER])
    assert required.x402_version == 2
    assert len(required.accepts) == 1
    quote = required.accepts[0]
    assert quote.scheme == "exact"
    assert str(quote.network) == ARC_TESTNET_NETWORK
    assert quote.asset == ARC_TESTNET_USDC
    assert quote.pay_to == PROVIDER
    assert quote.amount == str(PAYMENT_AMOUNT)
    assert quote.extra == {"name": "USDC", "version": "2"}


def test_installed_x402_client_payload_round_trips_through_provider_parser():
    buyer = Account.create()
    client = x402ClientSync()
    register_exact_evm_client(client, EthAccountSigner(buyer))

    payload = client.create_payment_payload(payment_required(ENDPOINT))

    assert isinstance(payload, PaymentPayload)
    exact = validate_payment_payload(payload)
    assert exact.authorization.from_address == buyer.address
    assert exact.authorization.to == PROVIDER
    assert exact.authorization.value == str(PAYMENT_AMOUNT)
    assert len(bytes.fromhex((exact.signature or "")[2:])) == 65


def test_demo_service_has_locked_assurance_sla():
    service = demo_service(ENDPOINT)
    assert service.price_usdc == "1"
    assert service.assurance is not None
    assert service.assurance.provider_address == PROVIDER.lower()
    assert service.assurance.max_response_time_ms == 2_000
    assert any("BTC, ETH, and SOL" in item for item in service.assurance.acceptance_criteria)


def altered_requirements(**changes) -> PaymentRequirements:
    data = payment_requirements().model_dump(by_alias=True)
    data.update(changes)
    return PaymentRequirements.model_validate(data)


@pytest.mark.parametrize(
    ("requirements", "message"),
    [
        (altered_requirements(network="eip155:1"), "network"),
        (altered_requirements(asset="0x" + "22" * 20), "asset"),
        (altered_requirements(payTo="0x" + "33" * 20), "recipient"),
        (altered_requirements(amount="999999"), "amount"),
    ],
)
def test_wrong_quote_fields_are_rejected(requirements, message):
    with pytest.raises(DemoPaymentRejected, match=message):
        validate_payment_payload(make_payload(requirements=requirements))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"nonce": "0x12"}, "nonce"),
        ({"to": "0x" + "44" * 20}, "recipient"),
        ({"value": "2"}, "amount"),
        ({"validBefore": "1"}, "expired"),
        ({"validAfter": str(int(time.time()) + 600)}, "not yet valid"),
    ],
)
def test_malformed_or_invalid_authorization_is_rejected(changes, message):
    with pytest.raises(DemoPaymentRejected, match=message):
        validate_payment_payload(make_payload(authorization_changes=changes))


def test_malformed_signature_is_rejected():
    with pytest.raises(DemoPaymentRejected, match="signature"):
        validate_payment_payload(make_payload(signature="0x12"))


def test_paid_payload_is_decoded_settled_once_and_returns_payment_response(service):
    provider, settler, _ = service
    headers = paid_header()
    response = provider.handle(headers)
    replay = provider.handle(headers)

    assert response.status == replay.status == 200
    assert response.body == replay.body == BAD_RESPONSE_BYTES
    settled = decode_payment_response_header(response.headers[PAYMENT_RESPONSE_HEADER])
    assert settled.success is True
    assert settled.transaction == TX_HASH
    assert settled.network == ARC_TESTNET_NETWORK
    assert settled.payer.lower() == BUYER.lower()
    assert settled.amount == str(PAYMENT_AMOUNT)
    assert settled.extra == {"asset": ARC_TESTNET_USDC, "payTo": PROVIDER}
    assert settler.verify_calls == 1
    assert settler.submit_calls == 1
    assert settler.wait_calls == 1


def test_same_authorization_with_changed_outer_payload_cannot_rebroadcast(service):
    provider, settler, _ = service
    first_payload = make_payload()
    changed_payload = first_payload.model_copy(
        update={"extensions": {"untrusted": {"value": "changed"}}}
    )

    assert provider.handle(paid_header(first_payload)).status == 200
    assert provider.handle(paid_header(changed_payload)).status == 200
    assert settler.submit_calls == 1


def test_bad_body_is_deterministic_and_materially_violates_sla(service):
    provider, _, _ = service
    first = provider.handle(paid_header()).body
    second = provider.handle(paid_header()).body
    text = json.loads(first)["brief"]

    assert first == second == BAD_RESPONSE_BYTES
    assert "DOGE" in text
    assert not all(asset in text for asset in ("BTC", "ETH", "SOL"))
    assert "risk" not in text.lower() and "caveat" not in text.lower()


def test_ambiguous_broadcast_is_recorded_and_never_retried(service):
    provider, settler, state_path = service
    settler.submit_error = DemoSettlementOutcomeUnknown("unknown")
    headers = paid_header()

    assert provider.handle(headers).status == 503
    assert provider.handle(headers).status == 503
    assert settler.submit_calls == 1
    state = state_path.read_text()
    assert '"status": "ambiguous"' in state
    assert SIGNATURE not in state


def test_known_transaction_polling_failure_does_not_resubmit(service):
    provider, settler, _ = service
    settler.wait_error = DemoSettlementPending(TX_HASH)
    headers = paid_header()

    first = provider.handle(headers)
    second = provider.handle(headers)
    assert first.status == second.status == 503
    assert TX_HASH.encode() in first.body
    assert settler.submit_calls == 1
    assert settler.wait_calls == 2


@pytest.mark.parametrize(
    "receipt,transfer_ok",
    [({"status": 0, "blockNumber": 123, "logs": []}, True), (None, False)],
)
def test_failed_settlement_never_returns_paid_content(service, receipt, transfer_ok):
    provider, settler, _ = service
    if receipt is not None:
        settler.receipt = receipt
    settler.transfer_ok = transfer_ok

    response = provider.handle(paid_header())
    assert response.status == 402
    assert response.body != BAD_RESPONSE_BYTES
    assert PAYMENT_RESPONSE_HEADER not in response.headers


def test_provider_state_contains_no_private_or_raw_payment_signing_material(service):
    provider, _, state_path = service
    provider.handle(paid_header())
    persisted = state_path.read_text()

    assert SIGNATURE not in persisted
    assert NONCE not in persisted
    assert "private" not in persisted.lower()
