"""Local tests for safe Studio-dev deployment and smoke-test tooling."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from arc_agent_pay.assurance.genlayer_live import (
    FinalizedGenLayerDeployment,
    GenLayerFeeEstimate,
    GENVM_CONTRACT_RELEASE,
    GENVM_RUNNER_IDENTIFIER,
    STUDIO_DEV_CHAIN_ID,
    STUDIO_DEV_NETWORK,
    _execution_result,
    _is_successful_transaction,
    build_deployment_artifact,
    contract_source_hash,
    contract_source_metadata,
    estimate_transaction_fees,
    finalize_and_persist_deployment,
    load_deployment_artifact,
    load_operator_account,
    persist_deployment_artifact,
    require_operator_balance,
    submit_genlayer_deployment,
    verify_studio_dev_network,
)
from arc_agent_pay.exceptions import (
    GenLayerDeploymentArtifactError,
    GenLayerDeploymentError,
    GenLayerExecutionFailed,
    GenLayerFeeEstimationFailed,
    GenLayerFinalityTimeout,
    GenLayerInsufficientBalance,
    GenLayerNetworkMismatch,
)
from scripts.smoke_genlayer_assurance import build_synthetic_smoke_payload

TX_ID = "0x" + "12" * 32
CONTRACT = "0x" + "34" * 20
OPERATOR = "0x" + "56" * 20
RUNNER = GENVM_RUNNER_IDENTIFIER
SOURCE = (
    f"# {GENVM_CONTRACT_RELEASE}\n"
    f'# {{ "Depends": "{RUNNER}" }}\n\n'
    "import genlayer as gl\n"
)
LIVE_SHAPES_PATH = (
    Path(__file__).parent / "fixtures" / "genlayer_finalized_transactions.json"
)


def finalized_transaction(*, execution: str = "FINISHED_WITH_RETURN"):
    return {
        "lifecycle": {"state": "finalized", "outcome": "accepted"},
        "tx_execution_result_name": execution,
        "recipient": CONTRACT,
    }


class FakeClient:
    def __init__(
        self,
        *,
        chain_id=STUDIO_DEV_CHAIN_ID,
        fee_estimate=None,
        fee_error=None,
        wait_result=None,
        wait_error=None,
        latest=None,
        balance=0,
        balance_error=None,
    ):
        self.chain_id = chain_id
        self.fee_estimate = fee_estimate or {
            "distribution": {
                "leaderTimeunitsAllocation": 100,
                "validatorTimeunitsAllocation": 200,
                "appealRounds": 0,
                "executionBudgetPerRound": 123,
                "rotations": [3],
            },
            "feeValue": 999,
            "policy": {"enabled": True},
        }
        self.fee_error = fee_error
        self.wait_result = wait_result
        self.wait_error = wait_error
        self.latest = latest
        self.balance = balance
        self.balance_error = balance_error
        self.balance_calls = []
        self.w3 = SimpleNamespace(eth=SimpleNamespace(get_balance=self.get_balance))
        self.deploy_calls = []

    def estimate_transaction_fees(self):
        if self.fee_error:
            raise self.fee_error
        return self.fee_estimate

    def deploy_contract(self, **kwargs):
        self.deploy_calls.append(kwargs)
        return bytes.fromhex(TX_ID[2:])

    def wait_for_finalization(self, **_kwargs):
        if self.wait_error:
            raise self.wait_error
        return self.wait_result

    def get_transaction(self, **_kwargs):
        return self.latest

    def get_balance(self, address, block_identifier):
        self.balance_calls.append((address, block_identifier))
        if self.balance_error:
            raise self.balance_error
        return self.balance


def test_missing_operator_key_is_rejected_without_generating_one():
    with pytest.raises(GenLayerDeploymentError, match="is required"):
        load_operator_account({})


def test_wrong_studio_dev_chain_is_rejected():
    with pytest.raises(GenLayerNetworkMismatch, match="expected Studio-dev"):
        verify_studio_dev_network(FakeClient(chain_id=61_999))


def test_fee_estimation_failure_stops_before_submission():
    client = FakeClient(fee_error=RuntimeError("method unavailable"))

    with pytest.raises(GenLayerFeeEstimationFailed, match="no transaction"):
        estimate_transaction_fees(client)
    assert client.deploy_calls == []


def test_successful_native_balance_rpc_query_uses_latest_and_atomic_integer():
    client = FakeClient(balance=1_000)
    estimate = estimate_transaction_fees(client)
    assert require_operator_balance(client, OPERATOR, estimate) == 1_000
    assert client.balance_calls == [(OPERATOR, "latest")]


def test_checksum_operator_is_preserved_at_balance_and_deployment_boundaries():
    checksum_operator = "0xAD9B0B4EDa94329c72837263979b0fD3Be4BCD18"
    client = FakeClient(balance=10**19)
    estimate = estimate_transaction_fees(client)

    assert require_operator_balance(client, checksum_operator, estimate) == 10**19
    assert client.balance_calls == [(checksum_operator, "latest")]

    submitter_client = FakeClient()
    submit_genlayer_deployment(
        submitter_client,
        source=SOURCE,
        authorized_submitter=checksum_operator,
        fee_estimate=estimate,
    )
    assert submitter_client.deploy_calls[0]["args"] == [checksum_operator]


def test_zero_native_balance_is_reported_as_insufficient():
    client = FakeClient(balance=0)
    estimate = estimate_transaction_fees(client)
    with pytest.raises(GenLayerInsufficientBalance, match="has 0 atomic GEN"):
        require_operator_balance(client, OPERATOR, estimate)


def test_sufficient_native_balance_is_compared_as_integer_atomic_units():
    client = FakeClient(balance=999)
    estimate = estimate_transaction_fees(client)
    assert require_operator_balance(client, OPERATOR, estimate) == 999


def test_insufficient_native_balance_is_rejected_without_float_conversion():
    client = FakeClient(balance=998)
    estimate = estimate_transaction_fees(client)
    with pytest.raises(GenLayerInsufficientBalance, match="estimated fee requires 999"):
        require_operator_balance(client, OPERATOR, estimate)


def test_malformed_or_rpc_balance_failure_uses_typed_query_error():
    estimate = estimate_transaction_fees(FakeClient())
    malformed = FakeClient(balance="not-a-number")
    with pytest.raises(GenLayerDeploymentError, match="could not query operator GEN balance"):
        require_operator_balance(malformed, OPERATOR, estimate)

    failed = FakeClient(balance_error=RuntimeError("RPC unavailable"))
    with pytest.raises(GenLayerDeploymentError, match="could not query operator GEN balance"):
        require_operator_balance(failed, OPERATOR, estimate)


def test_chain_mismatch_is_rejected_before_native_balance_query():
    client = FakeClient(chain_id=61_999, balance=10_000)
    estimate = estimate_transaction_fees(client)
    with pytest.raises(GenLayerNetworkMismatch):
        require_operator_balance(client, OPERATOR, estimate)
    assert client.balance_calls == []


def test_deployment_uses_estimated_fees_and_real_consensus():
    client = FakeClient()
    estimate = estimate_transaction_fees(client)
    transaction_id = submit_genlayer_deployment(
        client,
        source=SOURCE,
        authorized_submitter=OPERATOR,
        fee_estimate=estimate,
    )

    assert transaction_id == TX_ID
    assert client.deploy_calls == [
        {
            "code": SOURCE,
            "args": [OPERATOR],
            "leader_only": False,
            "fees": {
                "distribution": dict(estimate.distribution),
                "feeValue": estimate.fee_value,
            },
        }
    ]


def test_contract_source_metadata_requires_exact_live_header():
    version, runner = contract_source_metadata(SOURCE)

    assert SOURCE.splitlines()[:2] == [
        "# v0.3.0",
        f'# {{ "Depends": "{RUNNER}" }}',
    ]
    assert version == "v0.3.0"
    assert runner == RUNNER

    invalid_sources = [
        f'# {{ "Depends": "{RUNNER}" }}\n',
        f'# v0.2.16\n# {{ "Depends": "{RUNNER}" }}\n',
        '# v0.3.0\n# { "Depends": "py-genlayer:1jb45aa8" }\n',
        (
            f'# v0.3.0\n# {{ "Depends": "{RUNNER}" }}\n'
            "# ruff: noqa: F403, F405\n"
        ),
        f'# v0.3.0\n# {{ "Depends": "{RUNNER}" }}\nimport genlayer as gl\n',
    ]
    for invalid in invalid_sources:
        with pytest.raises(GenLayerDeploymentError, match="blank line"):
            contract_source_metadata(invalid)


def test_timeout_preserves_transaction_id_and_writes_no_artifact(tmp_path):
    path = tmp_path / "studio-dev.json"
    client = FakeClient(
        wait_error=TimeoutError("still processing"),
        latest={"lifecycle": {"state": "decided", "outcome": "accepted"}},
    )

    with pytest.raises(GenLayerFinalityTimeout) as raised:
        finalize_and_persist_deployment(
            client,
            TX_ID,
            source=SOURCE,
            authorized_submitter=OPERATOR,
            artifact_path=path,
            timeout_seconds=1,
        )

    assert raised.value.transaction_id == TX_ID
    assert raised.value.state == "decided"
    assert not path.exists()


def test_failed_execution_never_produces_artifact(tmp_path):
    path = tmp_path / "studio-dev.json"
    client = FakeClient(
        wait_result=finalized_transaction(execution="FINISHED_WITH_ERROR")
    )

    with pytest.raises(GenLayerExecutionFailed):
        finalize_and_persist_deployment(
            client,
            TX_ID,
            source=SOURCE,
            authorized_submitter=OPERATOR,
            artifact_path=path,
        )

    assert not path.exists()


def test_live_camel_case_failed_shape_is_final_but_unsuccessful(tmp_path):
    shapes = json.loads(LIVE_SHAPES_PATH.read_text())
    transaction = shapes["failed"]
    path = tmp_path / "studio-dev.json"

    assert transaction["lifecycle"]["state"] == "finalized"
    assert _execution_result(transaction) == "FINISHED_WITH_ERROR"
    assert _is_successful_transaction(transaction) is False

    with pytest.raises(GenLayerExecutionFailed, match="FINISHED_WITH_ERROR"):
        finalize_and_persist_deployment(
            FakeClient(wait_result=transaction),
            TX_ID,
            source=SOURCE,
            authorized_submitter=OPERATOR,
            artifact_path=path,
        )

    assert not path.exists()


def test_live_camel_case_success_shape_is_recognized():
    transaction = json.loads(LIVE_SHAPES_PATH.read_text())["successful"]

    assert transaction["lifecycle"]["state"] == "finalized"
    assert _execution_result(transaction) == "FINISHED_WITH_RETURN"
    assert _is_successful_transaction(transaction) is True


def test_artifact_written_only_after_finalized_success_and_reloads(tmp_path):
    path = tmp_path / "studio-dev.json"
    client = FakeClient(wait_result=finalized_transaction())
    deployed_at = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)

    artifact = finalize_and_persist_deployment(
        client,
        TX_ID,
        source=SOURCE,
        authorized_submitter=OPERATOR,
        artifact_path=path,
        deployed_at=deployed_at,
    )
    loaded = load_deployment_artifact(
        path,
        expected_network=STUDIO_DEV_NETWORK,
        source=SOURCE,
    )

    assert path.exists()
    assert loaded == artifact
    assert loaded.finalized_status == "finalized"
    assert loaded.execution_result_status == "FINISHED_WITH_RETURN"
    assert loaded.contract_source_hash == contract_source_hash(SOURCE)
    assert path.read_text() == json.dumps(
        artifact.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def test_artifact_source_hash_mismatch_is_rejected(tmp_path):
    finalized = FinalizedGenLayerDeployment(
        transaction_id=TX_ID,
        contract_address=CONTRACT,
        lifecycle_state="finalized",
        execution_result="FINISHED_WITH_RETURN",
    )
    artifact = build_deployment_artifact(
        finalized,
        source=SOURCE,
        authorized_submitter=OPERATOR,
        deployed_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
    )
    path = tmp_path / "studio-dev.json"
    persist_deployment_artifact(path, artifact)

    with pytest.raises(GenLayerDeploymentArtifactError, match="source hash"):
        load_deployment_artifact(
            path,
            expected_network=STUDIO_DEV_NETWORK,
            source=SOURCE + "\n# changed",
        )


def test_smoke_loader_rejects_artifact_network_mismatch(tmp_path):
    finalized = FinalizedGenLayerDeployment(
        transaction_id=TX_ID,
        contract_address=CONTRACT,
        lifecycle_state="finalized",
        execution_result="FINISHED_WITH_RETURN",
    )
    artifact = build_deployment_artifact(
        finalized,
        source=SOURCE,
        authorized_submitter=OPERATOR,
    )
    path = tmp_path / "studio-dev.json"
    tampered = artifact.model_dump(mode="json")
    tampered["network"] = "studionet"
    path.write_text(json.dumps(tampered))

    with pytest.raises(GenLayerNetworkMismatch, match="artifact targets"):
        load_deployment_artifact(
            path,
            expected_network=STUDIO_DEV_NETWORK,
            source=SOURCE,
        )


def test_conflicting_artifact_is_not_overwritten(tmp_path):
    path = tmp_path / "studio-dev.json"
    path.write_text("do-not-overwrite\n")
    finalized = FinalizedGenLayerDeployment(
        transaction_id=TX_ID,
        contract_address=CONTRACT,
        lifecycle_state="finalized",
        execution_result="FINISHED_WITH_RETURN",
    )
    artifact = build_deployment_artifact(
        finalized,
        source=SOURCE,
        authorized_submitter=OPERATOR,
    )

    with pytest.raises(GenLayerDeploymentArtifactError, match="refusing to overwrite"):
        persist_deployment_artifact(path, artifact)
    assert path.read_text() == "do-not-overwrite\n"


def test_smoke_payloads_are_deterministic_synthetic_and_distinct():
    good = build_synthetic_smoke_payload("good")
    bad = build_synthetic_smoke_payload("bad")

    assert good == build_synthetic_smoke_payload("good")
    assert bad == build_synthetic_smoke_payload("bad")
    assert good.dispute_id != bad.dispute_id
    assert good.evidence_hash != bad.evidence_hash
    assert "synthetic" in good.buyer_allegation.lower()


def test_fee_options_cannot_be_unbounded_or_zero():
    client = FakeClient(
        fee_estimate={
            "distribution": {},
            "feeValue": 0,
            "policy": {"enabled": True},
        }
    )
    with pytest.raises(GenLayerFeeEstimationFailed):
        estimate_transaction_fees(client)


def test_fee_model_returns_a_copy_for_submission():
    estimate = GenLayerFeeEstimate(
        fee_value=1,
        distribution={"rotations": [3]},
        policy={"enabled": True},
    )
    options = estimate.transaction_options
    options["feeValue"] = 99
    assert estimate.fee_value == 1
