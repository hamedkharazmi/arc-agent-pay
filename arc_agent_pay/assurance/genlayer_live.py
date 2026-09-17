"""Studio-dev deployment primitives for the Phase 3C GenLayer smoke test.

This module deliberately supports only the Consensus v0.6 release-candidate
Studio-dev network. It does not deploy to stable Studionet or Bradbury, and it
contains no Arc relay or WarrantyBond behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..exceptions import (
    GenLayerDeploymentArtifactError,
    GenLayerDeploymentError,
    GenLayerExecutionFailed,
    GenLayerFeeEstimationFailed,
    GenLayerFinalityTimeout,
    GenLayerInsufficientBalance,
    GenLayerNetworkMismatch,
    GenLayerSubmissionOutcomeUnknown,
    GenLayerTransportRetryExhausted,
)
from ..workflow.models import _normalize_address, _normalize_nonzero_bytes32
from .genlayer import GENLAYER_PY_VERSION
from .genlayer_transport import (
    begin_submission,
    finish_submission,
    install_studio_dev_retries,
    retry_exhausted_error,
    submission_error,
)
from .models import canonical_json

STUDIO_DEV_NETWORK = "studio-dev"
STUDIO_DEV_CHAIN_ID = 61_997
STUDIO_DEV_RPC_URL = "https://studio-dev.genlayer.com/api"
STUDIO_DEV_EXPLORER_URL = "https://explorer-studio-dev.genlayer.com"
DEFAULT_FINALITY_TIMEOUT_SECONDS = 900
DEFAULT_POLL_INTERVAL_SECONDS = 3
# Release-family metadata and runner proven by finalized Studio-dev deployments.
GENVM_CONTRACT_RELEASE = "v0.3.0"
GENVM_RUNNER_IDENTIFIER = (
    "py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng"
)

_ZERO_ADDRESS = "0x" + "0" * 40
_SHA256_RE = re.compile(r"^0x[0-9a-f]{64}$")
_EXECUTION_RESULT_NAMES = {
    0: "NOT_VOTED",
    1: "FINISHED_WITH_RETURN",
    2: "FINISHED_WITH_ERROR",
    3: "TIMEOUT",
    4: "NONDET_DISAGREE",
    5: "DETERMINISTIC_VIOLATION",
}


class _FrozenStrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class GenLayerDeploymentArtifact(_FrozenStrictModel):
    """Auditable record written only after successful protocol finality."""

    schema_version: Literal["1"] = "1"
    network: str = Field(min_length=1, max_length=64)
    rpc_url: str = Field(min_length=1, max_length=2048)
    chain_id: int = Field(gt=0)
    contract_address: str
    authorized_submitter: str
    deployment_transaction_id: str
    finalized_status: Literal["finalized"]
    execution_result_status: Literal["FINISHED_WITH_RETURN"]
    deployed_at: str = Field(min_length=20, max_length=40)
    contract_source_hash: str
    contract_version: str
    runner_identifier: str
    genlayer_py_version: Literal[GENLAYER_PY_VERSION]
    explorer_transaction_url: str
    explorer_contract_url: str

    _addresses = field_validator(
        "contract_address", "authorized_submitter", mode="before"
    )(_normalize_address)
    _transaction = field_validator("deployment_transaction_id", mode="before")(
        _normalize_nonzero_bytes32
    )

    @field_validator("contract_address", "authorized_submitter")
    @classmethod
    def reject_zero_address(cls, value: str) -> str:
        if value == _ZERO_ADDRESS:
            raise ValueError("address must not be zero")
        return value

    @field_validator("contract_source_hash")
    @classmethod
    def validate_source_hash(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("contract source hash must be a lowercase SHA-256 value")
        return value


class FinalizedGenLayerDeployment(_FrozenStrictModel):
    transaction_id: str
    contract_address: str
    lifecycle_state: Literal["finalized"]
    execution_result: Literal["FINISHED_WITH_RETURN"]

    _transaction = field_validator("transaction_id", mode="before")(
        _normalize_nonzero_bytes32
    )
    _address = field_validator("contract_address", mode="before")(_normalize_address)


@dataclass(frozen=True)
class GenLayerFeeEstimate:
    """Validated SDK fee estimate and the exact options passed to a transaction."""

    fee_value: int
    distribution: Mapping[str, Any]
    policy: Mapping[str, Any]

    @property
    def transaction_options(self) -> dict[str, Any]:
        return {
            "distribution": dict(self.distribution),
            "feeValue": self.fee_value,
        }

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "fee_value_atomic_gen": self.fee_value,
            "policy_enabled": bool(self.policy.get("enabled")),
            "leader_timeunits": self.distribution.get("leaderTimeunitsAllocation"),
            "validator_timeunits": self.distribution.get(
                "validatorTimeunitsAllocation"
            ),
            "appeal_rounds": self.distribution.get("appealRounds"),
            "rotations": self.distribution.get("rotations"),
            "execution_budget_per_round": self.distribution.get(
                "executionBudgetPerRound"
            ),
        }


def load_operator_account(
    environ: Optional[Mapping[str, str]] = None,
) -> Any:
    """Create the operator signer from its environment secret without logging it."""
    source = os.environ if environ is None else environ
    private_key = source.get("ASSURANCE_OPERATOR_PRIVATE_KEY", "").strip()
    if not private_key:
        raise GenLayerDeploymentError(
            "ASSURANCE_OPERATOR_PRIVATE_KEY is required; no key was generated"
        )
    try:
        from genlayer_py import create_account

        return create_account(private_key)
    except Exception as exc:
        raise GenLayerDeploymentError(
            "ASSURANCE_OPERATOR_PRIVATE_KEY is not a valid EVM private key"
        ) from exc


def create_studio_dev_client(account: Any) -> Any:
    """Build the v0.19 RC client with retries active before its first RPC."""
    from copy import deepcopy

    from genlayer_py.client import GenLayerClient
    from genlayer_py.chains import studio_devnet

    client = GenLayerClient(deepcopy(studio_devnet), account)
    install_studio_dev_retries(client)
    client.initialize_consensus_smart_contract()
    return client


def verify_studio_dev_network(client: Any) -> int:
    try:
        actual = int(client.chain_id)
    except Exception as exc:
        exhausted = retry_exhausted_error(exc)
        if exhausted is not None:
            raise exhausted
        raise GenLayerNetworkMismatch("could not query the connected chain ID") from exc
    if actual != STUDIO_DEV_CHAIN_ID:
        raise GenLayerNetworkMismatch(
            f"refusing GenLayer chain {actual}; expected Studio-dev {STUDIO_DEV_CHAIN_ID}"
        )
    return actual


def contract_source_hash(source: str) -> str:
    return "0x" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def contract_source_metadata(source: str) -> tuple[str, str]:
    lines = source.splitlines()
    if len(lines) < 3:
        raise GenLayerDeploymentError(
            "GenLayer contract must start with the pinned v0.3 version and Depends "
            "runner declarations followed by a blank line"
        )
    expected_depends = f'# {{ "Depends": "{GENVM_RUNNER_IDENTIFIER}" }}'
    if (
        lines[0] != f"# {GENVM_CONTRACT_RELEASE}"
        or lines[1] != expected_depends
        or lines[2] != ""
    ):
        raise GenLayerDeploymentError(
            "GenLayer contract must start with the pinned v0.3 version and Depends "
            "runner declarations followed by a blank line"
        )
    return GENVM_CONTRACT_RELEASE, GENVM_RUNNER_IDENTIFIER


def estimate_transaction_fees(client: Any) -> GenLayerFeeEstimate:
    """Use the v0.19 RC public fee estimator; never fabricate feeValue."""
    try:
        estimate = client.estimate_transaction_fees()
    except Exception as exc:
        exhausted = retry_exhausted_error(exc)
        if exhausted is not None:
            raise exhausted
        raise GenLayerFeeEstimationFailed(
            "Studio-dev fee estimation failed; no transaction was submitted"
        ) from exc
    if not isinstance(estimate, Mapping):
        raise GenLayerFeeEstimationFailed("SDK returned a malformed fee estimate")
    fee_value = estimate.get("feeValue", estimate.get("fee_value"))
    distribution = estimate.get("distribution")
    policy = estimate.get("policy")
    if (
        isinstance(fee_value, bool)
        or not isinstance(fee_value, int)
        or fee_value <= 0
        or not isinstance(distribution, Mapping)
        or not isinstance(policy, Mapping)
        or not policy.get("enabled")
    ):
        raise GenLayerFeeEstimationFailed(
            "SDK did not return an enabled, positive Studio-dev fee estimate"
        )
    return GenLayerFeeEstimate(
        fee_value=fee_value,
        distribution=dict(distribution),
        policy=dict(policy),
    )


def require_operator_balance(
    client: Any,
    operator_address: str,
    fee_estimate: GenLayerFeeEstimate,
) -> int:
    balance = query_native_balance(client, operator_address)
    if balance < fee_estimate.fee_value:
        raise GenLayerInsufficientBalance(
            f"Studio-dev operator {operator_address} has {balance} atomic GEN; "
            f"estimated fee requires {fee_estimate.fee_value}"
        )
    return balance


def query_native_balance(client: Any, operator_address: str) -> int:
    """Read native GEN through standard ``eth_getBalance`` in atomic units."""
    # The v0.19 RC does not expose a portable SDK convenience method on every
    # hosted network. Query the standard Ethereum balance endpoint directly,
    # and only trust it after confirming the exact Studio-dev chain.
    verify_studio_dev_network(client)
    try:
        web3 = client.w3
        raw_balance = web3.eth.get_balance(operator_address, "latest")
        if isinstance(raw_balance, bool):
            raise TypeError("native balance cannot be boolean")
        if isinstance(raw_balance, str):
            balance = int(raw_balance, 0)
        elif isinstance(raw_balance, int):
            balance = raw_balance
        else:
            raise TypeError("native balance must be an integer")
        if balance < 0:
            raise ValueError("native balance cannot be negative")
    except Exception as exc:
        exhausted = retry_exhausted_error(exc)
        if exhausted is not None:
            raise exhausted
        raise GenLayerDeploymentError(
            f"could not query operator GEN balance: {exc}"
        ) from exc
    return balance


def _checksum_address(value: str) -> str:
    """Normalize an address for Web3/genlayer-py calls without lowercasing it."""
    normalized = _normalize_address(value)
    try:
        from eth_utils import to_checksum_address
    except ImportError:
        return value
    return to_checksum_address(normalized)


def _transaction_id(value: Any) -> str:
    if isinstance(value, bytes):
        value = "0x" + value.hex()
    elif not isinstance(value, str) and hasattr(value, "hex"):
        value = str(value.hex())
        if not value.startswith("0x"):
            value = "0x" + value
    return _normalize_nonzero_bytes32(value)


def submit_genlayer_deployment(
    client: Any,
    *,
    source: str,
    authorized_submitter: str,
    fee_estimate: GenLayerFeeEstimate,
) -> str:
    """Submit once with real consensus enabled and return the resumable tx ID."""
    contract_source_metadata(source)
    submitter = _checksum_address(authorized_submitter)
    if submitter == _ZERO_ADDRESS:
        raise GenLayerDeploymentError("authorized submitter must not be zero")
    begin_submission(client)
    try:
        transaction_id = client.deploy_contract(
            code=source,
            args=[submitter],
            leader_only=False,
            fees=fee_estimate.transaction_options,
        )
        normalized = _transaction_id(transaction_id)
    except Exception as exc:
        classified = submission_error(exc, client)
        if isinstance(classified, GenLayerSubmissionOutcomeUnknown):
            raise classified
        if isinstance(classified, GenLayerTransportRetryExhausted):
            raise classified
        raise GenLayerDeploymentError("GenLayer deployment submission failed") from exc
    finish_submission(client)
    return normalized


def _lifecycle_state(transaction: Mapping[str, Any]) -> Optional[str]:
    lifecycle = transaction.get("lifecycle")
    return lifecycle.get("state") if isinstance(lifecycle, Mapping) else None


def _execution_result(transaction: Mapping[str, Any]) -> Optional[str]:
    value = next(
        (
            transaction[key]
            for key in (
                "txExecutionResultName",
                "tx_execution_result_name",
                "txExecutionResult",
                "tx_execution_result",
            )
            if transaction.get(key) is not None
        ),
        None,
    )
    value = getattr(value, "value", value)
    if isinstance(value, int) and not isinstance(value, bool):
        return _EXECUTION_RESULT_NAMES.get(value)
    if isinstance(value, str):
        return value
    consensus = transaction.get("consensus_data")
    if isinstance(consensus, Mapping):
        receipt = consensus.get("leader_receipt")
        if isinstance(receipt, list):
            receipt = receipt[0] if receipt else None
        if isinstance(receipt, Mapping) and receipt.get("execution_result") == "SUCCESS":
            return "FINISHED_WITH_RETURN"
    return None


def _is_successful_transaction(transaction: Mapping[str, Any]) -> bool:
    """Use the pinned SDK success predicate with the live camelCase shape."""
    normalized = dict(transaction)
    execution = _execution_result(transaction)
    if execution is not None:
        # v0.19.0rc2's helper currently reads snake_case, while hosted
        # Studio-dev transactions are returned in camelCase.
        normalized["tx_execution_result_name"] = execution
    try:
        from genlayer_py.transactions import is_successful
    except ImportError:
        lifecycle = transaction.get("lifecycle")
        return (
            isinstance(lifecycle, Mapping)
            and lifecycle.get("state") == "finalized"
            and lifecycle.get("outcome") in (None, "accepted")
            and execution == "FINISHED_WITH_RETURN"
        )
    try:
        return bool(is_successful(normalized))
    except Exception:
        # Never turn an SDK classification failure into deployment success.
        return False


def _contract_address(transaction: Mapping[str, Any]) -> str:
    decoded = transaction.get("tx_data_decoded")
    candidates: list[Any] = []
    if isinstance(decoded, Mapping):
        candidates.append(decoded.get("contract_address"))
    data = transaction.get("data")
    if isinstance(data, Mapping):
        candidates.append(data.get("contract_address"))
    candidates.extend(
        [
            transaction.get("contract_address"),
            transaction.get("recipient"),
            transaction.get("to_address"),
        ]
    )
    for candidate in candidates:
        if not candidate:
            continue
        try:
            address = _normalize_address(str(candidate))
        except ValueError:
            continue
        if address != _ZERO_ADDRESS:
            return address
    raise GenLayerDeploymentError(
        "finalized deployment did not expose a non-zero contract address"
    )


def wait_for_finalized_deployment(
    client: Any,
    transaction_id: str,
    *,
    timeout_seconds: int = DEFAULT_FINALITY_TIMEOUT_SECONDS,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
) -> FinalizedGenLayerDeployment:
    """Wait for Finalized; timeout is resumable and never means transaction failure."""
    normalized = _transaction_id(transaction_id)
    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("finality timeout and poll interval must be positive")
    interval_ms = poll_interval_seconds * 1000
    retries = max(1, math.ceil(timeout_seconds / poll_interval_seconds))
    transaction: Any = None
    try:
        transaction = client.wait_for_finalization(
            transaction_hash=normalized,
            interval=interval_ms,
            retries=retries,
            full_transaction=True,
        )
    except Exception as exc:
        state = None
        latest = None
        try:
            latest = client.get_transaction(transaction_hash=normalized)
            if isinstance(latest, Mapping):
                state = _lifecycle_state(latest)
        except Exception:
            pass
        if state == "finalized":
            transaction = latest
        elif state == "canceled":
            raise GenLayerExecutionFailed(
                f"deployment transaction {normalized} was canceled"
            ) from exc
        else:
            raise GenLayerFinalityTimeout(normalized, state) from exc
    if not isinstance(transaction, Mapping):
        raise GenLayerDeploymentError("finality response was not a transaction object")
    state = _lifecycle_state(transaction)
    if state != "finalized":
        raise GenLayerFinalityTimeout(normalized, state)
    lifecycle = transaction.get("lifecycle")
    outcome = lifecycle.get("outcome") if isinstance(lifecycle, Mapping) else None
    execution = _execution_result(transaction)
    if (
        outcome not in (None, "accepted")
        or execution != "FINISHED_WITH_RETURN"
        or not _is_successful_transaction(transaction)
    ):
        raise GenLayerExecutionFailed(
            f"deployment finalized without successful accepted execution "
            f"(outcome={outcome!r}, execution={execution!r})"
        )
    return FinalizedGenLayerDeployment(
        transaction_id=normalized,
        contract_address=_contract_address(transaction),
        lifecycle_state="finalized",
        execution_result="FINISHED_WITH_RETURN",
    )


def build_deployment_artifact(
    finalized: FinalizedGenLayerDeployment,
    *,
    source: str,
    authorized_submitter: str,
    deployed_at: Optional[datetime] = None,
) -> GenLayerDeploymentArtifact:
    version, runner = contract_source_metadata(source)
    timestamp = deployed_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("deployed_at must be timezone-aware")
    timestamp_text = timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return GenLayerDeploymentArtifact(
        network=STUDIO_DEV_NETWORK,
        rpc_url=STUDIO_DEV_RPC_URL,
        chain_id=STUDIO_DEV_CHAIN_ID,
        contract_address=finalized.contract_address,
        authorized_submitter=authorized_submitter,
        deployment_transaction_id=finalized.transaction_id,
        finalized_status="finalized",
        execution_result_status="FINISHED_WITH_RETURN",
        deployed_at=timestamp_text,
        contract_source_hash=contract_source_hash(source),
        contract_version=version,
        runner_identifier=runner,
        genlayer_py_version=GENLAYER_PY_VERSION,
        explorer_transaction_url=(
            f"{STUDIO_DEV_EXPLORER_URL}/transactions/{finalized.transaction_id}"
        ),
        explorer_contract_url=(
            f"{STUDIO_DEV_EXPLORER_URL}/contracts/{finalized.contract_address}"
        ),
    )


def persist_deployment_artifact(
    path: Path,
    artifact: GenLayerDeploymentArtifact,
) -> None:
    """Atomically persist one finalized artifact; conflicting replay is rejected."""
    destination = Path(path)
    serialized = canonical_json(artifact.model_dump(mode="json")) + "\n"
    if destination.exists():
        if destination.read_text() == serialized:
            return
        raise GenLayerDeploymentArtifactError(
            f"refusing to overwrite a different deployment artifact: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as temporary:
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, destination)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def finalize_and_persist_deployment(
    client: Any,
    transaction_id: str,
    *,
    source: str,
    authorized_submitter: str,
    artifact_path: Path,
    timeout_seconds: int = DEFAULT_FINALITY_TIMEOUT_SECONDS,
    deployed_at: Optional[datetime] = None,
) -> GenLayerDeploymentArtifact:
    """Create an artifact only after successful, accepted protocol finality."""
    finalized = wait_for_finalized_deployment(
        client,
        transaction_id,
        timeout_seconds=timeout_seconds,
    )
    artifact = build_deployment_artifact(
        finalized,
        source=source,
        authorized_submitter=authorized_submitter,
        deployed_at=deployed_at,
    )
    persist_deployment_artifact(artifact_path, artifact)
    return artifact


def load_deployment_artifact(
    path: Path,
    *,
    expected_network: str,
    source: str,
) -> GenLayerDeploymentArtifact:
    if expected_network != STUDIO_DEV_NETWORK:
        raise GenLayerNetworkMismatch(
            f"unsupported deployment network {expected_network!r}; use studio-dev"
        )
    try:
        raw = json.loads(Path(path).read_text())
        artifact = GenLayerDeploymentArtifact.model_validate(raw)
    except Exception as exc:
        raise GenLayerDeploymentArtifactError(
            f"could not load a valid deployment artifact: {path}"
        ) from exc
    if artifact.network != expected_network:
        raise GenLayerNetworkMismatch(
            f"artifact targets {artifact.network}, not {expected_network}"
        )
    if artifact.chain_id != STUDIO_DEV_CHAIN_ID or artifact.rpc_url != STUDIO_DEV_RPC_URL:
        raise GenLayerNetworkMismatch(
            "deployment artifact does not identify the Studio-dev v0.6 network"
        )
    expected_hash = contract_source_hash(source)
    if artifact.contract_source_hash != expected_hash:
        raise GenLayerDeploymentArtifactError(
            "deployment artifact source hash does not match the current contract"
        )
    version, runner = contract_source_metadata(source)
    if artifact.contract_version != version or artifact.runner_identifier != runner:
        raise GenLayerDeploymentArtifactError(
            "deployment artifact runner metadata does not match the current contract"
        )
    return artifact


__all__ = [
    "DEFAULT_FINALITY_TIMEOUT_SECONDS",
    "FinalizedGenLayerDeployment",
    "GENLAYER_PY_VERSION",
    "GENVM_CONTRACT_RELEASE",
    "GENVM_RUNNER_IDENTIFIER",
    "GenLayerDeploymentArtifact",
    "GenLayerFeeEstimate",
    "STUDIO_DEV_CHAIN_ID",
    "STUDIO_DEV_EXPLORER_URL",
    "STUDIO_DEV_NETWORK",
    "STUDIO_DEV_RPC_URL",
    "build_deployment_artifact",
    "contract_source_hash",
    "contract_source_metadata",
    "create_studio_dev_client",
    "estimate_transaction_fees",
    "finalize_and_persist_deployment",
    "load_deployment_artifact",
    "load_operator_account",
    "persist_deployment_artifact",
    "query_native_balance",
    "require_operator_balance",
    "submit_genlayer_deployment",
    "verify_studio_dev_network",
    "wait_for_finalized_deployment",
]
