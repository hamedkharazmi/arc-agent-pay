"""
registry/base.py — the discovery interface.

`Discovery` is the minimal contract the agent depends on for finding services.
Both the keyword `ServiceRegistry` and the embedding-based
`SemanticServiceRegistry` satisfy it, so they are interchangeable wherever a
discovery backend is accepted (e.g. `ResearchAgent(registry=...)`).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Service


@runtime_checkable
class Discovery(Protocol):
    """Anything that can find services for a free-text query."""

    def search(self, query: str, max_results: int = 5) -> list[Service]:
        """Return services ranked by relevance to `query`."""
        ...

    def get(self, name: str) -> Service:
        """Return a service by exact name (raises ServiceNotFoundError)."""
        ...
