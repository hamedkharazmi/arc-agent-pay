"""
Tests for SemanticServiceRegistry.

A deterministic stub embedder is injected so the vector search is reproducible
and runs offline (no model download). The chromadb-backed tests skip cleanly if
the [rag] extra isn't installed; the fallback test always runs.
"""

from __future__ import annotations

import pytest

from arc_agent_pay import SemanticServiceRegistry, ServiceRegistry

# Category dimensions for the deterministic embedder. Each builtin service name
# contains exactly one of these words, so queries map to a clear nearest match.
_CATS = ["price", "news", "whale", "research"]


def _stub_embedder(texts):
    return [[float(t.lower().count(c)) for c in _CATS] for t in texts]


def _registry():
    return ServiceRegistry(include_builtins=True)


# ---------------------------------------------------------------------------
# Graceful fallback (no chroma needed)
# ---------------------------------------------------------------------------

def test_falls_back_to_keyword_when_indexing_fails():
    def boom(_texts):
        raise RuntimeError("embedder unavailable")

    reg = _registry()
    sem = SemanticServiceRegistry(reg, embedder=boom)

    assert sem.available is False
    # Delegates to keyword search → identical results to the wrapped registry.
    assert sem.search("crypto prices", max_results=2) == reg.search(
        "crypto prices", max_results=2
    )


def test_get_delegates_to_wrapped_registry():
    reg = _registry()
    sem = SemanticServiceRegistry(reg, embedder=lambda t: [[0.0] for _ in t])
    assert sem.get("Crypto Price Feed").url == reg.get("Crypto Price Feed").url


# ---------------------------------------------------------------------------
# Vector search (requires chromadb)
# ---------------------------------------------------------------------------

@pytest.fixture
def chroma():
    return pytest.importorskip("chromadb")


def test_semantic_search_returns_nearest_service(chroma):
    sem = SemanticServiceRegistry(_registry(), embedder=_stub_embedder)
    assert sem.available is True

    whale = sem.search("large whale transactions", max_results=1)
    assert whale and whale[0].name == "Token Whale Tracker"

    news = sem.search("latest news headlines", max_results=1)
    assert news and news[0].name == "News Headlines"


def test_semantic_search_respects_max_results(chroma):
    sem = SemanticServiceRegistry(_registry(), embedder=_stub_embedder)
    results = sem.search("price data feed", max_results=2)
    assert len(results) <= 2


def test_register_reindexes(chroma):
    from arc_agent_pay.models import Service

    sem = SemanticServiceRegistry(_registry(), embedder=_stub_embedder)
    sem.register(
        Service(
            name="Weather Oracle",
            url="https://example.com/weather",
            description="research weather research forecasts research",
            tags=["research"],
        )
    )
    # New service is discoverable after the implicit reindex.
    assert sem.get("Weather Oracle").url == "https://example.com/weather"
