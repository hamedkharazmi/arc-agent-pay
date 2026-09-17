"""Compiled-contract tests for the trusted-resolver WarrantyBond MVP."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from eth_tester import EthereumTester
from eth_tester.exceptions import TransactionFailed
from eth_utils import keccak
from vyper import compile_code
from web3 import Web3
from web3.providers.eth_tester import EthereumTesterProvider


ROOT = Path(__file__).parents[2]
ZERO_ADDRESS = "0x" + "0" * 40
DEPOSIT_AMOUNT = 500_000
REFUND_AMOUNT = 125_000


@dataclass
class Context:
    w3: Web3
    token: object
    bond: object
    resolver: str
    provider: str
    buyer: str
    attacker: str


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


def transact(w3: Web3, fn, sender: str):
    tx_hash = fn.transact({"from": sender})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    assert receipt["status"] == 1
    return receipt


@pytest.fixture(scope="session")
def artifacts() -> tuple[dict, dict]:
    return (
        compile_contract("contracts/test/MockWarrantyToken.vy"),
        compile_contract("contracts/WarrantyBond.vy"),
    )


@pytest.fixture
def ctx(artifacts) -> Context:
    w3 = Web3(EthereumTesterProvider(EthereumTester()))
    resolver, provider, buyer, attacker = w3.eth.accounts[:4]
    token_artifact, bond_artifact = artifacts
    token = deploy(w3, token_artifact, resolver)
    bond = deploy(w3, bond_artifact, resolver, token.address, resolver)
    return Context(
        w3=w3,
        token=token,
        bond=bond,
        resolver=resolver,
        provider=provider,
        buyer=buyer,
        attacker=attacker,
    )


def deposit(ctx: Context, amount: int = DEPOSIT_AMOUNT) -> None:
    transact(ctx.w3, ctx.token.functions.mint(ctx.provider, amount), ctx.resolver)
    transact(ctx.w3, ctx.token.functions.approve(ctx.bond.address, amount), ctx.provider)
    transact(ctx.w3, ctx.bond.functions.deposit(amount), ctx.provider)


def dispute_id(label: str = "dispute-1") -> bytes:
    return keccak(text=label)


def payment_id_hash(label: str = "payment-1") -> bytes:
    return keccak(text=label)


def test_provider_deposit_credits_only_the_caller(ctx):
    transact(ctx.w3, ctx.token.functions.mint(ctx.provider, DEPOSIT_AMOUNT), ctx.resolver)
    transact(
        ctx.w3,
        ctx.token.functions.approve(ctx.bond.address, DEPOSIT_AMOUNT),
        ctx.provider,
    )
    receipt = transact(
        ctx.w3,
        ctx.bond.functions.deposit(DEPOSIT_AMOUNT),
        ctx.provider,
    )

    assert ctx.bond.functions.bond_balance(ctx.provider).call() == DEPOSIT_AMOUNT
    assert ctx.bond.functions.bond_balance(ctx.buyer).call() == 0
    assert ctx.token.functions.balances(ctx.provider).call() == 0
    assert ctx.token.functions.balances(ctx.bond.address).call() == DEPOSIT_AMOUNT
    event = ctx.bond.events.BondDeposited().process_receipt(receipt)[0]["args"]
    assert event["provider"] == ctx.provider
    assert event["amount"] == DEPOSIT_AMOUNT


def test_zero_deposit_is_rejected(ctx):
    with pytest.raises(TransactionFailed, match="zero amount"):
        ctx.bond.functions.deposit(0).transact({"from": ctx.provider})
    assert ctx.bond.functions.bond_balance(ctx.provider).call() == 0


def test_resolver_refund_transfers_exact_full_amount_and_debits_bond(ctx):
    deposit(ctx)
    receipt = transact(
        ctx.w3,
        ctx.bond.functions.refund(
            dispute_id(),
            payment_id_hash(),
            ctx.provider,
            ctx.buyer,
            REFUND_AMOUNT,
        ),
        ctx.resolver,
    )

    assert ctx.token.functions.balances(ctx.buyer).call() == REFUND_AMOUNT
    assert (
        ctx.bond.functions.bond_balance(ctx.provider).call()
        == DEPOSIT_AMOUNT - REFUND_AMOUNT
    )
    assert ctx.token.functions.balances(ctx.bond.address).call() == DEPOSIT_AMOUNT - REFUND_AMOUNT
    assert ctx.bond.functions.processed_disputes(dispute_id()).call() is True
    assert ctx.bond.functions.refunded_payments(payment_id_hash()).call() is True
    event = ctx.bond.events.WarrantyRefunded().process_receipt(receipt)[0]["args"]
    assert event["payment_amount"] == REFUND_AMOUNT
    assert event["buyer"] == ctx.buyer


def test_non_resolver_refund_is_rejected(ctx):
    deposit(ctx)
    with pytest.raises(TransactionFailed, match="not resolver"):
        ctx.bond.functions.refund(
            dispute_id(),
            payment_id_hash(),
            ctx.provider,
            ctx.buyer,
            REFUND_AMOUNT,
        ).transact({"from": ctx.attacker})
    assert ctx.bond.functions.bond_balance(ctx.provider).call() == DEPOSIT_AMOUNT


def test_insufficient_provider_bond_is_rejected_without_consuming_ids(ctx):
    deposit(ctx, REFUND_AMOUNT - 1)
    with pytest.raises(TransactionFailed, match="insufficient bond"):
        ctx.bond.functions.refund(
            dispute_id(),
            payment_id_hash(),
            ctx.provider,
            ctx.buyer,
            REFUND_AMOUNT,
        ).transact({"from": ctx.resolver})
    assert ctx.bond.functions.bond_balance(ctx.provider).call() == REFUND_AMOUNT - 1
    assert ctx.bond.functions.processed_disputes(dispute_id()).call() is False
    assert ctx.bond.functions.refunded_payments(payment_id_hash()).call() is False


def test_same_dispute_id_cannot_be_replayed(ctx):
    deposit(ctx)
    transact(
        ctx.w3,
        ctx.bond.functions.refund(
            dispute_id(),
            payment_id_hash(),
            ctx.provider,
            ctx.buyer,
            REFUND_AMOUNT,
        ),
        ctx.resolver,
    )
    balance = ctx.bond.functions.bond_balance(ctx.provider).call()
    with pytest.raises(TransactionFailed, match="dispute processed"):
        ctx.bond.functions.refund(
            dispute_id(),
            payment_id_hash("payment-2"),
            ctx.provider,
            ctx.buyer,
            REFUND_AMOUNT,
        ).transact({"from": ctx.resolver})
    assert ctx.bond.functions.bond_balance(ctx.provider).call() == balance


def test_same_payment_cannot_be_refunded_through_second_dispute(ctx):
    deposit(ctx)
    transact(
        ctx.w3,
        ctx.bond.functions.refund(
            dispute_id(),
            payment_id_hash(),
            ctx.provider,
            ctx.buyer,
            REFUND_AMOUNT,
        ),
        ctx.resolver,
    )
    balance = ctx.bond.functions.bond_balance(ctx.provider).call()
    with pytest.raises(TransactionFailed, match="payment refunded"):
        ctx.bond.functions.refund(
            dispute_id("dispute-2"),
            payment_id_hash(),
            ctx.provider,
            ctx.buyer,
            REFUND_AMOUNT,
        ).transact({"from": ctx.resolver})
    assert ctx.bond.functions.bond_balance(ctx.provider).call() == balance


@pytest.mark.parametrize(
    ("provider", "buyer", "amount", "message"),
    [
        (ZERO_ADDRESS, "buyer", REFUND_AMOUNT, "zero provider"),
        ("provider", ZERO_ADDRESS, REFUND_AMOUNT, "zero buyer"),
        ("provider", "buyer", 0, "zero amount"),
    ],
)
def test_refund_rejects_zero_provider_buyer_or_amount(ctx, provider, buyer, amount, message):
    deposit(ctx)
    provider_address = ctx.provider if provider == "provider" else provider
    buyer_address = ctx.buyer if buyer == "buyer" else buyer
    with pytest.raises(TransactionFailed, match=message):
        ctx.bond.functions.refund(
            dispute_id(),
            payment_id_hash(),
            provider_address,
            buyer_address,
            amount,
        ).transact({"from": ctx.resolver})
    assert ctx.bond.functions.bond_balance(ctx.provider).call() == DEPOSIT_AMOUNT
    assert ctx.bond.functions.processed_disputes(dispute_id()).call() is False
    assert ctx.bond.functions.refunded_payments(payment_id_hash()).call() is False


def test_failed_token_transfer_rolls_back_balance_and_replay_flags(ctx):
    deposit(ctx)
    transact(
        ctx.w3,
        ctx.token.functions.set_failures(True, False),
        ctx.resolver,
    )
    with pytest.raises(TransactionFailed, match="transfer failed"):
        ctx.bond.functions.refund(
            dispute_id(),
            payment_id_hash(),
            ctx.provider,
            ctx.buyer,
            REFUND_AMOUNT,
        ).transact({"from": ctx.resolver})

    assert ctx.bond.functions.bond_balance(ctx.provider).call() == DEPOSIT_AMOUNT
    assert ctx.bond.functions.processed_disputes(dispute_id()).call() is False
    assert ctx.bond.functions.refunded_payments(payment_id_hash()).call() is False
    assert ctx.token.functions.balances(ctx.buyer).call() == 0
