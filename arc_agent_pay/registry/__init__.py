"""
registry.py — Layer 3 of arc-agent-pay: the Service Registry.

ServiceRegistry is the discovery layer. It answers the question:
"Give me an x402-compatible API that does X."

Two sources of truth:
  1. LOCAL registry  — services you register in code or via a JSON file.
                       Always available. Used for mock services in tests
                       and for any real services you've manually added.
  2. Circle Agent Marketplace — agents.circle.com/services.
                       Currently a web-based catalog with no public REST API.
                       We link to it and provide a CLI helper to open it.
                       When Circle ships a search API we'll add it here.

The local registry is the foundation for the demo agent: we register
three realistic mock x402 services (price data, research, news) so the
demo works end-to-end without depending on external availability.

Usage:
    registry = ServiceRegistry()

    # Register a local service (e.g. your mock FastAPI server)
    registry.register(Service(
        name="Crypto Prices",
        url="https://your-x402-server.example.com/prices",
        description="Real-time crypto price data",
        price_usdc="0.001",
        tags=["crypto", "prices", "data"],
    ))

    # Search across all registered services
    results = registry.search("crypto price")
    service = results[0]

    # Or get by exact name
    service = registry.get("Crypto Prices")
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from ..exceptions import ServiceNotFoundError, ServiceRegistrationError
from ..models import Chain, Service
from . import catalog


# ---------------------------------------------------------------------------
# Built-in paid services
# These ship with the SDK and power the agent out of the box.
#
# The host defaults to the public hosted playground API but is overridable via
# ARC_SERVICES_BASE_URL so development can run against any compatible x402 server
# (e.g. ARC_SERVICES_BASE_URL=http://127.0.0.1:8402).
# ---------------------------------------------------------------------------

_DEFAULT_SERVICES_BASE_URL = "https://api.agentpay.bond"
_SERVICES_BASE_URL = os.environ.get(
    "ARC_SERVICES_BASE_URL", _DEFAULT_SERVICES_BASE_URL
).strip().rstrip("/")

_BUILTIN_MOCKS: list[Service] = [
    Service(
        name="Crypto Price Feed",
        url=f"{_SERVICES_BASE_URL}/prices",
        description=(
            "Real-time cryptocurrency prices (BTC, ETH, SOL, USDC). "
            "Returns JSON with price, 24h change, and market cap."
        ),
        price_usdc="0.001",
        tags=["crypto", "prices", "market-data", "finance"],
        chain=Chain.ARC_TESTNET,
        is_builtin=True,
    ),
    Service(
        name="Web Research API",
        url=f"{_SERVICES_BASE_URL}/research",
        description=(
            "Summarizes a topic using web sources. "
            "POST {topic: str} → returns a structured research brief."
        ),
        price_usdc="0.005",
        tags=["research", "web", "summarization", "ai"],
        chain=Chain.ARC_TESTNET,
        is_builtin=True,
    ),
    Service(
        name="News Headlines",
        url=f"{_SERVICES_BASE_URL}/news",
        description=(
            "Top headlines for a given category (tech, finance, world). "
            "Returns last 10 headlines with source and timestamp."
        ),
        price_usdc="0.002",
        tags=["news", "headlines", "media", "finance", "tech"],
        chain=Chain.ARC_TESTNET,
        is_builtin=True,
    ),
    Service(
        name="Token Whale Tracker",
        url=f"{_SERVICES_BASE_URL}/whales",
        description=(
            "Tracks large wallet movements for a given token address. "
            "Returns top 10 whale transactions in the last 24h."
        ),
        price_usdc="0.010",
        tags=["crypto", "whales", "on-chain", "analytics", "defi"],
        chain=Chain.ARC_TESTNET,
        is_builtin=True,
    ),
]


# ---------------------------------------------------------------------------
# ServiceRegistry
# ---------------------------------------------------------------------------

class ServiceRegistry:
    """
    Discovery layer for x402-compatible services.

    Maintains a thread-safe in-memory store of Service objects, searchable
    by name, description, and tags. Optionally persists to / loads from
    a JSON file so services survive between sessions.

    Args:
        registry_path: Optional path to a JSON file for persistence.
                       If provided and the file exists, services are loaded
                       on startup. Pass None for ephemeral in-memory only.
        include_builtins: Whether to pre-populate with built-in services.
                          Default True — gives the demo agent something to work
                          with immediately without any setup.
    """

    # Circle Marketplace — for humans to browse when no local result found
    MARKETPLACE_URL = "https://agents.circle.com/services"

    def __init__(
        self,
        registry_path: Optional[Path] = None,
        include_builtins: bool = True,
        *,
        catalog_url: Optional[str] = None,
        catalog_cache_path: Optional[Path] = None,
        catalog_ttl: float = catalog.DEFAULT_CACHE_TTL_SECONDS,
        sync_catalog: Optional[bool] = None,
    ) -> None:
        self._lock = threading.Lock()
        self._services: dict[str, Service] = {}   # name → Service
        self._registry_path = registry_path

        # External catalog config (P1-5). When a catalog is synced, builtins are
        # demoted to a fallback (used only when nothing external/manual matches).
        self._catalog_url = catalog_url
        self._catalog_cache_path = catalog_cache_path
        self._catalog_ttl = catalog_ttl
        self._has_external = False

        if include_builtins:
            for svc in _BUILTIN_MOCKS:
                self._services[svc.name] = svc

        if registry_path and Path(registry_path).exists():
            self._load_from_file(Path(registry_path))

        # Auto-sync when explicitly requested, or by default when a catalog URL is
        # configured (env or arg). Best-effort: never fail construction on a down
        # catalog — discovery still works on builtins / cache.
        resolved_url = catalog_url if catalog_url is not None else catalog.catalog_url()
        should_sync = sync_catalog if sync_catalog is not None else bool(resolved_url)
        if should_sync:
            self.sync_catalog()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, service: Service) -> None:
        """
        Add or update a service in the local registry.
        Overwrites silently if a service with the same name already exists.

        Raises ServiceRegistrationError if the service URL is empty.
        """
        if not service.url:
            raise ServiceRegistrationError(
                f"Service '{service.name}' has no URL."
            )
        with self._lock:
            self._services[service.name] = service
            if self._registry_path:
                self._save_to_file(Path(self._registry_path))

    def unregister(self, name: str) -> None:
        """Remove a service by name. Silent no-op if not found."""
        with self._lock:
            self._services.pop(name, None)
            if self._registry_path:
                self._save_to_file(Path(self._registry_path))

    # ------------------------------------------------------------------
    # External catalog sync (P1-5)
    # ------------------------------------------------------------------

    def sync_catalog(self, *, force: bool = False) -> int:
        """Pull services from the external catalog (cache-first) and merge them.

        Returns the number of external services loaded. Best-effort and never
        raises: on a missing URL or a failed fetch with no cache it loads nothing
        and leaves the builtins in place. Once at least one external service is
        present, builtins become a search fallback (see `search`).
        """
        services = catalog.load_catalog(
            url=self._catalog_url,
            cache_path=self._catalog_cache_path,
            ttl=self._catalog_ttl,
            force=force,
        )
        if not services:
            return 0
        with self._lock:
            for svc in services:
                if svc.url:
                    self._services[svc.name] = svc
            self._has_external = True
        return len(services)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        tags: Optional[list[str]] = None,
        max_results: int = 5,
        include_builtins: bool = True,
    ) -> list[Service]:
        """
        Search the local registry for services matching `query`.

        Scoring (higher = better match):
          +3  query word found in service name
          +2  query word found in a tag
          +1  query word found in description

        Args:
            query:            Free-text search query, e.g. "crypto price data"
            tags:             Optional list of tags to filter by (AND logic)
            max_results:      Maximum number of results to return
            include_builtins: Whether to include built-in SDK services in results

        Returns:
            List of Service objects sorted by relevance, best first.
            Empty list if no matches found.
        """
        query_words = query.lower().split()

        with self._lock:
            candidates = list(self._services.values())

        if not include_builtins:
            candidates = [s for s in candidates if not s.is_builtin]

        # Tag filter (AND — all requested tags must be present)
        if tags:
            tags_lower = [t.lower() for t in tags]
            candidates = [
                s for s in candidates
                if all(t in [st.lower() for st in s.tags] for t in tags_lower)
            ]

        # Score each candidate
        scored: list[tuple[int, Service]] = []
        for svc in candidates:
            score = 0
            name_lower = svc.name.lower()
            desc_lower = svc.description.lower()
            tags_lower_set = {t.lower() for t in svc.tags}

            for word in query_words:
                if word in name_lower:
                    score += 3
                for tag in tags_lower_set:
                    if word in tag:
                        score += 2
                        break
                if word in desc_lower:
                    score += 1

            if score > 0:
                scored.append((score, svc))

        # Sort by score descending, then name ascending for stability
        scored.sort(key=lambda x: (-x[0], x[1].name))

        # When an external catalog is active, builtins are fallback only: prefer
        # external/manual matches, and fall back to builtins only if none matched.
        if self._has_external and include_builtins:
            external = [(s, svc) for s, svc in scored if not svc.is_builtin]
            if external:
                scored = external

        return [svc for _, svc in scored[:max_results]]

    def get(self, name: str) -> Service:
        """
        Retrieve a service by exact name.
        Raises ServiceNotFoundError if not found.
        """
        with self._lock:
            svc = self._services.get(name)
        if svc is None:
            raise ServiceNotFoundError(
                f"No service named '{name}' in the local registry.\n"
                f"Browse the Circle Agent Marketplace at: {self.MARKETPLACE_URL}"
            )
        return svc

    def list_all(self, include_builtins: bool = True) -> list[Service]:
        """Return all registered services, sorted by name."""
        with self._lock:
            services = list(self._services.values())
        if not include_builtins:
            services = [s for s in services if not s.is_builtin]
        return sorted(services, key=lambda s: s.name)

    def list_by_tag(self, tag: str) -> list[Service]:
        """Return all services that include `tag` (case-insensitive)."""
        tag_lower = tag.lower()
        with self._lock:
            return [
                s for s in self._services.values()
                if any(t.lower() == tag_lower for t in s.tags)
            ]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._services)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_to_file(self, path: Path) -> None:
        """Serialize all services to JSON. Called automatically on mutation."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [svc.model_dump() for svc in self._services.values()]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load_from_file(self, path: Path) -> None:
        """Load services from a JSON file, merging with existing entries."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data:
                svc = Service(**item)
                self._services[svc.name] = svc
        except Exception as e:
            raise ServiceRegistrationError(
                f"Failed to load registry from {path}: {e}"
            ) from e

    def save(self, path: Optional[Path] = None) -> None:
        """
        Manually save the registry to a file.
        Uses self._registry_path if no path is provided.
        """
        target = path or self._registry_path
        if target is None:
            raise ServiceRegistrationError(
                "No path provided and no registry_path set on this instance."
            )
        with self._lock:
            self._save_to_file(Path(target))

    # ------------------------------------------------------------------
    # Marketplace reference (for when local search comes up empty)
    # ------------------------------------------------------------------

    def marketplace_url(self, query: str = "") -> str:
        """
        Return the Circle Agent Marketplace URL.
        When Circle ships a search API, this will return a deep-link.

        Note: The Marketplace is currently a web UI at agents.circle.com/services.
        There is no public REST search API yet. When one ships, this method
        will be updated to call it and return live results.
        """
        base = self.MARKETPLACE_URL
        if query:
            # Best-effort URL with query string for when the marketplace
            # adds search parameters
            encoded = query.replace(" ", "+")
            return f"{base}?q={encoded}"
        return base

    def suggest_marketplace(self, query: str) -> str:
        """
        Return a human-readable suggestion to browse the Marketplace.
        Used when local search returns no results.
        """
        return (
            f"No local services matched '{query}'.\n"
            f"Browse the Circle Agent Marketplace for x402 services:\n"
            f"  {self.marketplace_url(query)}\n"
            f"Once you find a service, register it locally with registry.register(Service(...))."
        )


# Imported at the end so ServiceRegistry is defined first (avoids import cycle).
from .base import Discovery  # noqa: E402
from .semantic import SemanticServiceRegistry  # noqa: E402

__all__ = ["ServiceRegistry", "Discovery", "SemanticServiceRegistry"]