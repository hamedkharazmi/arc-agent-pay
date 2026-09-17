#!/usr/bin/env python3
"""Deploy AgentPayAssurance to Consensus v0.6 Studio-dev only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from arc_agent_pay.assurance.genlayer_live import (
    DEFAULT_FINALITY_TIMEOUT_SECONDS,
    STUDIO_DEV_NETWORK,
    contract_source_hash,
    contract_source_metadata,
    create_studio_dev_client,
    estimate_transaction_fees,
    finalize_and_persist_deployment,
    load_deployment_artifact,
    load_operator_account,
    require_operator_balance,
    submit_genlayer_deployment,
    verify_studio_dev_network,
)
from arc_agent_pay.exceptions import (
    GenLayerDeploymentError,
    GenLayerFinalityTimeout,
    GenLayerSubmissionOutcomeUnknown,
    GenLayerTransportRetryExhausted,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "genlayer" / "contracts" / "AgentPayAssurance.py"
ARTIFACT_PATH = PROJECT_ROOT / "genlayer" / "deployments" / "studio-dev.json"


def _timeout_default() -> int:
    raw = os.environ.get(
        "GENLAYER_FINALITY_TIMEOUT_SECONDS",
        str(DEFAULT_FINALITY_TIMEOUT_SECONDS),
    )
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("GENLAYER_FINALITY_TIMEOUT_SECONDS must be an integer") from exc
    if value <= 0:
        raise ValueError("GENLAYER_FINALITY_TIMEOUT_SECONDS must be positive")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--network",
        required=True,
        choices=[STUDIO_DEV_NETWORK],
        help="explicit release-candidate network selection",
    )
    parser.add_argument(
        "--resume-transaction-id",
        help="resume finality monitoring without submitting another deployment",
    )
    parser.add_argument(
        "--finality-timeout-seconds",
        type=int,
        default=_timeout_default(),
    )
    args = parser.parse_args()
    if args.finality_timeout_seconds <= 0:
        parser.error("--finality-timeout-seconds must be positive")

    source = CONTRACT_PATH.read_text()
    version, runner = contract_source_metadata(source)
    source_hash = contract_source_hash(source)

    if ARTIFACT_PATH.exists():
        artifact = load_deployment_artifact(
            ARTIFACT_PATH,
            expected_network=args.network,
            source=source,
        )
        print(
            json.dumps(
                {
                    "status": "already-finalized",
                    "artifact": str(ARTIFACT_PATH),
                    "contract_address": artifact.contract_address,
                    "deployment_transaction_id": artifact.deployment_transaction_id,
                },
                indent=2,
            )
        )
        return

    account = load_operator_account()
    client = create_studio_dev_client(account)
    chain_id = verify_studio_dev_network(client)
    operator = account.address

    if args.resume_transaction_id:
        transaction_id = args.resume_transaction_id
        print(
            json.dumps(
                {
                    "operation": "resume-deployment-finality",
                    "network": args.network,
                    "chain_id": chain_id,
                    "operator_address": operator,
                    "transaction_id": transaction_id,
                    "contract_source_hash": source_hash,
                    "contract_version": version,
                    "runner": runner,
                },
                indent=2,
            ),
            flush=True,
        )
    else:
        estimate = estimate_transaction_fees(client)
        balance = require_operator_balance(client, operator, estimate)
        print(
            json.dumps(
                {
                    "operation": "deploy",
                    "network": args.network,
                    "chain_id": chain_id,
                    "operator_address": operator,
                    "operator_balance_atomic_gen": balance,
                    "contract_source_hash": source_hash,
                    "contract_version": version,
                    "runner": runner,
                    "fee_estimate": estimate.summary,
                    "leader_only": False,
                },
                indent=2,
            ),
            flush=True,
        )
        transaction_id = submit_genlayer_deployment(
            client,
            source=source,
            authorized_submitter=operator,
            fee_estimate=estimate,
        )
        print(f"GENLAYER_DEPLOYMENT_TRANSACTION_ID={transaction_id}", flush=True)

    try:
        artifact = finalize_and_persist_deployment(
            client,
            transaction_id,
            source=source,
            authorized_submitter=operator,
            artifact_path=ARTIFACT_PATH,
            timeout_seconds=args.finality_timeout_seconds,
        )
    except GenLayerFinalityTimeout as exc:
        print(
            json.dumps(
                {
                    "status": "client-wait-timeout",
                    "transaction_id": exc.transaction_id,
                    "last_state": exc.state,
                    "artifact_written": False,
                    "resume_command": (
                        "python scripts/deploy_genlayer_assurance.py "
                        f"--network studio-dev --resume-transaction-id {exc.transaction_id}"
                    ),
                },
                indent=2,
            ),
            flush=True,
        )
        raise SystemExit(2) from exc

    print(
        json.dumps(
            {
                "status": artifact.finalized_status,
                "execution_result": artifact.execution_result_status,
                "contract_address": artifact.contract_address,
                "deployment_transaction_id": artifact.deployment_transaction_id,
                "operator_address": artifact.authorized_submitter,
                "artifact": str(ARTIFACT_PATH),
                "explorer_transaction": artifact.explorer_transaction_url,
                "explorer_contract": artifact.explorer_contract_url,
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except GenLayerSubmissionOutcomeUnknown as exc:
        raise SystemExit(
            f"error: {exc}. No deployment retry was attempted; inspect the operator "
            "nonce/transactions before deciding whether a fresh submission is safe."
        ) from exc
    except GenLayerTransportRetryExhausted as exc:
        raise SystemExit(f"error: {exc}") from exc
    except GenLayerDeploymentError as exc:
        raise SystemExit(f"error: {exc}") from exc
