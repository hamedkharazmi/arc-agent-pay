"""Studio-dev transport retries and the no-retry submission boundary."""

from __future__ import annotations

from types import SimpleNamespace
import ssl

import pytest
from requests import exceptions as requests_exceptions

from arc_agent_pay.assurance.genlayer import GenLayerAdjudicationClient
from arc_agent_pay.assurance.genlayer_live import (
    GenLayerFeeEstimate,
    submit_genlayer_deployment,
)
from arc_agent_pay.assurance.genlayer_transport import (
    GenLayerRetryPolicy,
    SAFE_STUDIO_DEV_RPC_METHODS,
    install_studio_dev_retries,
)
from arc_agent_pay.exceptions import (
    GenLayerFinalityTimeout,
    GenLayerSubmissionOutcomeUnknown,
    GenLayerTransportRetryExhausted,
)

TX_ID = "0x" + "12" * 32
EVM_TX_ID = "0x" + "34" * 32
HASH = "0x" + "56" * 32
CONTRACT = "0x" + "78" * 20
OPERATOR = "0x" + "9a" * 20
SOURCE = (
    "# v0.3.0\n"
    '# { "Depends": '
    '"py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng" }\n\n'
    "import genlayer as gl\n"
)


def tls_eof() -> BaseException:
    low_level = ssl.SSLEOFError(8, "UNEXPECTED_EOF_WHILE_READING")
    ssl_error = requests_exceptions.SSLError("TLS EOF")
    ssl_error.__cause__ = low_level
    wrapped = RuntimeError("GenLayer provider request failed")
    wrapped.__cause__ = ssl_error
    return wrapped


class SequenceProvider:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def make_request(self, method, params):
        self.calls.append((str(method), params))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def retrying_client(results):
    provider = SequenceProvider(results)
    client = SimpleNamespace(provider=provider)
    sleeps = []
    install_studio_dev_retries(
        client,
        policy=GenLayerRetryPolicy(
            max_attempts=4,
            base_delay_seconds=0.5,
            max_delay_seconds=4.0,
            max_jitter_seconds=0.0,
        ),
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )
    return client, provider, sleeps


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("eth_getTransactionCount", {"result": "0x3"}),
        ("eth_estimateGas", {"result": "0x5208"}),
    ],
)
def test_nonce_and_estimate_gas_retry_tls_eof_then_succeed(method, expected):
    client, provider, sleeps = retrying_client([tls_eof(), expected])

    result = client.provider.make_request(method, ["request"])

    assert result == expected
    assert [call[0] for call in provider.calls] == [method, method]
    assert sleeps == [0.5]


def test_balance_read_retries_connection_failure_then_succeeds():
    error = requests_exceptions.ConnectionError("connection reset by peer")
    expected = {"result": hex(10**19)}
    client, provider, sleeps = retrying_client([error, expected])

    assert client.provider.make_request("eth_getBalance", [OPERATOR, "latest"]) == expected
    assert len(provider.calls) == 2
    assert sleeps == [0.5]


def test_finality_transaction_lookup_retries_tls_failure_then_succeeds():
    expected = {"result": {"lifecycle": {"state": "finalized"}}}
    client, provider, _ = retrying_client([tls_eof(), expected])

    result = client.provider.make_request("eth_getTransactionByHash", [TX_ID])

    assert result == expected
    assert len(provider.calls) == 2


def test_read_contract_retries_transport_failure_then_succeeds():
    expected = {"result": "encoded-verdict"}
    client, provider, _ = retrying_client(
        [requests_exceptions.Timeout("read timeout"), expected]
    )

    result = client.provider.make_request("gen_call", [{"type": "read"}])

    assert result == expected
    assert len(provider.calls) == 2


def test_deterministic_json_rpc_error_is_not_retried():
    error = RuntimeError("eth_estimateGas failed (code=-32000): execution reverted")
    client, provider, sleeps = retrying_client([error])

    with pytest.raises(RuntimeError, match="execution reverted"):
        client.provider.make_request("eth_estimateGas", [{}])

    assert len(provider.calls) == 1
    assert sleeps == []


def test_invalid_payload_application_error_is_not_retried():
    client, provider, sleeps = retrying_client([ValueError("invalid payload")])

    with pytest.raises(ValueError, match="invalid payload"):
        client.provider.make_request("gen_call", [{}])

    assert len(provider.calls) == 1
    assert sleeps == []


def test_retry_exhaustion_is_typed_and_preserves_original_cause():
    failures = [tls_eof() for _ in range(4)]
    client, provider, sleeps = retrying_client(failures)

    with pytest.raises(GenLayerTransportRetryExhausted) as raised:
        client.provider.make_request("eth_getTransactionCount", [OPERATOR, "pending"])

    assert raised.value.rpc_method == "eth_getTransactionCount"
    assert raised.value.attempts == 4
    assert raised.value.__cause__ is failures[-1]
    assert len(provider.calls) == 4
    assert sleeps == [0.5, 1.0, 2.0]


def test_submission_transport_failure_is_attempted_exactly_once():
    client, provider, sleeps = retrying_client([tls_eof()])

    with pytest.raises(GenLayerSubmissionOutcomeUnknown, match="blind resubmission"):
        client.provider.make_request("eth_sendRawTransaction", ["signed-transaction"])

    assert len(provider.calls) == 1
    assert sleeps == []


class AmbiguousAdjudicationClient:
    def __init__(self):
        self.write_calls = 0

    def write_contract(self, **_kwargs):
        self.write_calls += 1
        raise requests_exceptions.ConnectionError("connection lost during submission")


def test_adjudication_submission_transport_failure_calls_write_once():
    raw_client = AmbiguousAdjudicationClient()
    adapter = GenLayerAdjudicationClient(
        contract_address=CONTRACT,
        client=raw_client,
    )
    payload = SimpleNamespace(
        size_bytes=1,
        canonical="{}",
        dispute_id=HASH,
        input_hash=HASH,
    )

    with pytest.raises(GenLayerSubmissionOutcomeUnknown, match="blind resubmission"):
        adapter.submit_adjudication(payload)

    assert raw_client.write_calls == 1


class ExhaustedPreflightClient:
    def __init__(self, exhausted):
        self.exhausted = exhausted
        self.write_calls = 0

    def write_contract(self, **_kwargs):
        self.write_calls += 1
        wrapped = RuntimeError("SDK transaction preparation failed")
        wrapped.__cause__ = self.exhausted
        raise wrapped


def test_pre_submission_retry_exhaustion_stays_typed_not_ambiguous():
    root = requests_exceptions.ConnectionError("nonce endpoint unavailable")
    exhausted = GenLayerTransportRetryExhausted("eth_getTransactionCount", 4)
    exhausted.__cause__ = root
    raw_client = ExhaustedPreflightClient(exhausted)
    adapter = GenLayerAdjudicationClient(
        contract_address=CONTRACT,
        client=raw_client,
    )
    payload = SimpleNamespace(
        size_bytes=1,
        canonical="{}",
        dispute_id=HASH,
        input_hash=HASH,
    )

    with pytest.raises(GenLayerTransportRetryExhausted) as raised:
        adapter.submit_adjudication(payload)

    assert raised.value is exhausted
    assert raised.value.__cause__ is root
    assert raw_client.write_calls == 1


class AdapterClient:
    def __init__(self):
        self.write_calls = 0
        self.wait_calls = 0
        self.get_calls = 0

    def write_contract(self, **_kwargs):
        self.write_calls += 1
        return bytes.fromhex(TX_ID[2:])

    def wait_for_finalization(self, **_kwargs):
        self.wait_calls += 1
        raise requests_exceptions.ConnectionError("polling transport exhausted")

    def get_transaction(self, **_kwargs):
        self.get_calls += 1
        return {"lifecycle": {"state": "processing"}}


def test_transaction_id_then_polling_failure_preserves_resume_without_resubmission():
    raw_client = AdapterClient()
    adapter = GenLayerAdjudicationClient(
        contract_address=CONTRACT,
        client=raw_client,
    )
    payload = SimpleNamespace(
        size_bytes=1,
        canonical="{}",
        dispute_id=HASH,
        input_hash=HASH,
    )
    submission = adapter.submit_adjudication(payload)

    with pytest.raises(GenLayerFinalityTimeout) as raised:
        adapter.wait_for_finalized_verdict(submission, payload)

    assert raised.value.transaction_id == TX_ID
    assert raw_client.write_calls == 1
    assert raw_client.wait_calls == 1
    assert raw_client.get_calls == 1


class AmbiguousDeploymentClient:
    def __init__(self):
        self.deploy_calls = 0

    def deploy_contract(self, **_kwargs):
        self.deploy_calls += 1
        raise requests_exceptions.ConnectionError("connection lost during submission")


def test_deployment_submission_ambiguity_is_never_retried():
    client = AmbiguousDeploymentClient()
    estimate = GenLayerFeeEstimate(
        fee_value=1,
        distribution={"rotations": [3]},
        policy={"enabled": True},
    )

    with pytest.raises(GenLayerSubmissionOutcomeUnknown, match="outcome is unknown"):
        submit_genlayer_deployment(
            client,
            source=SOURCE,
            authorized_submitter=OPERATOR,
            fee_estimate=estimate,
        )

    assert client.deploy_calls == 1


def test_normal_successful_safe_and_submission_paths_are_unchanged():
    client, provider, sleeps = retrying_client(
        [
            {"result": "0xf22d"},
            {"result": EVM_TX_ID},
        ]
    )

    assert client.provider.make_request("eth_chainId", []) == {"result": "0xf22d"}
    assert client.provider.make_request("eth_sendRawTransaction", ["signed"]) == {
        "result": EVM_TX_ID
    }
    assert len(provider.calls) == 2
    assert sleeps == []
    assert "eth_sendRawTransaction" not in SAFE_STUDIO_DEV_RPC_METHODS
