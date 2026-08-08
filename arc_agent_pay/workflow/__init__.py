"""Validation-gated workflow primitives.

These models and signing helpers are contract- and partner-neutral. They define
the stable boundary used by a future escrow implementation and by independent
verification adapters.
"""

from .models import (
    DeliveryEvidence,
    SignedValidationVerdict,
    ValidationVerdict,
    WorkOrder,
    hash_content,
)
from .signing import (
    VERDICT_DOMAIN_NAME,
    VERDICT_DOMAIN_VERSION,
    VERDICT_TYPES,
    recover_verdict_signer,
    sign_verdict,
    verify_signed_verdict,
    verdict_domain,
    verdict_message,
)
from .verifier import Verifier

__all__ = [
    "DeliveryEvidence",
    "SignedValidationVerdict",
    "ValidationVerdict",
    "Verifier",
    "WorkOrder",
    "hash_content",
    "VERDICT_DOMAIN_NAME",
    "VERDICT_DOMAIN_VERSION",
    "VERDICT_TYPES",
    "recover_verdict_signer",
    "sign_verdict",
    "verify_signed_verdict",
    "verdict_domain",
    "verdict_message",
]
