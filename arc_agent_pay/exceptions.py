"""
Exceptions raised by arc-agent-pay.
All are subclasses of ArcAgentPayError so callers can catch broadly
or handle specific cases.
"""


class ArcAgentPayError(Exception):
    """Base exception for all arc-agent-pay errors."""


# ---------------------------------------------------------------------------
# Wallet errors
# ---------------------------------------------------------------------------

class CLINotFoundError(ArcAgentPayError):
    """
    Circle CLI (npm package @circle-fin/cli) is not installed or not on PATH.
    Install with: npm install -g @circle-fin/cli
    """


class WalletAuthError(ArcAgentPayError):
    """
    CLI authentication failed or session has expired.
    Re-authenticate with: circle wallet login <email> --testnet
    """


class WalletNotFoundError(ArcAgentPayError):
    """No wallet found for the given address / chain combination."""


class WalletFundingError(ArcAgentPayError):
    """Faucet or funding call failed."""


# ---------------------------------------------------------------------------
# Payment errors
# ---------------------------------------------------------------------------

class InsufficientFundsError(ArcAgentPayError):
    """
    Payment would exceed the session Budget.
    Raised by BudgetGuard before attempting the payment.
    """
    def __init__(self, required: str, remaining: str):
        self.required = required
        self.remaining = remaining
        super().__init__(
            f"Insufficient budget: need {required} USDC, "
            f"only {remaining} USDC remaining."
        )


class PaymentFailedError(ArcAgentPayError):
    """Circle Gateway returned an error during payment signing or settlement."""


class PaymentTimeoutError(ArcAgentPayError):
    """A paid request ended without a conclusive settlement response."""

    def __init__(self, message: str, *, payment_id: str | None = None):
        self.payment_id = payment_id
        super().__init__(message)


class InvalidPaymentRequestError(ArcAgentPayError):
    """Could not parse the 402 response body into a PaymentRequest."""


class PaymentPolicyError(ArcAgentPayError):
    """A payment was refused by the caller's autonomous-spending policy."""


class PaymentStoreError(ArcAgentPayError):
    """The durable payment journal could not safely reserve or update a payment."""


# ---------------------------------------------------------------------------
# Registry errors
# ---------------------------------------------------------------------------

class ServiceNotFoundError(ArcAgentPayError):
    """No service matched the search query in local or marketplace registry."""


class ServiceRegistrationError(ArcAgentPayError):
    """Failed to register a local mock service."""


# ---------------------------------------------------------------------------
# Validation-gated workflow errors
# ---------------------------------------------------------------------------

class WorkflowError(ArcAgentPayError):
    """Base exception for validation-gated payment workflows."""


class InvalidVerdictError(WorkflowError):
    """A validation verdict failed binding, timing, or signature checks."""


class InvalidFundingAuthorizationError(WorkflowError):
    """An escrow funding authorization failed binding or signature checks."""
