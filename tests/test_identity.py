"""
Tests for ERC-8004 identity + reputation (arc_agent_pay.identity).

Fake contracts are injected, so no web3 install or chain access is needed. We
verify read methods, address→id resolution, reputation summary scaling, profile
assembly, and that writes refuse to run without a funded account.
"""

from __future__ import annotations

import pytest

from arc_agent_pay.identity import AgentIdentity, ReputationClient, ValidationClient
from arc_agent_pay.onchain import (
    identity_registry_address,
    load_abi,
    reputation_registry_address,
)

ZERO = "0x0000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Fake web3 contract scaffolding
# ---------------------------------------------------------------------------

class _Call:
    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


class _IdentityFns:
    def __init__(self, owners, uris):
        self._owners = owners
        self._uris = uris

    def ownerOf(self, agent_id):
        return _Call(self._owners[agent_id])

    def tokenURI(self, agent_id):
        return _Call(self._uris.get(agent_id, ""))


class _TransferEvent:
    def __init__(self, logs):
        self._logs = logs

    def get_logs(self, from_block=0, to_block=None, argument_filters=None):
        af = argument_filters or {}
        to = af.get("to")
        return [lg for lg in self._logs if lg["args"]["to"] == to]


class _FakeEth:
    block_number = 100


class _FakeW3:
    eth = _FakeEth()


class _Events:
    def __init__(self, logs):
        self._logs = logs

    def Transfer(self):
        return _TransferEvent(self._logs)


class _IdentityContract:
    def __init__(self, owners, uris, logs):
        self.functions = _IdentityFns(owners, uris)
        self.events = _Events(logs)


class _ReputationFns:
    def __init__(self, summaries, clients=None):
        self._summaries = summaries
        self._clients = clients or {}

    def getClients(self, agent_id):
        return _Call(self._clients.get(agent_id, []))

    def getSummary(self, agent_id, clients, tag1, tag2):
        return _Call(self._summaries[agent_id])


class _ReputationContract:
    def __init__(self, summaries, clients=None):
        self.functions = _ReputationFns(summaries, clients)


_ZERO_RECORD = (ZERO, 0, 0, b"\x00" * 32, "", 0)


class _ValidationFns:
    def __init__(self, records, agent_hashes, summaries):
        # records: {request_hash_bytes: (validator, agentId, response, responseHash, tag, lastUpdate)}
        # This deployment has no requestExists(); existence is inferred from the
        # validator address on getValidationStatus (zero address == no request).
        self._records = records
        self._agent_hashes = agent_hashes  # {agentId: [request_hash_bytes, ...]}
        self._summaries = summaries  # {agentId: (count, avg)}

    def getValidationStatus(self, request_hash):
        return _Call(self._records.get(request_hash, _ZERO_RECORD))

    def getSummary(self, agent_id, validators, tag):
        return _Call(self._summaries.get(agent_id, (0, 0)))

    def getAgentValidations(self, agent_id):
        return _Call(self._agent_hashes.get(agent_id, []))


class _ValidationContract:
    def __init__(self, records=None, agent_hashes=None, summaries=None):
        self.functions = _ValidationFns(records or {}, agent_hashes or {}, summaries or {})


# ---------------------------------------------------------------------------
# Packaged config
# ---------------------------------------------------------------------------

def test_addresses_loaded_and_overridable(monkeypatch):
    assert identity_registry_address().startswith("0x8004A8")
    assert reputation_registry_address().startswith("0x8004B6")
    monkeypatch.setenv("ERC8004_IDENTITY_REGISTRY", "0xdeadbeef")
    assert identity_registry_address() == "0xdeadbeef"


def test_abis_have_expected_functions():
    id_fns = {e.get("name") for e in load_abi("identity_registry")}
    assert {"register", "ownerOf", "tokenURI"} <= id_fns
    rep_fns = {e.get("name") for e in load_abi("reputation_registry")}
    assert {"giveFeedback", "getSummary"} <= rep_fns


# ---------------------------------------------------------------------------
# Identity reads
# ---------------------------------------------------------------------------

def test_owner_of_and_token_uri():
    contract = _IdentityContract(owners={1: "0xabc"}, uris={1: "ipfs://meta"}, logs=[])
    ident = AgentIdentity(contract=contract)
    assert ident.owner_of(1) == "0xabc"
    assert ident.token_uri(1) == "ipfs://meta"


def test_resolve_returns_latest_minted_id():
    logs = [
        {"args": {"from": ZERO, "to": "0xUser", "tokenId": 7}},
        {"args": {"from": ZERO, "to": "0xUser", "tokenId": 42}},
        {"args": {"from": ZERO, "to": "0xOther", "tokenId": 9}},
    ]
    ident = AgentIdentity(contract=_IdentityContract({}, {}, logs), w3=_FakeW3())
    assert ident.resolve("0xUser") == 42
    assert ident.resolve("0xNobody") is None


# ---------------------------------------------------------------------------
# Reputation reads
# ---------------------------------------------------------------------------

def test_reputation_summary_scales_by_decimals():
    rep = ReputationClient(contract=_ReputationContract({1: (5, 450, 2)}, clients={1: ["0xc"]}))
    count, score = rep.summary(1)
    assert count == 5
    assert score == pytest.approx(4.5)


def test_reputation_summary_zero_when_no_clients():
    # No feedback yet -> getClients returns [] -> (0, 0.0) without calling getSummary
    # (Arc's getSummary reverts on an empty clientAddresses list).
    rep = ReputationClient(contract=_ReputationContract({}, clients={}))
    assert rep.summary(999) == (0, 0.0)


def test_profile_combines_identity_and_reputation():
    ident = AgentIdentity(contract=_IdentityContract({1: "0xowner"}, {1: "ipfs://m"}, []))
    rep = ReputationClient(contract=_ReputationContract({1: (3, 900, 2)}, clients={1: ["0xc"]}))
    profile = ident.profile(1, reputation=rep)
    assert profile.agent_id == 1
    assert profile.address == "0xowner"
    assert profile.feedback_count == 3
    assert profile.reputation_score == pytest.approx(9.0)


# ---------------------------------------------------------------------------
# Writes require an account
# ---------------------------------------------------------------------------

def test_register_requires_account():
    ident = AgentIdentity(contract=_IdentityContract({}, {}, []))
    with pytest.raises(ValueError):
        ident.register("ipfs://meta")


def test_give_feedback_requires_account():
    rep = ReputationClient(contract=_ReputationContract({}))
    with pytest.raises(ValueError):
        rep.give_feedback(1, 5)


# ---------------------------------------------------------------------------
# Validation reads
# ---------------------------------------------------------------------------

RH = b"\x11" * 32
RH_HEX = "0x" + "11" * 32
_REC = ("0xValidator", 838889, 100, b"\x00" * 32, "arc-agent-pay", 1720000000)


def test_validation_request_exists():
    val = ValidationClient(contract=_ValidationContract(records={RH: _REC}))
    assert val.request_exists(RH) is True
    assert val.request_exists(RH_HEX) is True  # hex string accepted
    assert val.request_exists(b"\x22" * 32) is False  # zero record -> no request


def test_validation_status_returns_record():
    val = ValidationClient(contract=_ValidationContract(records={RH: _REC}))
    status = val.status(RH)
    assert status["validator_address"] == "0xValidator"
    assert status["agent_id"] == 838889
    assert status["response"] == 100
    assert status["tag"] == "arc-agent-pay"
    assert status["responded"] is True
    assert status["response_hash"] == "0x" + "00" * 32


def test_validation_status_none_when_missing():
    # No record -> zero validator address -> status() returns None.
    val = ValidationClient(contract=_ValidationContract(records={}))
    assert val.status(RH) is None
    assert val.request_exists(RH) is False


def test_validation_summary_zero_without_validators():
    # No validator addresses to aggregate over -> (0, 0) without hitting getSummary.
    val = ValidationClient(contract=_ValidationContract(summaries={1: (5, 90)}))
    assert val.summary(1, []) == (0, 0)


def test_validation_summary_over_validators():
    val = ValidationClient(contract=_ValidationContract(summaries={1: (5, 90)}))
    count, avg = val.summary(1, ["0xValidator"])
    assert count == 5
    assert avg == 90


def test_agent_validations_returns_hex():
    val = ValidationClient(
        contract=_ValidationContract(agent_hashes={1: [b"\x11" * 32, b"\x22" * 32]})
    )
    assert val.agent_validations(1) == ["0x" + "11" * 32, "0x" + "22" * 32]


# ---------------------------------------------------------------------------
# Validation writes require an account / validate inputs
# ---------------------------------------------------------------------------

def test_request_validation_requires_account():
    val = ValidationClient(contract=_ValidationContract())
    with pytest.raises(ValueError):
        val.request_validation(validator_address="0xV", agent_id=1, request_hash=RH)


def test_respond_requires_account():
    val = ValidationClient(contract=_ValidationContract())
    with pytest.raises(ValueError):
        val.respond(request_hash=RH, response=100)


def test_respond_rejects_out_of_range_score():
    # Give an account so we get past the account guard and hit the range check.
    val = ValidationClient(contract=_ValidationContract(), account=object())
    with pytest.raises(ValueError):
        val.respond(request_hash=RH, response=101)


def test_bad_hash_length_rejected():
    val = ValidationClient(contract=_ValidationContract(records={}))
    with pytest.raises(ValueError):
        val.request_exists(b"\x11" * 4)
