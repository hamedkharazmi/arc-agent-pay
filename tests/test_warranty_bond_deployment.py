"""Safety tests for the Arc Testnet WarrantyBond deployment tooling."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from requests import ConnectionError as RequestsConnectionError

from arc_agent_pay.exceptions import SettlementSubmissionOutcomeUnknown
from scripts import deploy_warranty_bond as deploy


class FakeConstructor:
    def __init__(self, estimate: int = 100_000) -> None:
        self.estimate = estimate
        self.arguments = None

    def estimate_gas(self, transaction):
        assert transaction == {"from": deploy.ASSURANCE_OPERATOR}
        return self.estimate


class FakeFactory:
    def __init__(self, constructor: FakeConstructor) -> None:
        self._constructor = constructor

    def constructor(self, usdc, resolver):
        self._constructor.arguments = (usdc, resolver)
        return self._constructor


class FakeEth:
    def __init__(self, *, balance: int, chain_id: int = deploy.ARC_TESTNET_CHAIN_ID):
        self.chain_id = chain_id
        self.balance = balance
        self.gas_price = 25_000_000_000
        self.constructor = FakeConstructor()

    def get_code(self, address):
        assert address == deploy.ARC_TESTNET_USDC
        return b"contract"

    def get_balance(self, address):
        assert address == deploy.ASSURANCE_OPERATOR
        return self.balance

    def get_transaction_count(self, address, block_identifier):
        assert address == deploy.ASSURANCE_OPERATOR
        assert block_identifier == "pending"
        return 7

    def contract(self, *, abi, bytecode):
        assert abi
        assert bytecode.startswith("0x")
        return FakeFactory(self.constructor)


class FakeWeb3:
    def __init__(self, eth: FakeEth) -> None:
        self.eth = eth


class FakeHash(bytes):
    def hex(self):
        return "0x" + super().hex()


class FakeSigner:
    address = deploy.ASSURANCE_OPERATOR

    def sign_transaction(self, transaction):
        return SimpleNamespace(raw_transaction=b"signed")


@pytest.fixture
def unconfigured_paths(tmp_path, monkeypatch):
    addresses = tmp_path / "addresses.json"
    addresses.write_text(
        json.dumps({"arc_testnet": {"warranty_bond": None}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(deploy, "ADDRESSES", addresses)
    monkeypatch.setattr(deploy, "ARTIFACT", tmp_path / "deployment.json")
    return addresses


def test_compilation_matches_packaged_abi_and_locked_constructor():
    first = deploy.compile_warranty_bond()
    second = deploy.compile_warranty_bond()

    assert first.source_sha256 == second.source_sha256
    assert first.creation_bytecode_sha256 == second.creation_bytecode_sha256
    constructor = next(entry for entry in first.abi if entry["type"] == "constructor")
    assert [(item["name"], item["type"]) for item in constructor["inputs"]] == [
        ("_usdc", "address"),
        ("_resolver", "address"),
    ]


def test_preflight_uses_locked_chain_addresses_and_atomic_cost(unconfigured_paths):
    compilation = deploy.compile_warranty_bond()
    eth = FakeEth(balance=10**18)

    preflight, constructor = deploy.deployment_preflight(
        FakeWeb3(eth),
        FakeSigner(),
        compilation,
    )

    assert preflight.chain_id == deploy.ARC_TESTNET_CHAIN_ID
    assert preflight.signer == deploy.ASSURANCE_OPERATOR
    assert preflight.usdc == deploy.ARC_TESTNET_USDC
    assert preflight.resolver == deploy.ASSURANCE_OPERATOR
    assert preflight.native_balance_atomic == 10**18
    assert preflight.estimated_gas == 100_000
    assert preflight.gas_limit == 120_000
    assert preflight.estimated_transaction_cost_atomic == 3_000_000_000_000_000
    assert constructor.arguments == (
        deploy.ARC_TESTNET_USDC,
        deploy.ASSURANCE_OPERATOR,
    )
    deploy._require_sufficient_funds(preflight)


def test_preflight_rejects_wrong_chain_before_deployment(unconfigured_paths):
    compilation = deploy.compile_warranty_bond()
    eth = FakeEth(balance=10**18, chain_id=1)

    with pytest.raises(RuntimeError, match="expected Arc Testnet"):
        deploy.deployment_preflight(FakeWeb3(eth), FakeSigner(), compilation)

    assert eth.constructor.arguments is None


def test_zero_or_insufficient_balance_stops_before_submission(unconfigured_paths):
    compilation = deploy.compile_warranty_bond()
    preflight, _ = deploy.deployment_preflight(
        FakeWeb3(FakeEth(balance=0)),
        FakeSigner(),
        compilation,
    )

    with pytest.raises(RuntimeError, match="operator has 0 atomic"):
        deploy._require_sufficient_funds(preflight)


def test_existing_configuration_or_artifact_refuses_redeployment(
    unconfigured_paths,
    monkeypatch,
):
    unconfigured_paths.write_text(
        json.dumps({"arc_testnet": {"warranty_bond": "0x" + "11" * 20}}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="already configured"):
        deploy._assert_no_existing_deployment({})

    unconfigured_paths.write_text(
        json.dumps({"arc_testnet": {"warranty_bond": None}}),
        encoding="utf-8",
    )
    deploy.ARTIFACT.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="artifact already exists"):
        deploy._assert_no_existing_deployment({})

    monkeypatch.setenv("WARRANTY_BOND_ADDRESS", "0x" + "22" * 20)
    with pytest.raises(RuntimeError, match="already configured"):
        deploy._assert_no_existing_deployment()


def test_address_persistence_refuses_silent_overwrite(unconfigured_paths):
    first = "0x" + "11" * 20
    deploy._persist_configured_address(first)
    assert json.loads(unconfigured_paths.read_text())["arc_testnet"]["warranty_bond"] == first

    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        deploy._persist_configured_address("0x" + "22" * 20)


def test_safe_reads_retry_transient_transport_only():
    attempts = 0
    sleeps = []

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RequestsConnectionError("temporary disconnect")
        return 5042002

    result = deploy._safe_read(
        "chain ID",
        operation,
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )

    assert result == 5042002
    assert attempts == 2
    assert len(sleeps) == 1


def test_deployment_broadcast_transport_failure_is_never_retried():
    class SendingEth:
        def __init__(self):
            self.calls = 0

        def send_raw_transaction(self, raw):
            assert raw == b"signed"
            self.calls += 1
            raise RequestsConnectionError("outcome unknown")

    eth = SendingEth()
    with pytest.raises(SettlementSubmissionOutcomeUnknown, match="outcome is unknown"):
        deploy._broadcast_once(FakeWeb3(eth), FakeSigner(), {"nonce": 7})

    assert eth.calls == 1


def test_successful_deployment_broadcast_returns_hash_once():
    class SendingEth:
        def __init__(self):
            self.calls = 0

        def send_raw_transaction(self, raw):
            assert raw == b"signed"
            self.calls += 1
            return FakeHash(b"\x99" * 32)

    eth = SendingEth()
    assert deploy._broadcast_once(FakeWeb3(eth), FakeSigner(), {"nonce": 7}) == ("0x" + "99" * 32)
    assert eth.calls == 1
