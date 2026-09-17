"""Deterministic tests for Arc x402 payment verification from USDC logs."""

from __future__ import annotations

from types import SimpleNamespace

from eth_utils import keccak
import pytest

from arc_agent_pay import AssuranceTerms, Chain, PaymentExchange, Service
from arc_agent_pay.assurance import AssuranceEvidence
from arc_agent_pay.assurance.arc_payment import (
    ARC_TESTNET_CHAIN_ID,
    ARC_TESTNET_USDC,
    DISPUTE_ID_DOMAIN,
    PAYMENT_ID_DOMAIN,
    TRANSFER_TOPIC,
    ArcPaymentVerifier,
    dispute_id,
    payment_id_hash,
)
from arc_agent_pay.exceptions import (
    AmbiguousPaymentTransfer,
    InsufficientConfirmations,
    InvalidTransactionReference,
    MissingTransactionReference,
    PaymentAmountMismatch,
    PaymentTransferNotFound,
    ProviderBindingMismatch,
    TransactionNotFound,
    TransactionReverted,
    WrongArcChain,
    WrongPaymentAsset,
    WrongPaymentNetwork,
)


PROVIDER = "0x" + "11" * 20
BUYER = "0x" + "22" * 20
OTHER = "0x" + "33" * 20
BOND = "0x" + "44" * 20
OTHER_TOKEN = "0x" + "55" * 20
TX_HASH = "0x" + "aa" * 32
PAYMENT_ID = "payment_arc_verification_0001"
AMOUNT = 125_000
BLOCK_NUMBER = 100
BLOCK_TIMESTAMP = 1_800_000_000


def _topic_address(address: str) -> bytes:
    return b"\x00" * 12 + bytes.fromhex(address[2:])


def _transfer_log(
    *,
    sender: str = BUYER,
    recipient: str = PROVIDER,
    amount: int = AMOUNT,
    token: str = ARC_TESTNET_USDC,
) -> dict:
    return {
        "address": token,
        "topics": [
            TRANSFER_TOPIC,
            _topic_address(sender),
            _topic_address(recipient),
        ],
        "data": amount.to_bytes(32, byteorder="big"),
    }


def _evidence(
    *,
    transaction_reference=TX_HASH,
    payment_status: str = "success",
    network: str = "eip155:5042002",
    asset: str = ARC_TESTNET_USDC,
    pay_to: str = PROVIDER,
    amount: str = str(AMOUNT),
    provider: str = PROVIDER,
    service_price: str = "999.99",
    service_chain: Chain = Chain.ARC_TESTNET,
) -> AssuranceEvidence:
    service = Service(
        name="Assured API",
        url="https://api.example.test/result",
        price_usdc=service_price,
        chain=service_chain,
        assurance=AssuranceTerms(
            provider_address=provider,
            warranty_bond_address=BOND,
            acceptance_criteria=["Response is correct"],
            dispute_window_seconds=3_600,
        ),
    )
    exchange = PaymentExchange(
        payment_id=PAYMENT_ID,
        service_url=service.url,
        amount_usdc="0.000001",  # deliberately not authoritative
        amount_atomic="1",  # deliberately not authoritative
        chain="ARC-TESTNET",
        network=network,
        asset=asset,
        pay_to=pay_to,
        scheme="exact",
        payment_status=payment_status,
        request_method="GET",
        request_url=service.url,
        request_body=b"",
        request_headers={},
        parsed_payment_required={"x402Version": 2},
        selected_requirement={
            "scheme": "exact",
            "network": network,
            "asset": asset,
            "payTo": pay_to,
            "amount": amount,
        },
        original_payment_required="original",
        payment_payload_hash="0x" + "66" * 32,
        request_started_at=1.0,
        payment_required_at=2.0,
        payment_authorized_at=3.0,
        paid_response_received_at=4.0,
        paid_response_status=200,
        paid_response_body=b"{}",
        paid_response_headers={"content-type": "application/json"},
        raw_payment_response="settled",
        settlement_response={"transaction": transaction_reference},
        transaction_reference=transaction_reference,
    )
    return AssuranceEvidence.from_exchange(exchange, service=service)


class FakeEth:
    def __init__(
        self,
        *,
        chain_id: int = ARC_TESTNET_CHAIN_ID,
        receipt=None,
        latest_block: int = BLOCK_NUMBER + 1,
        block_timestamp: int = BLOCK_TIMESTAMP,
    ) -> None:
        self.chain_id = chain_id
        self.receipt = receipt
        self.block_number = latest_block
        self.block_timestamp = block_timestamp
        self.receipt_requests = []
        self.block_requests = []

    def get_transaction_receipt(self, tx_hash):
        self.receipt_requests.append(tx_hash)
        return self.receipt

    def get_block(self, block_number):
        self.block_requests.append(block_number)
        return {"number": block_number, "timestamp": self.block_timestamp}


def _receipt(*logs, status: int = 1, block_number: int = BLOCK_NUMBER):
    return {
        "status": status,
        "blockNumber": block_number,
        "logs": list(logs),
    }


def _verifier(*, receipt=None, chain_id=ARC_TESTNET_CHAIN_ID, latest=BLOCK_NUMBER + 1):
    eth = FakeEth(chain_id=chain_id, receipt=receipt, latest_block=latest)
    return ArcPaymentVerifier(w3=SimpleNamespace(eth=eth)), eth


def test_payment_id_hash_is_exact_and_deterministic():
    expected = "0x" + keccak(PAYMENT_ID_DOMAIN + PAYMENT_ID.encode("utf-8")).hex()
    assert payment_id_hash(PAYMENT_ID) == expected
    assert payment_id_hash(PAYMENT_ID) == payment_id_hash(PAYMENT_ID)


def test_dispute_id_is_exact_and_deterministic():
    payment_hash = payment_id_hash(PAYMENT_ID)
    evidence_hash = "0x" + "77" * 32
    expected = "0x" + keccak(
        DISPUTE_ID_DOMAIN
        + bytes.fromhex(payment_hash[2:])
        + bytes.fromhex(evidence_hash[2:])
    ).hex()
    assert dispute_id(payment_hash, evidence_hash) == expected
    assert dispute_id(payment_hash, evidence_hash) == expected


def test_hash_domains_are_separated():
    identifier = "same-input"
    payment_digest = payment_id_hash(identifier)
    non_payment_digest = "0x" + keccak(
        DISPUTE_ID_DOMAIN + identifier.encode("utf-8")
    ).hex()
    assert PAYMENT_ID_DOMAIN != DISPUTE_ID_DOMAIN
    assert payment_digest != non_payment_digest


def test_wrong_chain_is_rejected_before_receipt_lookup():
    verifier, eth = _verifier(receipt=_receipt(_transfer_log()), chain_id=1)
    with pytest.raises(WrongArcChain):
        verifier.verify(_evidence())
    assert eth.receipt_requests == []


def test_missing_transaction_reference_is_rejected():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log()))
    with pytest.raises(MissingTransactionReference):
        verifier.verify(_evidence(transaction_reference=None))


def test_zero_transaction_reference_is_rejected_as_invalid():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log()))
    with pytest.raises(InvalidTransactionReference):
        verifier.verify(_evidence(transaction_reference="0x" + "0" * 64))


def test_missing_receipt_is_rejected():
    verifier, _ = _verifier(receipt=None)
    with pytest.raises(TransactionNotFound):
        verifier.verify(_evidence())


def test_reverted_receipt_is_rejected():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log(), status=0))
    with pytest.raises(TransactionReverted):
        verifier.verify(_evidence())


def test_insufficient_confirmations_is_rejected():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log()), latest=BLOCK_NUMBER)
    with pytest.raises(InsufficientConfirmations) as exc_info:
        verifier.verify(_evidence())
    assert exc_info.value.confirmations == 1
    assert exc_info.value.required == 2


def test_wrong_x402_network_is_rejected():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log()))
    with pytest.raises(WrongPaymentNetwork):
        verifier.verify(_evidence(network="eip155:8453"))


def test_non_arc_service_snapshot_is_rejected():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log()))
    with pytest.raises(WrongPaymentNetwork):
        verifier.verify(_evidence(service_chain=Chain.BASE))


def test_wrong_x402_asset_is_rejected():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log()))
    with pytest.raises(WrongPaymentAsset):
        verifier.verify(_evidence(asset=OTHER_TOKEN))


def test_x402_pay_to_must_match_snapshotted_sla_provider():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log()))
    with pytest.raises(ProviderBindingMismatch):
        verifier.verify(_evidence(pay_to=OTHER))


def test_no_matching_usdc_transfer_is_rejected():
    verifier, _ = _verifier(receipt=_receipt())
    with pytest.raises(PaymentTransferNotFound):
        verifier.verify(_evidence())


def test_wrong_transfer_amount_is_rejected():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log(amount=AMOUNT + 1)))
    with pytest.raises(PaymentAmountMismatch):
        verifier.verify(_evidence())


def test_wrong_transfer_recipient_is_rejected():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log(recipient=OTHER)))
    with pytest.raises(PaymentTransferNotFound):
        verifier.verify(_evidence())


def test_transfer_from_zero_address_is_not_a_payment():
    verifier, _ = _verifier(
        receipt=_receipt(_transfer_log(sender="0x" + "0" * 40))
    )
    with pytest.raises(PaymentTransferNotFound):
        verifier.verify(_evidence())


def test_transfer_from_unexpected_token_contract_is_ignored():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log(token=OTHER_TOKEN)))
    with pytest.raises(PaymentTransferNotFound):
        verifier.verify(_evidence())


def test_multiple_matching_transfers_are_rejected_as_ambiguous():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log(), _transfer_log()))
    with pytest.raises(AmbiguousPaymentTransfer):
        verifier.verify(_evidence())


def test_matching_transfer_derives_canonical_payment_facts():
    verifier, eth = _verifier(receipt=_receipt(_transfer_log()))
    evidence = _evidence(service_price="123456.00")
    original_evidence = evidence.model_dump(mode="json")
    verified = verifier.verify(evidence)

    assert verified.payment_id == PAYMENT_ID
    assert verified.payment_id_hash == payment_id_hash(PAYMENT_ID)
    assert verified.transaction_hash == TX_HASH
    assert verified.block_number == BLOCK_NUMBER
    assert verified.block_timestamp == BLOCK_TIMESTAMP
    assert verified.confirmations == 2
    assert verified.buyer == BUYER
    assert verified.provider == PROVIDER
    assert verified.asset == ARC_TESTNET_USDC
    assert verified.amount_atomic == AMOUNT
    assert verified.chain_id == ARC_TESTNET_CHAIN_ID
    assert verified.evidence_hash == evidence.evidence_hash
    assert evidence.model_dump(mode="json") == original_evidence
    assert eth.block_requests == [BLOCK_NUMBER]


def test_unknown_local_status_can_still_verify_economic_settlement():
    verifier, _ = _verifier(receipt=_receipt(_transfer_log()))
    verified = verifier.verify(_evidence(payment_status="unknown"))
    assert verified.amount_atomic == AMOUNT
    assert verified.buyer == BUYER


def test_success_local_status_without_transfer_is_not_eligible():
    verifier, _ = _verifier(receipt=_receipt())
    evidence = _evidence(payment_status="success")
    with pytest.raises(PaymentTransferNotFound):
        verifier.verify(evidence)
