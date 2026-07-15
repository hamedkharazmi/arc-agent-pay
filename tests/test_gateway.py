"""
Tests for gateway.py.

The Circle Gateway integration shells out to an inline Node.js script. These
tests mock the Node binary lookup and the subprocess so nothing executes Node
or touches the network — we only verify script construction, JSON parsing, and
error mapping.
"""

from __future__ import annotations

import json

import pytest

import arc_agent_pay.gateway as gateway_mod
from arc_agent_pay.exceptions import PaymentFailedError, WalletFundingError
from arc_agent_pay.gateway import CHAIN_TO_GATEWAY, GatewayManager, _run_node
from arc_agent_pay.models import Chain, Wallet


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, input: bytes | None = None):  # noqa: A002
        # capture the script piped over stdin for assertions
        _FakeProc.last_input = input
        return self._stdout, self._stderr


@pytest.fixture
def mock_node(monkeypatch):
    """Patch node lookup + subprocess; configure stdout/stderr/returncode."""
    monkeypatch.setattr(gateway_mod.shutil, "which", lambda _: "/usr/bin/node")

    def configure(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        async def fake_exec(*_cmd, **_kwargs):
            return _FakeProc(stdout, stderr, returncode)

        monkeypatch.setattr(gateway_mod.asyncio, "create_subprocess_exec", fake_exec)

    return configure


def _wallet() -> Wallet:
    return Wallet(address="0x" + "a" * 40, chain=Chain.ARC_TESTNET, testnet=True)


# ---------------------------------------------------------------------------
# _run_node
# ---------------------------------------------------------------------------

async def test_run_node_missing_node_raises(monkeypatch):
    monkeypatch.setattr(gateway_mod.shutil, "which", lambda _: None)
    with pytest.raises(EnvironmentError):
        await _run_node("console.log('{}')")


async def test_run_node_parses_json(mock_node):
    mock_node(stdout=json.dumps({"ok": True}).encode())
    assert await _run_node("...") == {"ok": True}


async def test_run_node_plain_text_wrapped(mock_node):
    mock_node(stdout=b"hello")
    assert await _run_node("...") == {"output": "hello"}


async def test_run_node_nonzero_raises(mock_node):
    mock_node(stderr=b"boom", returncode=1)
    with pytest.raises(RuntimeError):
        await _run_node("...")


# ---------------------------------------------------------------------------
# GatewayManager construction + script content
# ---------------------------------------------------------------------------

def test_chain_name_resolution():
    gm = GatewayManager(_wallet(), private_key="0xkey")
    assert gm.chain_name == CHAIN_TO_GATEWAY[Chain.ARC_TESTNET] == "arcTestnet"


def test_client_init_script_includes_chain_and_key():
    gm = GatewayManager(_wallet(), private_key="0xsecret", rpc_url="https://rpc")
    script = gm._client_init_script()
    assert 'chain: "arcTestnet"' in script
    assert 'privateKey: "0xsecret"' in script
    assert 'rpcUrl: "https://rpc"' in script


# ---------------------------------------------------------------------------
# GatewayManager operations
# ---------------------------------------------------------------------------

async def test_get_balances_parses(mock_node):
    payload = {"gateway": {"formattedAvailable": "0.95"}}
    mock_node(stdout=json.dumps(payload).encode())
    gm = GatewayManager(_wallet(), private_key="0xkey")
    assert await gm.available_usdc() == "0.95"


async def test_deposit_failure_maps_to_wallet_funding_error(mock_node):
    mock_node(stderr=b"insufficient gas", returncode=1)
    gm = GatewayManager(_wallet(), private_key="0xkey")
    with pytest.raises(WalletFundingError):
        await gm.deposit("1.00")


async def test_withdraw_failure_maps_to_payment_failed_error(mock_node):
    mock_node(stderr=b"revert", returncode=1)
    gm = GatewayManager(_wallet(), private_key="0xkey")
    with pytest.raises(PaymentFailedError):
        await gm.withdraw("1.00")


async def test_supports_batching_reads_flag(mock_node):
    mock_node(stdout=json.dumps({"supported": True}).encode())
    gm = GatewayManager(_wallet(), private_key="0xkey")
    assert await gm.supports_batching("https://api.example.com/x402") is True
