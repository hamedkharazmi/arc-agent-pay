"""Unit tests for Web3 transaction wiring around the validation escrow."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from eth_account import Account

from arc_agent_pay.exceptions import InvalidVerdictError, WorkflowError
from arc_agent_pay.onchain import load_abi
from arc_agent_pay.workflow import (
    DeliveryEvidence,
    EscrowClient,
    EscrowStatus,
    ValidationVerdict,
    WorkOrder,
    hash_content,
    sign_funding_authorization,
    sign_verdict,
)


PAYER_KEY = "0x" + "22" * 32
VALIDATOR_KEY = "0x" + "11" * 32
PAYER = Account.from_key(PAYER_KEY).address.lower()
VALIDATOR = Account.from_key(VALIDATOR_KEY).address.lower()
ESCROW = "0x" + "aa" * 20
TOKEN = "0x" + "36" + "00" * 19
PROVIDER = "0x" + "cc" * 20
NOW = 1_100


def make_order(**overrides) -> WorkOrder:
    values = {
        "escrow": ESCROW,
        "payer": PAYER,
        "provider": PROVIDER,
        "validator": VALIDATOR,
        "asset": TOKEN,
        "amount": 100_000,
        "chain_id": 5_042_002,
        "delivery_deadline": 1_500,
        "refund_after": 2_000,
        "task_hash": hash_content("task"),
        "nonce": "0x" + "01" * 32,
    }
    values.update(overrides)
    return WorkOrder(**values)


def make_delivery(order: WorkOrder) -> DeliveryEvidence:
    return DeliveryEvidence(
        order_hash=order.order_hash,
        evidence_hash=hash_content("result"),
        evidence_uri="ipfs://result",
        delivered_at=1_000,
    )


class FakeFunction:
    def __init__(self, name, args, result=None):
        self.name = name
        self.args = args
        self.result = result
        self.tx = None

    def call(self):
        return self.result

    def build_transaction(self, tx):
        self.tx = tx
        return {**tx, "to": ESCROW, "data": "0x1234"}


class FakeFunctions:
    def __init__(self):
        self.calls = []
        self.status_value = EscrowStatus.FUNDED

    def _call(self, name, *args, result=None):
        fn = FakeFunction(name, args, result)
        self.calls.append(fn)
        return fn

    def status(self, *args):
        return self._call("status", *args, result=self.status_value)

    def fund(self, *args):
        return self._call("fund", *args)

    def release(self, *args):
        return self._call("release", *args)

    def refund_rejected(self, *args):
        return self._call("refund_rejected", *args)

    def refund_timeout(self, *args):
        return self._call("refund_timeout", *args)


class FakeHash(bytes):
    def hex(self):
        return "0x" + super().hex()


class FakeEth:
    def __init__(self):
        self.nonce_requests = []
        self.sent = []
        self.receipt_status = 1

    def get_transaction_count(self, address, block_identifier):
        self.nonce_requests.append((address, block_identifier))
        return 7

    def send_raw_transaction(self, raw):
        self.sent.append(raw)
        return FakeHash(b"\x99" * 32)

    def wait_for_transaction_receipt(self, tx_hash):
        return {"status": self.receipt_status}

    def get_block(self, block_identifier):
        assert block_identifier == "latest"
        return {"timestamp": NOW}


class FakeAccount:
    address = PAYER

    def __init__(self):
        self.transactions = []

    def sign_transaction(self, tx):
        self.transactions.append(tx)
        return SimpleNamespace(raw_transaction=b"signed")


@pytest.fixture
def client():
    functions = FakeFunctions()
    contract = SimpleNamespace(functions=functions)
    w3 = SimpleNamespace(eth=FakeEth())
    account = FakeAccount()
    return EscrowClient(
        ESCROW,
        account=account,
        w3=w3,
        contract=contract,
    ), functions, w3, account


def test_status_reads_order_hash(client):
    escrow, functions, _, _ = client
    order = make_order()

    assert escrow.status(order) is EscrowStatus.FUNDED
    assert functions.calls[-1].name == "status"
    assert functions.calls[-1].args == (bytes.fromhex(order.order_hash[2:]),)


def test_packaged_abi_exposes_the_complete_state_machine():
    functions = {
        entry.get("name")
        for entry in load_abi("validation_escrow")
        if entry.get("type") == "function"
    }
    assert {
        "fund",
        "release",
        "refund_rejected",
        "refund_timeout",
        "status",
        "hash_order",
        "hash_delivery",
    } <= functions


def test_fund_verifies_signature_and_uses_pending_nonce(client):
    escrow, functions, w3, account = client
    order = make_order()
    authorization = sign_funding_authorization(order, private_key=PAYER_KEY)

    tx_hash = escrow.fund(order, authorization)

    assert tx_hash == "0x" + "99" * 32
    assert functions.calls[-1].name == "fund"
    assert w3.eth.nonce_requests == [(PAYER, "pending")]
    assert account.transactions[-1]["nonce"] == 7


def test_release_and_rejected_refund_apply_local_verdict_policy(client):
    escrow, functions, _, _ = client
    order = make_order()
    delivery = make_delivery(order)

    approved = ValidationVerdict.for_delivery(
        delivery,
        approved=True,
        score=95,
        reason="accepted",
        issued_at=1_050,
        valid_until=1_800,
    )
    signed_approved = sign_verdict(approved, private_key=VALIDATOR_KEY, order=order)
    escrow.release(order, delivery, signed_approved)
    assert functions.calls[-1].name == "release"
    with pytest.raises(InvalidVerdictError, match="approved verdict"):
        escrow.refund_rejected(order, delivery, signed_approved)

    rejected = ValidationVerdict.for_delivery(
        delivery,
        approved=False,
        score=10,
        reason="rejected",
        issued_at=1_050,
        valid_until=1_800,
    )
    signed_rejected = sign_verdict(rejected, private_key=VALIDATOR_KEY, order=order)
    escrow.refund_rejected(order, delivery, signed_rejected)
    assert functions.calls[-1].name == "refund_rejected"
    with pytest.raises(InvalidVerdictError, match="rejected"):
        escrow.release(order, delivery, signed_rejected)


def test_writes_reject_wrong_contract_and_missing_dependencies(client):
    escrow, _, _, _ = client
    wrong = make_order(escrow="0x" + "ab" * 20)
    with pytest.raises(WorkflowError, match="different escrow"):
        escrow.refund_timeout(wrong)

    no_signer = EscrowClient(
        ESCROW,
        contract=SimpleNamespace(functions=FakeFunctions()),
    )
    with pytest.raises(WorkflowError, match="account and Web3"):
        no_signer.refund_timeout(make_order())


def test_reverted_receipt_is_not_reported_as_success(client):
    escrow, _, w3, _ = client
    w3.eth.receipt_status = 0
    with pytest.raises(WorkflowError, match="reverted on-chain"):
        escrow.refund_timeout(make_order())
