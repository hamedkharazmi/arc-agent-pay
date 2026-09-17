"""
Core data models for arc-agent-pay.
Every other module imports from here — keep this dependency-free.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
import re
import time
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Chain(str, Enum):
    """Supported blockchains. Arc testnet is the primary target."""
    ARC_TESTNET = "ARC-TESTNET"
    BASE        = "BASE"
    BASE_SEPOLIA = "BASE-SEPOLIA"
    ETHEREUM    = "ETH"


class WalletType(str, Enum):
    AGENT = "agent"
    LOCAL = "local"


class PaymentStatus(str, Enum):
    PENDING   = "pending"
    AUTHORIZED = "authorized"  # payment payload signed; paid request not resolved yet
    SUCCESS   = "success"
    FAILED    = "failed"
    SKIPPED   = "skipped"   # budget guard blocked it
    UNKNOWN   = "unknown"   # signed/submitted, but settlement outcome is ambiguous


# ---------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------

class Wallet(BaseModel):
    """A Circle agent wallet, provisioned and ready to spend."""
    address: str
    chain: Chain
    wallet_type: WalletType = WalletType.AGENT
    testnet: bool = True
    # Balances are strings to avoid float precision issues
    usdc_balance: Optional[str] = None
    gateway_balance: Optional[str] = None

    @field_validator("address")
    @classmethod
    def address_must_be_hex(cls, v: str) -> str:
        if not v.startswith("0x") or len(v) != 42:
            raise ValueError(f"Invalid EVM address: {v!r}")
        return v.lower()


# ---------------------------------------------------------------------------
# Service (x402-compatible API endpoint)
# ---------------------------------------------------------------------------

_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_ZERO_EVM_ADDRESS = "0x" + "0" * 40


def _normalize_nonzero_evm_address(value: str) -> str:
    """Normalize an EVM address using the workflow models' strict convention."""
    if not isinstance(value, str) or not _EVM_ADDRESS_RE.fullmatch(value):
        raise ValueError("must be a 20-byte 0x-prefixed EVM address")
    normalized = value.lower()
    if normalized == _ZERO_EVM_ADDRESS:
        raise ValueError("zero address is not allowed")
    return normalized


class AssuranceTerms(BaseModel):
    """Machine-readable warranty terms advertised with an assured service.

    Phase 1 intentionally supports only a full refund and a short list of plain
    acceptance criteria.  The model is optional on :class:`Service`, preserving
    the existing catalog shape for services that do not offer Assurance.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "1"
    provider_address: str
    warranty_bond_address: str
    acceptance_criteria: list[str] = Field(min_length=1)
    max_response_time_ms: Optional[int] = Field(default=None, gt=0)
    dispute_window_seconds: int = Field(gt=0)
    refund_bps: Literal[10000] = 10000

    _addresses = field_validator(
        "provider_address", "warranty_bond_address", mode="before"
    )(_normalize_nonzero_evm_address)

    @field_validator("version")
    @classmethod
    def version_must_not_be_empty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("version must not be empty")
        return normalized

    @field_validator("acceptance_criteria")
    @classmethod
    def criteria_must_not_be_empty(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("acceptance criteria must not contain empty entries")
        return normalized


class Service(BaseModel):
    """A paid API endpoint discovered via the registry."""
    name: str
    url: str
    description: str = ""
    price_usdc: str = "0.001"          # per-request price as string
    chain: Chain = Chain.ARC_TESTNET
    tags: list[str] = Field(default_factory=list)
    # Set to True for locally registered mock services
    is_builtin: bool = False
    # The provider's ERC-8004 agent id, if it advertises an on-chain identity.
    # When set, a trust policy can gate payment on the provider's reputation.
    provider_agent_id: Optional[int] = None
    # Optional post-payment warranty terms. Existing service JSON remains valid.
    assurance: Optional[AssuranceTerms] = None

    @property
    def price_decimal(self) -> Decimal:
        return Decimal(self.price_usdc)


# ---------------------------------------------------------------------------
# Payment record
# ---------------------------------------------------------------------------

class Payment(BaseModel):
    """Record of a nanopayment made by the interceptor.

    The original five fields remain required/compatible.  The optional protocol
    fields make the same model useful as a durable audit record without forcing
    existing callers to migrate.
    """
    service_url: str
    amount_usdc: str
    chain: Chain
    status: PaymentStatus = PaymentStatus.PENDING
    tx_reference: Optional[str] = None   # gateway reference / tx hash
    error: Optional[str] = None
    payment_id: Optional[str] = None
    request_key: Optional[str] = None
    method: str = "GET"
    scheme: str = "exact"
    network: Optional[str] = None
    asset: Optional[str] = None
    pay_to: Optional[str] = None
    amount_atomic: Optional[str] = None
    response_status: Optional[int] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Onchain agent identity (ERC-8004)
# ---------------------------------------------------------------------------

class AgentProfile(BaseModel):
    """An agent's onchain identity + reputation (ERC-8004 registries on Arc)."""
    agent_id: int
    address: str
    metadata_uri: str = ""
    reputation_score: Optional[float] = None  # aggregate summary value
    feedback_count: int = 0


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class Budget(BaseModel):
    """
    Spending envelope for a session.
    The BudgetGuard in budget.py enforces this before every payment.
    """
    total_usdc: str                      # max spend for this session
    spent_usdc: str = "0"
    currency: str = "USDC"

    @property
    def remaining(self) -> Decimal:
        return Decimal(self.total_usdc) - Decimal(self.spent_usdc)

    @property
    def is_exhausted(self) -> bool:
        return self.remaining <= Decimal("0")

    def can_afford(self, amount: str) -> bool:
        return Decimal(amount) <= self.remaining


# ---------------------------------------------------------------------------
# PaymentRequest  (parsed from a 402 response)
# ---------------------------------------------------------------------------

class PaymentRequest(BaseModel):
    """
    Structured form of what an x402 server sends back in a 402 response.
    Circle Gateway uses EIP-3009 transfer-with-authorization.

    Typical 402 body (Circle Gateway format):
    {
        "x402Version": 1,
        "accepts": [{
            "scheme": "exact",
            "network": "arc-testnet",
            "maxAmountRequired": "1000",   # in USDC base units (6 decimals)
            "resource": "https://...",
            "description": "...",
            "mimeType": "application/json",
            "payTo": "0x...",
            "maxTimeoutSeconds": 60,
            "asset": "0x..."              # USDC contract address
        }]
    }
    """
    version: int = 1
    scheme: str = "exact"
    network: str = "arc-testnet"
    max_amount_required: str           # raw units (USDC has 6 decimals)
    resource: str
    pay_to: str
    asset: str                         # USDC contract address on chain
    max_timeout_seconds: int = 60
    description: str = ""

    @property
    def amount_usdc(self) -> Decimal:
        """Convert raw 6-decimal units to human USDC amount."""
        return Decimal(self.max_amount_required) / Decimal("1000000")
