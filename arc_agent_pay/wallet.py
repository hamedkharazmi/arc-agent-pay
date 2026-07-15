"""
WalletManager — Layer 1 of arc-agent-pay.

Wraps the Circle CLI (npm package @circle-fin/cli) via subprocess.
Python developers never touch the CLI directly; they call this class.

The CLI follows the pattern:
    circle <resource> <verb> [--option value] [--output json]

All methods use --output json so output is machine-parseable.
Async methods use asyncio.create_subprocess_exec for non-blocking calls.

Prerequisites (one-time, done by the developer):
    npm install -g @circle-fin/cli
    circle wallet login <email> --testnet
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Optional

from .exceptions import (
    CLINotFoundError,
    WalletAuthError,
    WalletFundingError,
)
from .models import Chain, Wallet, WalletType


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_cli() -> str:
    """Return path to `circle` binary or raise CLINotFoundError."""
    path = shutil.which("circle")
    if path is None:
        raise CLINotFoundError(
            "Circle CLI not found. Install with:\n"
            "  npm install -g @circle-fin/cli\n"
            "Then authenticate:\n"
            "  circle wallet login <email> --testnet"
        )
    return path


async def _run(*args: str) -> dict:
    """
    Run `circle <args> --output json --quiet` and return parsed JSON.
    Raises WalletAuthError on auth failures, RuntimeError on other non-zero exits.
    """
    cli = _ensure_cli()
    cmd = [cli, *args, "--output", "json", "--quiet"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    stdout_text = stdout.decode().strip()
    stderr_text = stderr.decode().strip()

    if proc.returncode != 0:
        combined = stderr_text or stdout_text
        if any(word in combined.lower() for word in ("auth", "login", "session", "otp")):
            raise WalletAuthError(
                f"Circle CLI auth error. Re-run: circle wallet login <email> --testnet\n"
                f"Detail: {combined}"
            )
        raise RuntimeError(f"Circle CLI error (exit {proc.returncode}): {combined}")

    if not stdout_text:
        return {}

    try:
        return json.loads(stdout_text)
    except json.JSONDecodeError:
        # CLI returned plain text (some commands do this) — wrap it
        return {"output": stdout_text}


# ---------------------------------------------------------------------------
# WalletManager
# ---------------------------------------------------------------------------

class WalletManager:
    """
    Manages Circle agent wallets on Arc testnet (default) or mainnet.

    Quick start:
        manager = WalletManager(testnet=True)
        wallet  = await manager.get_or_create(chain=Chain.ARC_TESTNET)
        await manager.fund_from_faucet(wallet)
        print(await manager.balance(wallet))
    """

    def __init__(self, testnet: bool = True) -> None:
        self.testnet = testnet
        self._testnet_flag = ["--testnet"] if testnet else []

    # ------------------------------------------------------------------
    # Authentication helpers (for scripted / agent-driven flows)
    # ------------------------------------------------------------------

    async def init_login(self, email: str) -> str:
        """
        Start a two-step login. Returns the request_id.
        Your agent or script must then supply the OTP via complete_login().

        This is the --init flow documented in the CLI reference.
        """
        result = await _run("wallet", "login", email, "--init", *self._testnet_flag)
        request_id = result.get("requestId") or result.get("request_id")
        if not request_id:
            raise WalletAuthError(
                f"No requestId in --init response: {result}"
            )
        return request_id

    async def complete_login(self, request_id: str, otp: str) -> None:
        """Complete the two-step login with the OTP received by email."""
        await _run(
            "wallet", "login",
            "--request", request_id,
            "--otp", otp,
            *self._testnet_flag,
        )

    async def auth_status(self) -> dict:
        """Return current session status (authenticated, email, expiry)."""
        return await _run("wallet", "status", "--type", "agent")

    # ------------------------------------------------------------------
    # Wallet lifecycle
    # ------------------------------------------------------------------

    async def list_wallets(self, chain: Chain) -> list[Wallet]:
        """Return all agent wallets on a given chain."""
        result = await _run(
            "wallet", "list",
            "--chain", chain.value,
            "--type", "agent",
        )
        wallets_raw = result if isinstance(result, list) else result.get("wallets", [])
        return [
            Wallet(
                address=w["address"],
                chain=chain,
                wallet_type=WalletType.AGENT,
                testnet=self.testnet,
            )
            for w in wallets_raw
        ]

    async def create_wallet(
        self,
        chain: Chain = Chain.ARC_TESTNET,
        idempotency_key: Optional[str] = None,
    ) -> Wallet:
        """Create a new agent wallet. Max 5 per account."""
        args = ["wallet", "create", "--type", "agent", *self._testnet_flag]
        if idempotency_key:
            args += ["--idempotency-key", idempotency_key]

        result = await _run(*args)
        address = result.get("address")
        if not address:
            raise RuntimeError(f"No address in wallet create response: {result}")

        return Wallet(address=address, chain=chain, testnet=self.testnet)

    async def get_or_create(self, chain: Chain = Chain.ARC_TESTNET) -> Wallet:
        """
        Return the first existing agent wallet on `chain`, or create one.
        This is the most common entry point — agents call this once at startup.
        """
        wallets = await self.list_wallets(chain)
        if wallets:
            wallet = wallets[0]
            wallet.usdc_balance = await self.balance(wallet)
            return wallet
        return await self.create_wallet(chain)

    # ------------------------------------------------------------------
    # Funding
    # ------------------------------------------------------------------

    async def fund_from_faucet(self, wallet: Wallet) -> None:
        """
        Request testnet USDC from the Circle faucet.
        On Arc Testnet, omitting --amount gives 2 USDC automatically.
        Only works when testnet=True.
        """
        if not self.testnet:
            raise WalletFundingError("Faucet is only available on testnet.")

        await _run(
            "wallet", "fund",
            "--address", wallet.address,
            "--chain", wallet.chain.value,
            # No --amount and no --method on Arc Testnet = faucet mode
        )

    # ------------------------------------------------------------------
    # Balance & status
    # ------------------------------------------------------------------

    async def balance(self, wallet: Wallet) -> str:
        """Return USDC balance as a string (e.g. '1.50')."""
        result = await _run(
            "wallet", "balance",
            "--address", wallet.address,
            "--chain", wallet.chain.value,
        )
        # CLI returns something like {"USDC": "2.000000"} or {"balance": "2.0"}
        usdc = (
            result.get("USDC")
            or result.get("usdc")
            or result.get("balance")
            or "0"
        )
        return str(usdc)

    async def refresh(self, wallet: Wallet) -> Wallet:
        """Return the same wallet with an up-to-date usdc_balance."""
        wallet.usdc_balance = await self.balance(wallet)
        return wallet

    # ------------------------------------------------------------------
    # Spending policy (mainnet only — documented in CLI reference)
    # ------------------------------------------------------------------

    async def set_transfer_limit(
        self,
        wallet: Wallet,
        *,
        per_tx: Optional[str] = None,
        daily: Optional[str] = None,
        weekly: Optional[str] = None,
        monthly: Optional[str] = None,
    ) -> None:
        """
        Set on-chain spending limits for a mainnet wallet.
        Requires a second email OTP (Circle prompts interactively).
        Skipped silently on testnet (limits not supported there).
        """
        if self.testnet:
            return  # CLI docs: testnet chains not supported for limit set

        args = [
            "wallet", "limit", "set",
            "--address", wallet.address,
            "--chain", wallet.chain.value,
            "--policy-type", "stablecoin",
        ]
        if per_tx:
            args += ["--per-tx", per_tx]
        if daily:
            args += ["--daily", daily]
        if weekly:
            args += ["--weekly", weekly]
        if monthly:
            args += ["--monthly", monthly]

        await _run(*args)

    async def get_budget_remaining(self, wallet: Wallet) -> dict:
        """
        Return on-chain remaining budget for a mainnet wallet.
        Returns empty dict on testnet.
        """
        if self.testnet:
            return {}
        return await _run(
            "wallet", "limit", "budget",
            "--address", wallet.address,
        )