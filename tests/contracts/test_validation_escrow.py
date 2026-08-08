"""Executable state-machine tests for the compiled Vyper validation escrow."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from eth_abi import encode
from eth_account import Account
from eth_tester import EthereumTester
from eth_tester.exceptions import TransactionFailed
from eth_utils import keccak
from vyper import compile_code
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider

from arc_agent_pay.workflow import (
    DeliveryEvidence,
    EscrowStatus,
    SignedValidationVerdict,
    ValidationVerdict,
    WorkOrder,
    delivery_tuple,
    hash_content,
    order_tuple,
    sign_funding_authorization,
    sign_verdict,
    signature_parts,
    verdict_domain,
    verdict_message,
    verdict_tuple,
)
from arc_agent_pay.workflow.signing import VERDICT_TYPES


PAYER_KEY = "0x" + "22" * 32
VALIDATOR_KEY = "0x" + "11" * 32
OTHER_KEY = "0x" + "33" * 32

PAYER = Account.from_key(PAYER_KEY).address
VALIDATOR = Account.from_key(VALIDATOR_KEY).address
OTHER = Account.from_key(OTHER_KEY).address

DOMAIN_TYPE_HASH = keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
DOMAIN_NAME_HASH = keccak(text="ArcAgentPay Validation")
DOMAIN_VERSION_HASH = keccak(text="1")

ROOT = Path(__file__).parents[2]


@dataclass
class Context:
    tester: EthereumTester
    w3: Web3
    token: object
    escrow: object
    relayer: str
    provider: str
    now: int


def compile_contract(path: str) -> dict:
    source_path = ROOT / path
    return compile_code(
        source_path.read_text(),
        contract_path=source_path,
        output_formats=["abi", "bytecode"],
    )


def deploy(w3: Web3, compiled: dict, sender: str, *args):
    factory = w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])
    tx_hash = factory.constructor(*args).transact({"from": sender})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    assert receipt["status"] == 1
    return w3.eth.contract(address=receipt["contractAddress"], abi=compiled["abi"])


@pytest.fixture(scope="session")
def artifacts() -> tuple[dict, dict]:
    return (
        compile_contract("contracts/test/MockEIP3009.vy"),
        compile_contract("contracts/ValidationEscrow.vy"),
    )


@pytest.fixture
def ctx(artifacts) -> Context:
    tester = EthereumTester()
    w3 = Web3(EthereumTesterProvider(tester))
    relayer = w3.eth.accounts[0]
    provider = w3.eth.accounts[1]
    token_artifact, escrow_artifact = artifacts
    token = deploy(w3, token_artifact, relayer)
    escrow = deploy(
        w3,
        escrow_artifact,
        relayer,
        token.address,
    )
    return Context(
        tester=tester,
        w3=w3,
        token=token,
        escrow=escrow,
        relayer=relayer,
        provider=provider,
        now=w3.eth.get_block("latest")["timestamp"],
    )


def make_order(ctx: Context, **overrides) -> WorkOrder:
    values = {
        "escrow": ctx.escrow.address,
        "payer": PAYER,
        "provider": ctx.provider,
        "validator": VALIDATOR,
        "asset": ctx.token.address,
        "amount": 100_000,
        "chain_id": ctx.w3.eth.chain_id,
        "delivery_deadline": ctx.now + 1_000,
        "refund_after": ctx.now + 2_000,
        "task_hash": hash_content("produce the report"),
        "nonce": "0x" + "01" * 32,
    }
    values.update(overrides)
    return WorkOrder(**values)


def transact(ctx: Context, fn) -> None:
    tx_hash = fn.transact({"from": ctx.relayer})
    assert ctx.w3.eth.wait_for_transaction_receipt(tx_hash)["status"] == 1


def fund(ctx: Context, order: WorkOrder) -> None:
    transact(ctx, ctx.token.functions.mint(PAYER, order.amount))
    authorization = sign_funding_authorization(order, private_key=PAYER_KEY)
    v, r, s = signature_parts(authorization.signature)
    transact(ctx, ctx.escrow.functions.fund(order_tuple(order), v, r, s))


def travel_to(ctx: Context, timestamp: int) -> None:
    ctx.tester.time_travel(timestamp)
    ctx.tester.mine_block()


def delivery_and_verdict(
    ctx: Context,
    order: WorkOrder,
    *,
    approved: bool = True,
) -> tuple[DeliveryEvidence, SignedValidationVerdict]:
    delivery = DeliveryEvidence(
        order_hash=order.order_hash,
        evidence_hash=hash_content("delivered result"),
        evidence_uri="ipfs://bafy-result",
        delivered_at=ctx.now + 100,
    )
    verdict = ValidationVerdict.for_delivery(
        delivery,
        approved=approved,
        score=95 if approved else 10,
        reason="accepted" if approved else "rejected",
        issued_at=ctx.now + 150,
        valid_until=ctx.now + 1_500,
    )
    return delivery, sign_verdict(verdict, private_key=VALIDATOR_KEY, order=order)


def settle_args(order, delivery, signed):
    v, r, s = signature_parts(signed.signature)
    return (
        order_tuple(order),
        delivery_tuple(delivery),
        verdict_tuple(signed.verdict),
        v,
        r,
        s,
    )


def status(ctx: Context, order: WorkOrder) -> EscrowStatus:
    return EscrowStatus(
        ctx.escrow.functions.status(bytes.fromhex(order.order_hash[2:])).call()
    )


def test_contract_hashes_match_sdk_and_eip712_domain(ctx):
    order = make_order(ctx)
    delivery = DeliveryEvidence(
        order_hash=order.order_hash,
        evidence_hash=hash_content("result"),
        evidence_uri="ipfs://result",
        delivered_at=ctx.now + 10,
    )
    assert ctx.escrow.functions.hash_order(order_tuple(order)).call() == order.order_hash_bytes
    assert (
        ctx.escrow.functions.hash_delivery(delivery_tuple(delivery)).call()
        == delivery.delivery_hash_bytes
    )

    expected_domain = keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                DOMAIN_TYPE_HASH,
                DOMAIN_NAME_HASH,
                DOMAIN_VERSION_HASH,
                order.chain_id,
                order.escrow,
            ],
        )
    )
    assert ctx.escrow.functions.domain_separator().call() == expected_domain


def test_fund_moves_exact_amount_and_cannot_be_replayed(ctx):
    order = make_order(ctx)
    fund(ctx, order)

    assert status(ctx, order) is EscrowStatus.FUNDED
    assert ctx.token.functions.balances(PAYER).call() == 0
    assert ctx.token.functions.balances(ctx.escrow.address).call() == order.amount
    with pytest.raises(TransactionFailed, match="order already exists"):
        ctx.escrow.functions.fund(
            order_tuple(order), 27, b"\x01" * 32, b"\x01" * 32
        ).transact({"from": ctx.relayer})


def test_receive_authorization_cannot_be_frontrun(ctx):
    order = make_order(ctx)
    transact(ctx, ctx.token.functions.mint(PAYER, order.amount))
    attacker = ctx.w3.eth.accounts[2]
    with pytest.raises(TransactionFailed, match="caller must be payee"):
        ctx.token.functions.receiveWithAuthorization(
            PAYER,
            ctx.escrow.address,
            order.amount,
            0,
            order.delivery_deadline,
            bytes.fromhex(order.order_hash[2:]),
            27,
            b"\x01" * 32,
            b"\x01" * 32,
        ).transact({"from": attacker})


def test_payer_signature_cannot_fund_altered_order_terms(ctx):
    order = make_order(ctx)
    altered = make_order(ctx, provider=ctx.w3.eth.accounts[3])
    transact(ctx, ctx.token.functions.mint(PAYER, order.amount))
    authorization = sign_funding_authorization(order, private_key=PAYER_KEY)
    v, r, s = signature_parts(authorization.signature)

    with pytest.raises(TransactionFailed, match="invalid signature"):
        ctx.escrow.functions.fund(order_tuple(altered), v, r, s).transact(
            {"from": ctx.relayer}
        )
    assert status(ctx, altered) is EscrowStatus.NONE
    assert ctx.token.functions.balances(PAYER).call() == order.amount

    transact(ctx, ctx.escrow.functions.fund(order_tuple(order), v, r, s))
    assert status(ctx, order) is EscrowStatus.FUNDED


def test_contract_rejects_non_independent_roles_even_without_sdk_model(ctx):
    order = make_order(ctx)
    malformed = list(order_tuple(order))
    malformed[2] = malformed[1]

    with pytest.raises(TransactionFailed, match="payer is provider"):
        ctx.escrow.functions.fund(
            tuple(malformed), 27, b"\x01" * 32, b"\x01" * 32
        ).transact({"from": ctx.relayer})


def test_approved_verdict_releases_exact_amount(ctx):
    order = make_order(ctx)
    fund(ctx, order)
    delivery, signed = delivery_and_verdict(ctx, order)
    travel_to(ctx, ctx.now + 200)

    transact(ctx, ctx.escrow.functions.release(*settle_args(order, delivery, signed)))

    assert status(ctx, order) is EscrowStatus.RELEASED
    assert ctx.token.functions.balances(ctx.provider).call() == order.amount
    assert ctx.token.functions.balances(ctx.escrow.address).call() == 0
    with pytest.raises(TransactionFailed, match="order not funded"):
        ctx.escrow.functions.release(*settle_args(order, delivery, signed)).transact(
            {"from": ctx.relayer}
        )


def test_rejected_verdict_refunds_immediately(ctx):
    order = make_order(ctx)
    fund(ctx, order)
    delivery, signed = delivery_and_verdict(ctx, order, approved=False)
    travel_to(ctx, ctx.now + 200)

    transact(
        ctx,
        ctx.escrow.functions.refund_rejected(*settle_args(order, delivery, signed)),
    )

    assert status(ctx, order) is EscrowStatus.REFUNDED
    assert ctx.token.functions.balances(PAYER).call() == order.amount
    assert ctx.token.functions.balances(ctx.provider).call() == 0


def test_timeout_refund_is_unavailable_early_then_returns_funds(ctx):
    order = make_order(ctx)
    fund(ctx, order)
    with pytest.raises(TransactionFailed, match="refund not available"):
        ctx.escrow.functions.refund_timeout(order_tuple(order)).transact(
            {"from": ctx.relayer}
        )

    travel_to(ctx, order.refund_after)
    transact(ctx, ctx.escrow.functions.refund_timeout(order_tuple(order)))

    assert status(ctx, order) is EscrowStatus.REFUNDED
    assert ctx.token.functions.balances(PAYER).call() == order.amount


def test_wrong_validator_signature_cannot_release(ctx):
    order = make_order(ctx)
    fund(ctx, order)
    delivery, signed = delivery_and_verdict(ctx, order)
    wrong = Account.sign_typed_data(
        OTHER_KEY,
        domain_data=verdict_domain(
            chain_id=order.chain_id,
            verifying_contract=order.escrow,
        ),
        message_types=VERDICT_TYPES,
        message_data=verdict_message(signed.verdict),
    )
    forged = SignedValidationVerdict(
        verdict=signed.verdict,
        validator=OTHER,
        signature=wrong.signature.to_0x_hex(),
    )
    travel_to(ctx, ctx.now + 200)

    with pytest.raises(TransactionFailed, match="wrong validator"):
        ctx.escrow.functions.release(*settle_args(order, delivery, forged)).transact(
            {"from": ctx.relayer}
        )
    assert ctx.token.functions.balances(ctx.escrow.address).call() == order.amount


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("evidence_uri", "ipfs://tampered"),
        ("delivered_at", None),
    ],
)
def test_tampered_delivery_cannot_release(ctx, field, value):
    order = make_order(ctx)
    fund(ctx, order)
    delivery, signed = delivery_and_verdict(ctx, order)
    changed = delivery.model_copy(
        update={field: ctx.now + 101 if value is None else value}
    )
    travel_to(ctx, ctx.now + 200)

    with pytest.raises(TransactionFailed, match="wrong delivery"):
        ctx.escrow.functions.release(*settle_args(order, changed, signed)).transact(
            {"from": ctx.relayer}
        )


def test_late_delivery_cannot_release(ctx):
    order = make_order(ctx)
    fund(ctx, order)
    late = DeliveryEvidence(
        order_hash=order.order_hash,
        evidence_hash=hash_content("late result"),
        evidence_uri="ipfs://late",
        delivered_at=order.delivery_deadline + 1,
    )
    verdict = ValidationVerdict.for_delivery(
        late,
        approved=True,
        score=100,
        reason="otherwise valid",
        issued_at=order.delivery_deadline + 2,
        valid_until=order.refund_after,
    )
    signed = sign_verdict(verdict, private_key=VALIDATOR_KEY, order=order)
    travel_to(ctx, order.delivery_deadline + 100)

    with pytest.raises(TransactionFailed, match="late delivery"):
        ctx.escrow.functions.release(*settle_args(order, late, signed)).transact(
            {"from": ctx.relayer}
        )


def test_approval_and_rejection_paths_cannot_be_swapped(ctx):
    approved_order = make_order(ctx)
    fund(ctx, approved_order)
    delivery, approved = delivery_and_verdict(ctx, approved_order)
    travel_to(ctx, ctx.now + 200)
    with pytest.raises(TransactionFailed, match="verdict approved"):
        ctx.escrow.functions.refund_rejected(
            *settle_args(approved_order, delivery, approved)
        ).transact({"from": ctx.relayer})

    rejected_order = make_order(ctx, nonce="0x" + "02" * 32)
    fund(ctx, rejected_order)
    rejected_delivery, rejected = delivery_and_verdict(
        ctx, rejected_order, approved=False
    )
    with pytest.raises(TransactionFailed, match="verdict rejected"):
        ctx.escrow.functions.release(
            *settle_args(rejected_order, rejected_delivery, rejected)
        ).transact({"from": ctx.relayer})
