"""
arc-agent-pay
=============
A Python SDK that makes it trivial for any AI agent to discover,
pay for, and consume API services using USDC nanopayments on Arc.

Quick start
-----------
    import os
    from arc_agent_pay import PaymentClient, ServiceRegistry
    from arc_agent_pay.models import Chain
    from eth_account import Account

    registry = ServiceRegistry()
    services = registry.search("crypto prices")

    account = Account.from_key("0x" + os.environ["AGENT_PRIVATE_KEY"])

    async with PaymentClient(account=account, budget_usdc="0.05") as client:
        response = await client.get(services[0].url)
        print(response.json())
        print(client.summary())
"""

from .budget import BudgetGuard
from .interceptor import PaymentClient
from .models import Chain, Wallet, Service, Payment, Budget, PaymentRequest
from .registry import Discovery, SemanticServiceRegistry, ServiceRegistry
from .spending import InMemorySpendLedger, SpendCaps, SqliteSpendLedger
from .workflow import (
    DeliveryEvidence,
    SignedValidationVerdict,
    ValidationVerdict,
    Verifier,
    WorkOrder,
    hash_content,
    sign_verdict,
    verify_signed_verdict,
)
from .exceptions import (
    ArcAgentPayError,
    InsufficientFundsError,
    PaymentFailedError,
    PaymentTimeoutError,
    InvalidPaymentRequestError,
    ServiceNotFoundError,
    ServiceRegistrationError,
    WorkflowError,
    InvalidVerdictError,
)

__version__ = "0.1.0"

__all__ = [
    # Core classes
    "PaymentClient",
    "ServiceRegistry",
    "SemanticServiceRegistry",
    "Discovery",
    "BudgetGuard",
    # Spending caps
    "SpendCaps",
    "InMemorySpendLedger",
    "SqliteSpendLedger",
    # Validation-gated workflows
    "WorkOrder",
    "DeliveryEvidence",
    "ValidationVerdict",
    "SignedValidationVerdict",
    "Verifier",
    "hash_content",
    "sign_verdict",
    "verify_signed_verdict",
    # Models
    "Chain",
    "Wallet",
    "Service",
    "Payment",
    "Budget",
    "PaymentRequest",
    # Exceptions
    "ArcAgentPayError",
    "InsufficientFundsError",
    "PaymentFailedError",
    "PaymentTimeoutError",
    "InvalidPaymentRequestError",
    "ServiceNotFoundError",
    "ServiceRegistrationError",
    "WorkflowError",
    "InvalidVerdictError",
]
