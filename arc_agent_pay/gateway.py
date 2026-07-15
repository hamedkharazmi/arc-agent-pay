"""
gateway.py — Circle Gateway integration for arc-agent-pay.

The Circle Gateway SDK (@circle-fin/x402-batching) is TypeScript-only.
This module wraps it via Node subprocess for deposit/withdraw/balance ops.

For the PAYMENT flow itself (signing EIP-3009 + retrying), we use the
official Python x402 SDK (pip install x402[httpx,evm]) which handles
all EVM signing natively. The GatewayManager here is only needed for:
  - Initial USDC deposit into the Gateway Wallet contract  (one-time, onchain)
  - Checking Gateway balance
  - Withdrawing funds after a session
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Optional

from .exceptions import PaymentFailedError, WalletFundingError
from .models import Chain, Wallet


# ---------------------------------------------------------------------------
# Chain name mapping: our Chain enum → @circle-fin/x402-batching chain names
# ---------------------------------------------------------------------------

CHAIN_TO_GATEWAY: dict[Chain, str] = {
    Chain.ARC_TESTNET:   "arcTestnet",
    Chain.BASE_SEPOLIA:  "baseSepolia",
    Chain.BASE:          "base",
    Chain.ETHEREUM:      "mainnet",
}


# ---------------------------------------------------------------------------
# Internal helper: run a small inline Node.js script
# ---------------------------------------------------------------------------

async def _run_node(script: str) -> dict:
    """
    Execute an inline Node.js ESM script and return parsed JSON from stdout.
    Raises RuntimeError on non-zero exit.
    """
    node = shutil.which("node")
    if node is None:
        raise EnvironmentError(
            "Node.js is not installed or not on PATH.\n"
            "Install from https://nodejs.org (LTS recommended)."
        )

    proc = await asyncio.create_subprocess_exec(
        node, "--input-type=module",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=script.encode())

    if proc.returncode != 0:
        raise RuntimeError(
            f"Node script failed (exit {proc.returncode}):\n"
            f"{stderr.decode().strip()}"
        )

    text = stdout.decode().strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"output": text}


# ---------------------------------------------------------------------------
# GatewayManager
# ---------------------------------------------------------------------------

class GatewayManager:
    """
    Manages Circle Gateway USDC balance for a single agent wallet.

    Wraps @circle-fin/x402-batching's GatewayClient via Node subprocess
    for deposit/withdraw/balance ops.

    The x402 payment signing itself (what happens on every 402 intercept)
    is handled by the official x402 Python SDK in interceptor.py.
    This class handles the surrounding lifecycle:
      - Deposit USDC into the Gateway contract once
      - Check available Gateway balance
      - Withdraw after a session ends

    Usage:
        gm = GatewayManager(wallet, private_key=os.environ["AGENT_PRIVATE_KEY"])
        await gm.deposit("1.00")           # one-time onchain tx, needs gas
        balances = await gm.get_balances()
        print(balances["gateway"]["formattedAvailable"])
    """

    def __init__(
        self,
        wallet: Wallet,
        private_key: str,
        rpc_url: Optional[str] = None,
    ) -> None:
        self.wallet = wallet
        self.private_key = private_key
        self.rpc_url = rpc_url
        self.chain_name = CHAIN_TO_GATEWAY.get(wallet.chain, "arcTestnet")

    def _client_init_script(self) -> str:
        """GatewayClient constructor as an inline ESM snippet."""
        rpc_line = f', rpcUrl: "{self.rpc_url}"' if self.rpc_url else ""
        return (
            'import { GatewayClient } from "@circle-fin/x402-batching/client";\n'
            f'const client = new GatewayClient({{\n'
            f'  chain: "{self.chain_name}",\n'
            f'  privateKey: "{self.private_key}"{rpc_line}\n'
            f'}});\n'
        )

    # bigint-safe JSON serializer for Node scripts
    _BIGINT_REPLACER = '(_, v) => typeof v === "bigint" ? v.toString() : v'

    async def deposit(self, amount_usdc: str) -> dict:
        """
        Deposit USDC from the agent wallet into the Gateway Wallet contract.

        This is a ONE-TIME onchain transaction (requires gas).
        After depositing, all subsequent x402 payments are gasless off-chain
        EIP-3009 authorizations — no more gas needed per payment.

        Args:
            amount_usdc: Decimal USDC string, e.g. "1.00"

        Returns:
            dict with depositTxHash, amount (raw), formattedAmount.
        """
        script = (
            self._client_init_script()
            + f'const result = await client.deposit("{amount_usdc}");\n'
            + f'console.log(JSON.stringify(result, {self._BIGINT_REPLACER}));\n'
        )
        try:
            return await _run_node(script)
        except RuntimeError as e:
            raise WalletFundingError(f"Gateway deposit failed: {e}") from e

    async def get_balances(self) -> dict:
        """
        Return wallet USDC balance and Gateway balance.

        Returns:
            {
                "wallet":  {"balance": "str", "formatted": "1.00"},
                "gateway": {
                    "total": "str",
                    "available": "str",
                    "withdrawing": "str",
                    "withdrawable": "str",
                    "formattedTotal": "1.00",
                    "formattedAvailable": "0.95",
                }
            }
        """
        script = (
            self._client_init_script()
            + 'const result = await client.getBalances();\n'
            + f'console.log(JSON.stringify(result, {self._BIGINT_REPLACER}));\n'
        )
        return await _run_node(script)

    async def available_usdc(self) -> str:
        """Convenience: return just the available Gateway balance as a string."""
        balances = await self.get_balances()
        return balances.get("gateway", {}).get("formattedAvailable", "0")

    async def withdraw(
        self,
        amount_usdc: str,
        destination_chain: Optional[Chain] = None,
        recipient: Optional[str] = None,
    ) -> dict:
        """
        Withdraw USDC from Gateway balance back to a wallet.

        Args:
            amount_usdc:       Decimal USDC string.
            destination_chain: Defaults to same chain as the wallet.
            recipient:         EVM address (defaults to the agent wallet address).

        Returns:
            dict with mintTxHash, amount, formattedAmount, sourceChain,
            destinationChain, recipient.
        """
        options: dict = {}
        if destination_chain:
            options["chain"] = CHAIN_TO_GATEWAY.get(destination_chain, "arcTestnet")
        if recipient:
            options["recipient"] = recipient

        opts_json = json.dumps(options) if options else "{}"
        script = (
            self._client_init_script()
            + f'const result = await client.withdraw("{amount_usdc}", {opts_json});\n'
            + f'console.log(JSON.stringify(result, {self._BIGINT_REPLACER}));\n'
        )
        try:
            return await _run_node(script)
        except RuntimeError as e:
            raise PaymentFailedError(f"Gateway withdrawal failed: {e}") from e

    async def supports_batching(self, url: str) -> bool:
        """
        Check if a URL's x402 endpoint supports Circle Gateway batched payments.
        Returns True if the GatewayWalletBatched scheme is advertised.
        """
        script = (
            self._client_init_script()
            + f'const result = await client.supports("{url}");\n'
            + 'console.log(JSON.stringify(result));\n'
        )
        result = await _run_node(script)
        return bool(result.get("supported", False))