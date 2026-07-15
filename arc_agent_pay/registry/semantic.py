"""
registry/semantic.py — embedding-based (RAG-style) service discovery.

`SemanticServiceRegistry` wraps a keyword `ServiceRegistry` and replaces its
ranking with vector similarity: each service (name + description + tags) is
embedded into a Chroma collection, and `search()` embeds the query and returns
the nearest services. Storage, persistence, and `get()` are delegated to the
wrapped registry — only ranking changes.

Graceful degradation: if the optional deps (chromadb / fastembed) are missing or
embedding fails for any reason, every call transparently falls back to the
wrapped registry's keyword search. So importing/using this class never hard-fails
just because the `[rag]` extra isn't installed.

Install the extra:  pip install "arc-agent-pay[rag]"
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Optional

from ..models import Service

if TYPE_CHECKING:  # avoid a runtime import cycle with the package __init__
    from . import ServiceRegistry

logger = logging.getLogger(__name__)

Embedder = Callable[[list[str]], list[list[float]]]


def _default_embedder() -> Embedder:
    """Build a fastembed-backed embedder (downloads a small ONNX model once)."""
    from fastembed import TextEmbedding

    model = TextEmbedding()

    def embed(texts: list[str]) -> list[list[float]]:
        # Convert numpy float32 → Python float (Chroma rejects np scalar lists).
        return [[float(x) for x in vec] for vec in model.embed(texts)]

    return embed


def _service_text(service: Service) -> str:
    tags = ", ".join(service.tags)
    return f"{service.name}. {service.description}. tags: {tags}"


class SemanticServiceRegistry:
    """A Discovery backend that ranks services by embedding similarity."""

    def __init__(
        self,
        registry: "ServiceRegistry",
        *,
        embedder: Optional[Embedder] = None,
        collection_name: str = "arc_agent_pay_services",
    ) -> None:
        self._registry = registry
        self._embedder = embedder
        self._collection_name = collection_name
        self._collection = None
        self._embed: Optional[Embedder] = None
        self.available = False
        try:
            self._build_index()
            self.available = True
        except Exception as e:  # noqa: BLE001 - any failure → keyword fallback
            logger.warning("Semantic discovery unavailable (%s); using keyword search", e)

    # ------------------------------------------------------------------
    # Index construction
    # ------------------------------------------------------------------

    def _build_index(self) -> None:
        import chromadb

        self._embed = self._embedder or _default_embedder()

        client = chromadb.EphemeralClient()
        # Fresh collection each build so a re-index reflects current services.
        try:
            client.delete_collection(self._collection_name)
        except Exception:
            pass
        collection = client.create_collection(self._collection_name)

        services = self._registry.list_all()
        if services:
            collection.add(
                ids=[s.name for s in services],
                embeddings=self._embed([_service_text(s) for s in services]),
                metadatas=[{"name": s.name} for s in services],
            )
        self._collection = collection

    def reindex(self) -> None:
        """Rebuild the vector index (call after registering new services)."""
        if not self.available:
            return
        try:
            self._build_index()
        except Exception as e:  # noqa: BLE001
            logger.warning("Re-index failed (%s); disabling semantic search", e)
            self.available = False

    # ------------------------------------------------------------------
    # Discovery interface
    # ------------------------------------------------------------------

    def search(self, query: str, max_results: int = 5) -> list[Service]:
        if not self.available or self._collection is None or self._embed is None:
            return self._registry.search(query, max_results=max_results)
        try:
            result = self._collection.query(
                query_embeddings=self._embed([query]),
                n_results=max_results,
            )
            ids = result.get("ids", [[]])[0]
            services = []
            for name in ids:
                try:
                    services.append(self._registry.get(name))
                except Exception:  # noqa: BLE001 - skip stale ids
                    continue
            return services or self._registry.search(query, max_results=max_results)
        except Exception as e:  # noqa: BLE001 - fall back on any query failure
            logger.warning("Semantic query failed (%s); using keyword search", e)
            return self._registry.search(query, max_results=max_results)

    # Delegate the rest to the wrapped registry so this is a drop-in.
    def get(self, name: str) -> Service:
        return self._registry.get(name)

    def register(self, service: Service) -> None:
        self._registry.register(service)
        self.reindex()

    def list_all(self, include_builtins: bool = True) -> list[Service]:
        return self._registry.list_all(include_builtins)
