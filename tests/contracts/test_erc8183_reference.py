"""Executable lifecycle tests for the packaged ERC-8183 reference profile."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from eth_account import Account
from eth_tester import EthereumTester
from eth_tester.exceptions import TransactionFailed
from vyper import compile_code
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider

from arc_agent_pay.onchain import load_abi
from arc_agent_pay.workflow import Erc8183Client, Erc8183Status, WorkOrder, hash_content
from arc_agent_pay.exceptions import WorkflowError


ROOT = Path(__file__).parents[2]
AMOUNT = 100_000


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


@dataclass
class Context:
    tester: EthereumTester
    w3: Web3
    token: object
    jobs: object
    client: Erc8183Client
    provider: Erc8183Client
    evaluator: Erc8183Client
    now: int


@pytest.fixture(scope="session")
def artifacts() -> tuple[dict, dict]:
    return (
        compile_contract("contracts/test/MockERC20.vy"),
        compile_contract("contracts/test/MockERC8183Reference.vy"),
    )


@pytest.fixture
def ctx(artifacts) -> Context:
    tester = EthereumTester()
    w3 = Web3(EthereumTesterProvider(tester))
    token_artifact, jobs_artifact = artifacts
    token = deploy(w3, token_artifact, w3.eth.accounts[0])
    jobs = deploy(w3, jobs_artifact, w3.eth.accounts[0], token.address)
    profile_jobs = w3.eth.contract(
        address=jobs.address,
        abi=load_abi("erc8183_reference"),
    )
    accounts = [Account.from_key(key.to_bytes()) for key in tester.backend.account_keys[:3]]

    def sdk(index: int) -> Erc8183Client:
        return Erc8183Client(
            jobs.address,
            account=accounts[index],
            w3=w3,
            contract=profile_jobs,
            token_contract=token,
        )

    return Context(
        tester=tester,
        w3=w3,
        token=token,
        jobs=jobs,
        client=sdk(0),
        provider=sdk(1),
        evaluator=sdk(2),
        now=w3.eth.get_block("latest")["timestamp"],
    )


def create(ctx: Context, *, expiry_offset: int = 1_000) -> int:
    result = ctx.client.create_job(
        provider=ctx.provider.account.address,
        evaluator=ctx.evaluator.account.address,
        expired_at=ctx.now + expiry_offset,
        description="ipfs://job-brief",
    )
    assert result.tx_hash.startswith("0x")
    return result.job_id


def fund(ctx: Context, job_id: int) -> None:
    tx_hash = ctx.token.functions.mint(ctx.client.account.address, AMOUNT).transact(
        {"from": ctx.w3.eth.accounts[0]}
    )
    ctx.w3.eth.wait_for_transaction_receipt(tx_hash)
    ctx.provider.set_budget(job_id, AMOUNT)
    ctx.client.approve_payment(AMOUNT)
    ctx.client.fund(job_id, expected_budget=AMOUNT)


def test_complete_lifecycle_moves_the_exact_budget(ctx):
    job_id = create(ctx)
    fund(ctx, job_id)
    assert ctx.client.get_job(job_id).status is Erc8183Status.FUNDED
    assert ctx.token.functions.balances(ctx.jobs.address).call() == AMOUNT

    deliverable = hash_content("delivered work")
    reason = hash_content("accepted by evaluator")
    ctx.provider.submit(job_id, deliverable)
    assert ctx.client.get_job(job_id).status is Erc8183Status.SUBMITTED
    ctx.evaluator.complete(job_id, reason)

    job = ctx.client.get_job(job_id)
    assert job.status is Erc8183Status.COMPLETED
    assert job.description == "ipfs://job-brief"
    assert ctx.token.functions.balances(ctx.provider.account.address).call() == AMOUNT
    assert ctx.token.functions.balances(ctx.jobs.address).call() == 0


def test_evaluator_rejection_refunds_the_client(ctx):
    job_id = create(ctx)
    fund(ctx, job_id)
    ctx.provider.submit(job_id, hash_content("bad work"))
    ctx.evaluator.reject(job_id, hash_content("failed validation"))

    assert ctx.client.get_job(job_id).status is Erc8183Status.REJECTED
    assert ctx.token.functions.balances(ctx.client.account.address).call() == AMOUNT
    assert ctx.token.functions.balances(ctx.provider.account.address).call() == 0


def test_permissionless_expiry_refunds_the_client(ctx):
    job_id = create(ctx, expiry_offset=100)
    fund(ctx, job_id)
    ctx.tester.time_travel(ctx.now + 100)
    ctx.tester.mine_block()

    ctx.provider.claim_refund(job_id)

    assert ctx.client.get_job(job_id).status is Erc8183Status.EXPIRED
    assert ctx.token.functions.balances(ctx.client.account.address).call() == AMOUNT


def test_contract_enforces_role_authorization(ctx):
    job_id = create(ctx)
    with pytest.raises(TransactionFailed, match="unauthorized"):
        ctx.client.set_budget(job_id, AMOUNT)

    fund(ctx, job_id)
    with pytest.raises(TransactionFailed, match="unauthorized"):
        ctx.client.submit(job_id, hash_content("work"))


def test_client_refuses_a_changed_budget_before_funding(ctx):
    job_id = create(ctx)
    ctx.provider.set_budget(job_id, AMOUNT + 1)
    with pytest.raises(WorkflowError, match="budget changed"):
        ctx.client.fund(job_id, expected_budget=AMOUNT)


def test_work_order_creation_checks_signer_chain_contract_and_token(ctx):
    order = WorkOrder(
        escrow=ctx.jobs.address,
        payer=ctx.client.account.address,
        provider=ctx.provider.account.address,
        validator=ctx.evaluator.account.address,
        asset=ctx.token.address,
        amount=AMOUNT,
        chain_id=ctx.w3.eth.chain_id,
        delivery_deadline=ctx.now + 500,
        refund_after=ctx.now + 1_000,
        task_hash=hash_content("task"),
        nonce="0x" + "01" * 32,
    )

    result = ctx.client.create_from_work_order(order)
    job = ctx.client.get_job(result.job_id)
    assert job.description.endswith(order.order_hash)
    assert job.budget == 0

    with pytest.raises(WorkflowError, match="payer"):
        ctx.provider.create_from_work_order(order)
    with pytest.raises(WorkflowError, match="different chain"):
        ctx.client.create_from_work_order(
            order.model_copy(update={"chain_id": order.chain_id + 1})
        )
