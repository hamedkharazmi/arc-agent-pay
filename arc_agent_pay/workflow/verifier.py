"""Provider-neutral validation interface."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import DeliveryEvidence, SignedValidationVerdict, WorkOrder


@runtime_checkable
class Verifier(Protocol):
    """An independent service that evaluates delivered work and signs a verdict."""

    name: str

    async def verify(
        self,
        order: WorkOrder,
        delivery: DeliveryEvidence,
    ) -> SignedValidationVerdict:
        """Evaluate one delivery and return a signed, evidence-bound verdict."""
        ...
