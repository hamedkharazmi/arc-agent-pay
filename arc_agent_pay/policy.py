"""Pre-signature policy for autonomous HTTP payments.

``BudgetGuard`` remains the small, per-client session envelope.  ``PaymentPolicy``
adds controls that need the complete x402 quote and, for rolling limits, a
payment journal.  Stores evaluate the rolling checks while holding their write
lock/transaction so concurrent agents cannot both pass the same remaining cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Iterable
from urllib.parse import urlparse

from .exceptions import PaymentPolicyError
from .models import Payment


def _optional_decimal(name: str, value: str | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"{name} must be a valid decimal amount") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _normalized(values: Iterable[str] | None, *, lower: bool = False) -> frozenset[str]:
    if values is None:
        return frozenset()
    if isinstance(values, str):
        values = (values,)
    cleaned = (str(value).strip() for value in values)
    return frozenset((value.lower() if lower else value) for value in cleaned if value)


def payment_host(url: str) -> str:
    """Return the normalized hostname used for provider policy and accounting."""
    return (urlparse(url).hostname or "").lower()


@dataclass(frozen=True)
class PaymentTotals:
    """Amounts already committed inside the policy's rolling windows."""

    daily_usdc: Decimal = Decimal("0")
    hourly_count: int = 0
    provider_daily_usdc: Decimal = Decimal("0")


@dataclass(frozen=True)
class PaymentPolicy:
    """Controls which payments an autonomous client may sign.

    Rolling limits are *prospective*: a 0.02 payment is refused when 0.09 has
    already been committed under a 0.10 cap.  Pending and unknown payments count
    until conclusively failed, which is safer than reopening budget after an
    ambiguous network response.
    """

    max_payment_usdc: str | Decimal | None = None
    daily_cap_usdc: str | Decimal | None = None
    max_payments_per_hour: int | None = None
    provider_daily_cap_usdc: str | Decimal | None = None
    allowed_hosts: Iterable[str] | None = None
    blocked_hosts: Iterable[str] | None = None
    allowed_networks: Iterable[str] | None = None
    allowed_assets: Iterable[str] | None = None
    allowed_pay_to: Iterable[str] | None = None
    payments_disabled: bool = False
    fail_closed: bool = True

    _max_payment: Decimal | None = field(init=False, repr=False)
    _daily_cap: Decimal | None = field(init=False, repr=False)
    _provider_daily_cap: Decimal | None = field(init=False, repr=False)
    _allowed_hosts: frozenset[str] = field(init=False, repr=False)
    _blocked_hosts: frozenset[str] = field(init=False, repr=False)
    _allowed_networks: frozenset[str] = field(init=False, repr=False)
    _allowed_assets: frozenset[str] = field(init=False, repr=False)
    _allowed_pay_to: frozenset[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_max_payment", _optional_decimal("max_payment_usdc", self.max_payment_usdc)
        )
        object.__setattr__(
            self, "_daily_cap", _optional_decimal("daily_cap_usdc", self.daily_cap_usdc)
        )
        object.__setattr__(
            self,
            "_provider_daily_cap",
            _optional_decimal("provider_daily_cap_usdc", self.provider_daily_cap_usdc),
        )
        if self.max_payments_per_hour is not None and int(self.max_payments_per_hour) <= 0:
            raise ValueError("max_payments_per_hour must be positive")
        if self.max_payments_per_hour is not None:
            object.__setattr__(self, "max_payments_per_hour", int(self.max_payments_per_hour))
        object.__setattr__(self, "_allowed_hosts", _normalized(self.allowed_hosts, lower=True))
        object.__setattr__(self, "_blocked_hosts", _normalized(self.blocked_hosts, lower=True))
        object.__setattr__(self, "_allowed_networks", _normalized(self.allowed_networks))
        object.__setattr__(self, "_allowed_assets", _normalized(self.allowed_assets, lower=True))
        object.__setattr__(self, "_allowed_pay_to", _normalized(self.allowed_pay_to, lower=True))

    @property
    def has_rolling_limits(self) -> bool:
        return (
            self._daily_cap is not None
            or self.max_payments_per_hour is not None
            or self._provider_daily_cap is not None
        )

    def check_static(self, payment: Payment) -> None:
        """Check quote-local controls that do not require journal totals."""
        if self.payments_disabled:
            raise PaymentPolicyError("payments are disabled by policy")

        amount = Decimal(payment.amount_usdc)
        if not amount.is_finite() or amount < 0:
            raise PaymentPolicyError("payment amount cannot be negative")
        if self._max_payment is not None and amount > self._max_payment:
            raise PaymentPolicyError(
                f"payment {amount} USDC exceeds per-payment maximum {self._max_payment} USDC"
            )

        host = payment_host(payment.service_url)
        if host in self._blocked_hosts:
            raise PaymentPolicyError(f"provider host {host!r} is blocked")
        if self._allowed_hosts and host not in self._allowed_hosts:
            raise PaymentPolicyError(f"provider host {host!r} is not allowlisted")
        if self._allowed_networks and payment.network not in self._allowed_networks:
            raise PaymentPolicyError(f"payment network {payment.network!r} is not allowed")

        asset = (payment.asset or "").lower()
        if self._allowed_assets and asset not in self._allowed_assets:
            raise PaymentPolicyError(f"payment asset {payment.asset!r} is not allowed")
        pay_to = (payment.pay_to or "").lower()
        if self._allowed_pay_to and pay_to not in self._allowed_pay_to:
            raise PaymentPolicyError(f"payment recipient {payment.pay_to!r} is not allowed")

    def check_totals(self, payment: Payment, totals: PaymentTotals) -> None:
        """Check a new reservation against totals computed under a store lock."""
        amount = Decimal(payment.amount_usdc)
        if self._daily_cap is not None and totals.daily_usdc + amount > self._daily_cap:
            raise PaymentPolicyError(
                f"daily cap {self._daily_cap} USDC would be exceeded "
                f"({totals.daily_usdc} committed + {amount} requested)"
            )
        if (
            self.max_payments_per_hour is not None
            and totals.hourly_count + 1 > self.max_payments_per_hour
        ):
            raise PaymentPolicyError(
                f"velocity cap {self.max_payments_per_hour} payments/hour would be exceeded"
            )
        if (
            self._provider_daily_cap is not None
            and totals.provider_daily_usdc + amount > self._provider_daily_cap
        ):
            raise PaymentPolicyError(
                f"provider daily cap {self._provider_daily_cap} USDC would be exceeded "
                f"for {payment_host(payment.service_url)} "
                f"({totals.provider_daily_usdc} committed + {amount} requested)"
            )
