"""
BudgetGuard — session-level spending enforcement.

Every payment goes through here before touching Circle Gateway.
If the payment would exceed the budget ceiling, InsufficientFundsError
is raised immediately — no network call is made.

Usage:
    guard = BudgetGuard(total_usdc="1.00")
    guard.check("0.001")          # raises if over budget
    guard.record("0.001")         # call after a confirmed payment
    print(guard.budget.remaining) # Decimal('0.999')
"""

from __future__ import annotations

import threading
from decimal import Decimal

from .exceptions import InsufficientFundsError
from .models import Budget


class BudgetGuard:
    """
    Thread-safe spending guard for a single agent session.

    Args:
        total_usdc: Maximum USDC spend for this session, e.g. "1.00"
    """

    def __init__(self, total_usdc: str) -> None:
        self._lock = threading.Lock()
        self.budget = Budget(total_usdc=total_usdc)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def check(self, amount_usdc: str) -> None:
        """
        Raise InsufficientFundsError if `amount_usdc` exceeds remaining budget.
        Call this BEFORE attempting any payment.
        """
        with self._lock:
            if not self.budget.can_afford(amount_usdc):
                raise InsufficientFundsError(
                    required=amount_usdc,
                    remaining=str(self.budget.remaining),
                )

    def record(self, amount_usdc: str) -> None:
        """
        Deduct `amount_usdc` from remaining budget.
        Call this AFTER a payment is confirmed successful.
        """
        with self._lock:
            new_spent = Decimal(self.budget.spent_usdc) + Decimal(amount_usdc)
            self.budget.spent_usdc = str(new_spent)

    def check_and_record(self, amount_usdc: str) -> None:
        """
        Atomic check + record in a single lock acquisition.
        Use this when you want to reserve the amount before the async
        payment call to prevent double-spending in concurrent agents.
        """
        with self._lock:
            if not self.budget.can_afford(amount_usdc):
                raise InsufficientFundsError(
                    required=amount_usdc,
                    remaining=str(self.budget.remaining),
                )
            new_spent = Decimal(self.budget.spent_usdc) + Decimal(amount_usdc)
            self.budget.spent_usdc = str(new_spent)

    def refund(self, amount_usdc: str) -> None:
        """
        Return `amount_usdc` to the budget (e.g. if a payment fails after
        check_and_record was already called).
        """
        with self._lock:
            new_spent = Decimal(self.budget.spent_usdc) - Decimal(amount_usdc)
            # Never go below zero
            self.budget.spent_usdc = str(max(new_spent, Decimal("0")))

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def spent(self) -> str:
        return self.budget.spent_usdc

    @property
    def remaining(self) -> str:
        return str(self.budget.remaining)

    @property
    def is_exhausted(self) -> bool:
        return self.budget.is_exhausted

    def summary(self) -> dict:
        return {
            "total_usdc": self.budget.total_usdc,
            "spent_usdc": self.budget.spent_usdc,
            "remaining_usdc": str(self.budget.remaining),
            "exhausted": self.budget.is_exhausted,
        }