"""ERC-8183 draft models, commitments, and ABI-profile tests."""

from __future__ import annotations

import pytest
from eth_abi import encode
from eth_utils import keccak
from pydantic import ValidationError

from arc_agent_pay.onchain import load_abi
from arc_agent_pay.workflow import (
    DeliveryEvidence,
    Erc8183Job,
    Erc8183JobSpec,
    Erc8183Status,
    ValidationVerdict,
    WorkOrder,
    ZERO_ADDRESS,
    deliverable_commitment,
    hash_content,
    verdict_commitment,
)


ESCROW = "0x" + "aa" * 20
CLIENT = "0x" + "bb" * 20
PROVIDER = "0x" + "cc" * 20
EVALUATOR = "0x" + "dd" * 20
TOKEN = "0x" + "36" + "00" * 19


def order() -> WorkOrder:
    return WorkOrder(
        escrow=ESCROW,
        payer=CLIENT,
        provider=PROVIDER,
        validator=EVALUATOR,
        asset=TOKEN,
        amount=100_000,
        chain_id=5_042_002,
        delivery_deadline=1_500,
        refund_after=2_000,
        task_hash=hash_content("task"),
        nonce="0x" + "01" * 32,
    )


def delivery(work_order: WorkOrder) -> DeliveryEvidence:
    return DeliveryEvidence(
        order_hash=work_order.order_hash,
        evidence_hash=hash_content("result"),
        evidence_uri="ipfs://result",
        delivered_at=1_000,
    )


def verdict(evidence: DeliveryEvidence, **overrides) -> ValidationVerdict:
    values = {
        "order_hash": evidence.order_hash,
        "evidence_hash": evidence.evidence_hash,
        "delivery_hash": evidence.delivery_hash,
        "approved": True,
        "score": 95,
        "reason_hash": hash_content("accepted"),
        "issued_at": 1_100,
        "valid_until": 1_800,
    }
    values.update(overrides)
    return ValidationVerdict(**values)


def test_work_order_maps_to_a_compact_job_spec():
    work_order = order()
    spec = Erc8183JobSpec.from_work_order(work_order)

    assert spec.provider == work_order.provider
    assert spec.evaluator == work_order.validator
    assert spec.expired_at == work_order.refund_after
    assert spec.budget == work_order.amount
    assert spec.description == f"urn:arc-agent-pay:work-order:{work_order.order_hash}"
    assert spec.work_order_hash == work_order.order_hash


def test_delivery_and_full_verdict_commitments_match_the_documented_encoding():
    evidence = delivery(order())
    decision = verdict(evidence)
    type_hash = keccak(
        text=(
            "ValidationVerdict(bytes32 orderHash,bytes32 evidenceHash,bytes32 deliveryHash,"
            "bool approved,uint8 score,bytes32 reasonHash,uint256 issuedAt,uint256 validUntil)"
        )
    )
    expected = keccak(
        encode(
            [
                "bytes32",
                "bytes32",
                "bytes32",
                "bytes32",
                "bool",
                "uint8",
                "bytes32",
                "uint256",
                "uint256",
            ],
            [
                type_hash,
                bytes.fromhex(decision.order_hash[2:]),
                bytes.fromhex(decision.evidence_hash[2:]),
                bytes.fromhex(decision.delivery_hash[2:]),
                decision.approved,
                decision.score,
                bytes.fromhex(decision.reason_hash[2:]),
                decision.issued_at,
                decision.valid_until,
            ],
        )
    )

    assert deliverable_commitment(evidence) == evidence.delivery_hash
    assert verdict_commitment(decision) == "0x" + expected.hex()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence_hash", hash_content("other evidence")),
        ("delivery_hash", hash_content("other delivery")),
        ("approved", False),
        ("score", 94),
        ("reason_hash", hash_content("other reason")),
        ("issued_at", 1_101),
        ("valid_until", 1_801),
    ],
)
def test_every_verdict_field_changes_the_attestation_commitment(field, value):
    evidence = delivery(order())
    baseline = verdict(evidence)
    assert verdict_commitment(verdict(evidence, **{field: value})) != verdict_commitment(
        baseline
    )


def test_job_model_allows_optional_provider_and_hook_but_not_missing_roles():
    job = Erc8183Job(
        job_id=1,
        client=CLIENT,
        provider=ZERO_ADDRESS,
        evaluator=EVALUATOR,
        description="open for bidding",
        budget=0,
        expired_at=2_000,
        status=Erc8183Status.OPEN,
        hook=ZERO_ADDRESS,
    )
    assert job.provider == ZERO_ADDRESS
    with pytest.raises(ValidationError, match="zero address"):
        Erc8183Job(**{**job.model_dump(), "client": ZERO_ADDRESS})


def test_packaged_abi_is_the_published_reference_profile():
    functions = {
        entry["name"]: tuple(item["type"] for item in entry.get("inputs", []))
        for entry in load_abi("erc8183_reference")
        if entry["type"] == "function"
    }
    assert functions == {
        "paymentToken": (),
        "getJob": ("uint256",),
        "createJob": ("address", "address", "uint256", "string", "address"),
        "setProvider": ("uint256", "address"),
        "setBudget": ("uint256", "uint256", "bytes"),
        "fund": ("uint256", "bytes"),
        "submit": ("uint256", "bytes32", "bytes"),
        "complete": ("uint256", "bytes32", "bytes"),
        "reject": ("uint256", "bytes32", "bytes"),
        "claimRefund": ("uint256",),
    }
