"""Unit tests for WarrantyBond Web3 transaction wiring and validation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arc_agent_pay.assurance import BondTransactionResult, WarrantyBondClient
from arc_agent_pay.exceptions import SettlementSubmissionOutcomeUnknown, WorkflowError
from arc_agent_pay.onchain import load_abi, warranty_bond_address


BOND = "0x" + "aa" * 20
PROVIDER = "0x" + "bb" * 20
BUYER = "0x" + "cc" * 20
DISPUTE_ID = "0x" + "11" * 32
PAYMENT_ID_HASH = "0x" + "22" * 32


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
        return {**tx, "to": BOND, "data": "0x1234"}


class FakeFunctions:
    def __init__(self):
        self.calls = []
        self.balance = 500_000
        self.resolver_address = PROVIDER
        self.usdc_address = "0x3600000000000000000000000000000000000000"
        self.processed = False
        self.refunded = False

    def _call(self, name, *args, result=None):
        fn = FakeFunction(name, args, result)
        self.calls.append(fn)
        return fn

    def bond_balance(self, *args):
        return self._call("bond_balance", *args, result=self.balance)

    def deposit(self, *args):
        return self._call("deposit", *args)

    def refund(self, *args):
        return self._call("refund", *args)

    def resolver(self):
        return self._call("resolver", result=self.resolver_address)

    def usdc(self):
        return self._call("usdc", result=self.usdc_address)

    def processed_disputes(self, *args):
        return self._call("processed_disputes", *args, result=self.processed)

    def refunded_payments(self, *args):
        return self._call("refunded_payments", *args, result=self.refunded)


class FakeHash(bytes):
    def hex(self):
        return "0x" + super().hex()


class FakeEth:
    def __init__(self):
        self.nonce_requests = []
        self.sent = []
        self.receipt_status = 1
        self.send_error = None
        self.receipt_requests = []
        self.chain_id = 5_042_002

    def get_transaction_count(self, address, block_identifier):
        self.nonce_requests.append((address, block_identifier))
        return 9

    def send_raw_transaction(self, raw):
        self.sent.append(raw)
        if self.send_error:
            raise self.send_error
        return FakeHash(b"\x99" * 32)

    def wait_for_transaction_receipt(self, tx_hash):
        self.receipt_requests.append(tx_hash)
        return {
            "status": self.receipt_status,
            "blockNumber": 123,
            "gasUsed": 45_678,
        }


class FakeAccount:
    address = PROVIDER

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
    return (
        WarrantyBondClient(
            BOND,
            account=account,
            w3=w3,
            contract=contract,
        ),
        functions,
        w3,
        account,
    )


def test_packaged_abi_exposes_locked_mvp_contract_api():
    entries = load_abi("warranty_bond")
    functions = {
        entry.get("name") for entry in entries if entry.get("type") == "function"
    }
    events = {entry.get("name") for entry in entries if entry.get("type") == "event"}
    assert {
        "usdc",
        "resolver",
        "bond_balance",
        "deposit",
        "refund",
        "processed_disputes",
        "refunded_payments",
    } <= functions
    assert {"BondDeposited", "WarrantyRefunded"} <= events


def test_configuration_contains_confirmed_arc_testnet_deployment(monkeypatch):
    monkeypatch.delenv("WARRANTY_BOND_ADDRESS", raising=False)
    assert (
        warranty_bond_address()
        == "0x74c4b5006136d37134B60F4Ff952E446BC11C901"
    )


def test_bond_balance_validates_and_normalizes_provider(client):
    bond, functions, _, _ = client
    assert bond.bond_balance(PROVIDER.upper().replace("0X", "0x")) == 500_000
    assert functions.calls[-1].name == "bond_balance"
    assert functions.calls[-1].args[0].lower() == PROVIDER
    with pytest.raises(ValueError, match="EVM address"):
        bond.bond_balance("not-an-address")


def test_deposit_sends_and_returns_confirmed_receipt_metadata(client):
    bond, functions, w3, account = client
    result = bond.deposit(100_000)

    assert isinstance(result, BondTransactionResult)
    assert result.tx_hash == "0x" + "99" * 32
    assert result.block_number == 123
    assert result.gas_used == 45_678
    assert result.receipt["status"] == 1
    assert functions.calls[-1].name == "deposit"
    assert functions.calls[-1].args == (100_000,)
    assert w3.eth.nonce_requests == [(PROVIDER, "pending")]
    assert account.transactions[-1]["nonce"] == 9


@pytest.mark.parametrize("amount", [0, -1, True, "100"])
def test_deposit_rejects_non_positive_or_non_integer_amount(client, amount):
    bond, functions, _, _ = client
    with pytest.raises(ValueError, match="amount"):
        bond.deposit(amount)
    assert functions.calls == []


def test_refund_encodes_validated_bytes32_addresses_and_amount(client):
    bond, functions, _, _ = client
    result = bond.refund(
        DISPUTE_ID.upper().replace("0X", "0x"),
        PAYMENT_ID_HASH,
        PROVIDER,
        BUYER,
        125_000,
    )

    assert result.block_number == 123
    assert functions.calls[-1].name == "refund"
    dispute, payment, provider, buyer, amount = functions.calls[-1].args
    assert dispute == bytes.fromhex(DISPUTE_ID[2:])
    assert payment == bytes.fromhex(PAYMENT_ID_HASH[2:])
    assert provider.lower() == PROVIDER
    assert buyer.lower() == BUYER
    assert amount == 125_000


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("dispute_id", "0x" + "0" * 64, "zero bytes32"),
        ("payment_id_hash", "bad", "32-byte"),
        ("provider", "bad", "EVM address"),
        ("buyer", "0x" + "0" * 40, "zero address"),
        ("payment_amount", 0, "amount"),
    ],
)
def test_refund_rejects_invalid_inputs_before_building_transaction(
    client, field, value, message
):
    bond, functions, _, _ = client
    values = {
        "dispute_id": DISPUTE_ID,
        "payment_id_hash": PAYMENT_ID_HASH,
        "provider": PROVIDER,
        "buyer": BUYER,
        "payment_amount": 125_000,
    }
    values[field] = value
    with pytest.raises(ValueError, match=message):
        bond.refund(**values)
    assert functions.calls == []


def test_write_requires_account_and_web3():
    no_signer = WarrantyBondClient(
        BOND,
        contract=SimpleNamespace(functions=FakeFunctions()),
    )
    with pytest.raises(WorkflowError, match="account and Web3"):
        no_signer.deposit(1)


def test_reverted_receipt_is_not_reported_as_success(client):
    bond, _, w3, _ = client
    w3.eth.receipt_status = 0
    with pytest.raises(WorkflowError, match="reverted on-chain"):
        bond.deposit(1)


def test_phase4_view_helpers_expose_replay_and_configuration(client):
    bond, functions, _, _ = client
    functions.processed = True
    functions.refunded = True

    assert bond.chain_id() == 5_042_002
    assert bond.resolver() == PROVIDER
    assert bond.usdc() == "0x3600000000000000000000000000000000000000"
    assert bond.dispute_processed(DISPUTE_ID) is True
    assert bond.payment_refunded(PAYMENT_ID_HASH) is True


def test_submit_refund_returns_hash_before_receipt_polling(client):
    bond, _, w3, _ = client

    tx_hash = bond.submit_refund(
        DISPUTE_ID,
        PAYMENT_ID_HASH,
        PROVIDER,
        BUYER,
        125_000,
    )

    assert tx_hash == "0x" + "99" * 32
    assert len(w3.eth.sent) == 1
    assert w3.eth.receipt_requests == []


def test_submit_refund_transport_ambiguity_is_never_retried(client):
    bond, _, w3, _ = client
    from requests import exceptions as requests_exceptions

    w3.eth.send_error = requests_exceptions.ConnectionError("connection reset")
    with pytest.raises(SettlementSubmissionOutcomeUnknown, match="blind resubmission"):
        bond.submit_refund(
            DISPUTE_ID,
            PAYMENT_ID_HASH,
            PROVIDER,
            BUYER,
            125_000,
        )
    assert len(w3.eth.sent) == 1
