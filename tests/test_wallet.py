"""
Tests for wallet.py.

All tests are pure in-memory: the Circle CLI binary and the subprocess call
are mocked, so nothing touches the network or the real `circle` CLI.
"""

from __future__ import annotations

import json

import pytest

import arc_agent_pay.wallet as wallet_mod
from arc_agent_pay.exceptions import CLINotFoundError, WalletAuthError, WalletFundingError
from arc_agent_pay.models import Chain, Wallet, WalletType
from arc_agent_pay.wallet import WalletManager, _ensure_cli, _run

# Valid checksum-agnostic EVM addresses for model construction
ADDR_A = "0x" + "a" * 40
ADDR_B = "0x" + "b" * 40
ADDR_NEW = "0x" + "c" * 40


# ---------------------------------------------------------------------------
# Subprocess mocking helpers
# ---------------------------------------------------------------------------

class _FakeProc:
    """Stand-in for the object returned by asyncio.create_subprocess_exec."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, input: bytes | None = None):  # noqa: A002
        return self._stdout, self._stderr


@pytest.fixture
def mock_cli(monkeypatch):
    """
    Patch the CLI lookup + subprocess. Returns a setter that configures the
    fake process output for the next `_run` call, and captures the command.
    """
    monkeypatch.setattr(wallet_mod.shutil, "which", lambda _: "/usr/bin/circle")

    captured: dict = {}

    def configure(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        async def fake_exec(*cmd, **_kwargs):
            captured["cmd"] = list(cmd)
            return _FakeProc(stdout, stderr, returncode)

        monkeypatch.setattr(wallet_mod.asyncio, "create_subprocess_exec", fake_exec)
        return captured

    return configure


# ---------------------------------------------------------------------------
# _ensure_cli
# ---------------------------------------------------------------------------

def test_ensure_cli_found(monkeypatch):
    monkeypatch.setattr(wallet_mod.shutil, "which", lambda _: "/usr/bin/circle")
    assert _ensure_cli() == "/usr/bin/circle"


def test_ensure_cli_missing_raises(monkeypatch):
    monkeypatch.setattr(wallet_mod.shutil, "which", lambda _: None)
    with pytest.raises(CLINotFoundError):
        _ensure_cli()


# ---------------------------------------------------------------------------
# _run
# ---------------------------------------------------------------------------

async def test_run_parses_json(mock_cli):
    captured = mock_cli(stdout=json.dumps({"address": "0xabc"}).encode())
    result = await _run("wallet", "list")
    assert result == {"address": "0xabc"}
    # --output json --quiet always appended
    assert captured["cmd"][-3:] == ["--output", "json", "--quiet"]


async def test_run_empty_stdout_returns_empty_dict(mock_cli):
    mock_cli(stdout=b"")
    assert await _run("wallet", "status") == {}


async def test_run_plain_text_is_wrapped(mock_cli):
    mock_cli(stdout=b"not json at all")
    assert await _run("wallet", "x") == {"output": "not json at all"}


async def test_run_auth_failure_raises_wallet_auth_error(mock_cli):
    mock_cli(stderr=b"Session expired, please login again", returncode=1)
    with pytest.raises(WalletAuthError):
        await _run("wallet", "list")


async def test_run_other_failure_raises_runtime_error(mock_cli):
    mock_cli(stderr=b"network unreachable", returncode=2)
    with pytest.raises(RuntimeError):
        await _run("wallet", "list")


# ---------------------------------------------------------------------------
# WalletManager.balance
# ---------------------------------------------------------------------------

async def test_balance_reads_usdc_field(mock_cli):
    mock_cli(stdout=json.dumps({"USDC": "2.000000"}).encode())
    mgr = WalletManager(testnet=True)
    w = Wallet(address=ADDR_A, chain=Chain.ARC_TESTNET, testnet=True)
    assert await mgr.balance(w) == "2.000000"


async def test_balance_defaults_to_zero(mock_cli):
    mock_cli(stdout=json.dumps({"unexpected": "shape"}).encode())
    mgr = WalletManager(testnet=True)
    w = Wallet(address=ADDR_A, chain=Chain.ARC_TESTNET, testnet=True)
    assert await mgr.balance(w) == "0"


# ---------------------------------------------------------------------------
# WalletManager.get_or_create
# ---------------------------------------------------------------------------

async def test_get_or_create_returns_existing(monkeypatch):
    mgr = WalletManager(testnet=True)
    existing = Wallet(
        address=ADDR_B,
        chain=Chain.ARC_TESTNET,
        wallet_type=WalletType.AGENT,
        testnet=True,
    )

    async def fake_list(_chain):
        return [existing]

    async def fake_balance(_w):
        return "5.0"

    monkeypatch.setattr(mgr, "list_wallets", fake_list)
    monkeypatch.setattr(mgr, "balance", fake_balance)

    w = await mgr.get_or_create(Chain.ARC_TESTNET)
    assert w.address == ADDR_B
    assert w.usdc_balance == "5.0"


async def test_get_or_create_creates_when_none(monkeypatch):
    mgr = WalletManager(testnet=True)
    created = Wallet(address=ADDR_NEW, chain=Chain.ARC_TESTNET, testnet=True)

    async def fake_list(_chain):
        return []

    async def fake_create(_chain):
        return created

    monkeypatch.setattr(mgr, "list_wallets", fake_list)
    monkeypatch.setattr(mgr, "create_wallet", fake_create)

    w = await mgr.get_or_create(Chain.ARC_TESTNET)
    assert w.address == ADDR_NEW


# ---------------------------------------------------------------------------
# WalletManager.fund_from_faucet
# ---------------------------------------------------------------------------

async def test_fund_from_faucet_rejects_mainnet():
    mgr = WalletManager(testnet=False)
    w = Wallet(address=ADDR_A, chain=Chain.ARC_TESTNET, testnet=False)
    with pytest.raises(WalletFundingError):
        await mgr.fund_from_faucet(w)
