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
# Arc Assurance payment verification errors
# ---------------------------------------------------------------------------

class ArcPaymentVerificationError(ArcAgentPayError):
    """Base error for a payment that cannot be proven from Arc USDC logs."""


class WrongArcChain(ArcPaymentVerificationError):
    """The connected RPC is not Arc Testnet."""


class MissingTransactionReference(ArcPaymentVerificationError):
    """Assurance evidence has no settlement transaction reference."""


class InvalidTransactionReference(ArcPaymentVerificationError):
    """The settlement transaction reference is not a 32-byte transaction hash."""


class TransactionNotFound(ArcPaymentVerificationError):
    """The referenced Arc transaction receipt does not exist."""


class TransactionReverted(ArcPaymentVerificationError):
    """The referenced Arc transaction did not execute successfully."""


class InsufficientConfirmations(ArcPaymentVerificationError):
    """The successful transaction has not reached the configured confirmation depth."""

    def __init__(self, confirmations: int, required: int):
        self.confirmations = confirmations
        self.required = required
        super().__init__(
            f"Arc payment has {confirmations} confirmation(s); {required} required"
        )


class WrongPaymentNetwork(ArcPaymentVerificationError):
    """The selected x402 quote does not target Arc Testnet."""


class WrongPaymentAsset(ArcPaymentVerificationError):
    """The selected x402 quote is not denominated in Arc Testnet USDC."""


class ProviderBindingMismatch(ArcPaymentVerificationError):
    """The selected x402 recipient does not match the snapshotted SLA provider."""


class PaymentTransferNotFound(ArcPaymentVerificationError):
    """No qualifying Arc USDC payment transfer exists in the receipt."""


class AmbiguousPaymentTransfer(ArcPaymentVerificationError):
    """More than one Arc USDC transfer qualifies as the asserted payment."""


class PaymentAmountMismatch(ArcPaymentVerificationError):
    """The Arc USDC transfer amount does not match the selected x402 quote."""


# ---------------------------------------------------------------------------
# GenLayer Assurance adjudication errors
# ---------------------------------------------------------------------------

class GenLayerAdjudicationError(ArcAgentPayError):
    """Base error for GenLayer SLA adjudication submission or verification."""


class AdjudicationPayloadTooLarge(GenLayerAdjudicationError):
    """The canonical adjudication payload exceeds the fixed MVP size limit."""


class GenLayerTransactionNotFinal(GenLayerAdjudicationError):
    """A GenLayer transaction has not reached protocol finality."""


class GenLayerFinalityTimeout(GenLayerTransactionNotFinal):
    """The local wait ended while a GenLayer transaction remained non-final."""

    def __init__(self, transaction_id: str, state: str | None = None):
        self.transaction_id = transaction_id
        self.state = state
        suffix = f"; last lifecycle state: {state}" if state else ""
        super().__init__(
            f"stopped waiting for GenLayer transaction {transaction_id}{suffix}; "
            "the transaction may still finalize and can be resumed"
        )


class GenLayerExecutionFailed(GenLayerAdjudicationError):
    """A finalized GenLayer adjudication did not execute successfully."""


class GenLayerVerdictNotFound(GenLayerAdjudicationError):
    """A finalized transaction did not produce a stored contract verdict."""


class GenLayerVerdictBindingMismatch(GenLayerAdjudicationError):
    """A stored verdict is not bound to the expected adjudication input."""


class GenLayerDeploymentError(GenLayerAdjudicationError):
    """A GenLayer Intelligent Contract deployment could not be safely completed."""


class GenLayerNetworkMismatch(GenLayerDeploymentError):
    """The connected GenLayer chain does not match the explicitly selected network."""


class GenLayerFeeEstimationFailed(GenLayerDeploymentError):
    """The matching GenLayer SDK could not produce a supported fee estimate."""


class GenLayerInsufficientBalance(GenLayerDeploymentError):
    """The operator lacks the estimated GEN needed for a transaction."""


class GenLayerDeploymentArtifactError(GenLayerDeploymentError):
    """A deployment artifact is missing, inconsistent, or unsafe to overwrite."""


class GenLayerTransportRetryExhausted(GenLayerAdjudicationError):
    """A safe GenLayer RPC read exhausted its bounded transport retries."""

    def __init__(self, rpc_method: str, attempts: int):
        self.rpc_method = rpc_method
        self.attempts = attempts
        super().__init__(
            f"transient transport failure persisted for {rpc_method} after "
            f"{attempts} attempt(s)"
        )


class GenLayerSubmissionOutcomeUnknown(GenLayerAdjudicationError):
    """A broadcast transport failure makes blind resubmission unsafe."""

    def __init__(
        self,
        rpc_method: str,
        *,
        evm_transaction_hash: str | None = None,
    ):
        self.rpc_method = rpc_method
        self.evm_transaction_hash = evm_transaction_hash
        detail = (
            f"; EVM transaction hash: {evm_transaction_hash}"
            if evm_transaction_hash
            else ""
        )
        super().__init__(
            f"GenLayer submission outcome is unknown after a transport failure in "
            f"{rpc_method}{detail}; blind resubmission is unsafe"
        )


# ---------------------------------------------------------------------------
# Assurance settlement errors
# ---------------------------------------------------------------------------

class AssuranceSettlementError(ArcAgentPayError):
    """Base error for cross-chain Assurance settlement binding and relay."""


class SettlementBindingError(AssuranceSettlementError):
    """The Arc, evidence, dispute, or GenLayer bindings do not agree."""


class SettlementIneligible(AssuranceSettlementError):
    """A correctly bound dispute is not eligible for a WarrantyBond refund."""


class SettlementPreflightError(AssuranceSettlementError):
    """WarrantyBond configuration or provider coverage failed preflight."""


class SettlementAlreadyProcessed(AssuranceSettlementError):
    """The dispute or payment is already consumed locally or on WarrantyBond."""


class SettlementSubmissionOutcomeUnknown(AssuranceSettlementError):
    """An Arc refund broadcast may have succeeded and must not be repeated."""

    def __init__(self, message: str, *, transaction_hash: str | None = None):
        self.transaction_hash = transaction_hash
        suffix = f"; transaction hash: {transaction_hash}" if transaction_hash else ""
        super().__init__(f"{message}{suffix}; blind resubmission is unsafe")


class SettlementPending(AssuranceSettlementError):
    """A known Arc refund transaction remains resumable by transaction hash."""

    def __init__(self, transaction_hash: str, message: str):
        self.transaction_hash = transaction_hash
        super().__init__(f"{message}; resume Arc transaction {transaction_hash}")


class SettlementTransactionFailed(AssuranceSettlementError):
    """A known Arc refund transaction was mined unsuccessfully."""


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
