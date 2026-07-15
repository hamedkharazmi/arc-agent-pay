"""
Tests for registry.py.
All tests are pure in-memory — no network, no Circle CLI.
"""

from __future__ import annotations

import json
import pytest

from arc_agent_pay.registry import ServiceRegistry
from arc_agent_pay.models import Service
from arc_agent_pay.exceptions import ServiceNotFoundError, ServiceRegistrationError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_service(
    name: str = "Test API",
    url: str = "http://localhost:8402/test",
    description: str = "A test service",
    price_usdc: str = "0.001",
    tags: list[str] | None = None,
    is_builtin: bool = False,
) -> Service:
    return Service(
        name=name,
        url=url,
        description=description,
        price_usdc=price_usdc,
        tags=tags or ["test"],
        is_builtin=is_builtin,
    )


@pytest.fixture
def empty_registry() -> ServiceRegistry:
    """Registry with no mock services pre-loaded."""
    return ServiceRegistry(include_builtins=False)


@pytest.fixture
def populated_registry() -> ServiceRegistry:
    """Registry with three manually registered services."""
    reg = ServiceRegistry(include_builtins=False)
    reg.register(make_service(
        name="Crypto Price Feed",
        url="http://localhost:8402/prices",
        description="Real-time cryptocurrency prices",
        tags=["crypto", "prices", "market-data"],
        price_usdc="0.001",
    ))
    reg.register(make_service(
        name="Web Research API",
        url="http://localhost:8402/research",
        description="Summarizes a topic using web sources",
        tags=["research", "summarization", "ai"],
        price_usdc="0.005",
    ))
    reg.register(make_service(
        name="News Headlines",
        url="http://localhost:8402/news",
        description="Top news headlines by category",
        tags=["news", "media", "headlines"],
        price_usdc="0.002",
    ))
    return reg


# ---------------------------------------------------------------------------
# Built-in mocks
# ---------------------------------------------------------------------------

class TestBuiltinMocks:

    def test_mocks_loaded_by_default(self):
        reg = ServiceRegistry(include_builtins=True)
        assert reg.count >= 3

    def test_no_mocks_when_disabled(self):
        reg = ServiceRegistry(include_builtins=False)
        assert reg.count == 0

    def test_mock_services_are_flagged(self):
        reg = ServiceRegistry(include_builtins=True)
        all_services = reg.list_all()
        mock_services = [s for s in all_services if s.is_builtin]
        assert len(mock_services) >= 3


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:

    def test_register_adds_service(self, empty_registry):
        svc = make_service(name="My API")
        empty_registry.register(svc)
        assert empty_registry.count == 1

    def test_register_overwrites_same_name(self, empty_registry):
        empty_registry.register(make_service(name="API", price_usdc="0.001"))
        empty_registry.register(make_service(name="API", price_usdc="0.999"))
        assert empty_registry.count == 1
        assert empty_registry.get("API").price_usdc == "0.999"

    def test_register_empty_url_raises(self, empty_registry):
        with pytest.raises(ServiceRegistrationError):
            empty_registry.register(Service(
                name="Bad", url="", description="no url", price_usdc="0.001"
            ))

    def test_unregister_removes_service(self, empty_registry):
        empty_registry.register(make_service(name="To Remove"))
        empty_registry.unregister("To Remove")
        assert empty_registry.count == 0

    def test_unregister_nonexistent_is_silent(self, empty_registry):
        empty_registry.unregister("does-not-exist")  # should not raise


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------

class TestGet:

    def test_get_existing_service(self, populated_registry):
        svc = populated_registry.get("Crypto Price Feed")
        assert svc.url == "http://localhost:8402/prices"

    def test_get_nonexistent_raises(self, populated_registry):
        with pytest.raises(ServiceNotFoundError, match="Crypto Wizard"):
            populated_registry.get("Crypto Wizard")

    def test_error_message_contains_marketplace_url(self, empty_registry):
        with pytest.raises(ServiceNotFoundError) as exc_info:
            empty_registry.get("anything")
        assert "agents.circle.com" in str(exc_info.value)


# ---------------------------------------------------------------------------
# search() — relevance scoring
# ---------------------------------------------------------------------------

class TestSearch:

    def test_search_finds_by_name(self, populated_registry):
        results = populated_registry.search("crypto")
        assert len(results) >= 1
        assert results[0].name == "Crypto Price Feed"

    def test_search_finds_by_tag(self, populated_registry):
        results = populated_registry.search("research")
        names = [r.name for r in results]
        assert "Web Research API" in names

    def test_search_finds_by_description(self, populated_registry):
        results = populated_registry.search("headlines")
        names = [r.name for r in results]
        assert "News Headlines" in names

    def test_search_empty_query_returns_empty(self, populated_registry):
        # Empty query has no words to score against
        results = populated_registry.search("")
        assert results == []

    def test_search_no_match_returns_empty(self, populated_registry):
        results = populated_registry.search("blockchain quantum fusion")
        assert results == []

    def test_search_respects_max_results(self, populated_registry):
        # Add a fourth service that also matches "api"
        populated_registry.register(make_service(
            name="Extra API",
            tags=["api"],
            description="api endpoint",
        ))
        results = populated_registry.search("api", max_results=2)
        assert len(results) <= 2

    def test_name_match_scores_higher_than_description(self, empty_registry):
        """A word in the name should rank above the same word only in description."""
        empty_registry.register(make_service(
            name="Price Oracle",
            description="gives you data",
            tags=["oracle"],
        ))
        empty_registry.register(make_service(
            name="Data Feed",
            description="oracle-style price stream",
            tags=["data"],
        ))
        results = empty_registry.search("oracle")
        assert results[0].name == "Price Oracle"

    def test_search_tag_filter(self, populated_registry):
        results = populated_registry.search("api", tags=["crypto"])
        for svc in results:
            assert "crypto" in [t.lower() for t in svc.tags]

    def test_search_excludes_mocks_when_requested(self):
        reg = ServiceRegistry(include_builtins=True)
        results = reg.search("price", include_builtins=False)
        assert all(not s.is_builtin for s in results)

    def test_search_multi_word_query(self, populated_registry):
        results = populated_registry.search("crypto price market")
        assert len(results) >= 1
        assert results[0].name == "Crypto Price Feed"


# ---------------------------------------------------------------------------
# list_all() and list_by_tag()
# ---------------------------------------------------------------------------

class TestListing:

    def test_list_all_sorted_by_name(self, populated_registry):
        names = [s.name for s in populated_registry.list_all()]
        assert names == sorted(names)

    def test_list_all_count_matches_registered(self, populated_registry):
        assert populated_registry.count == len(populated_registry.list_all())

    def test_list_by_tag_returns_matching(self, populated_registry):
        results = populated_registry.list_by_tag("crypto")
        assert all("crypto" in [t.lower() for t in s.tags] for s in results)

    def test_list_by_tag_case_insensitive(self, populated_registry):
        results_lower = populated_registry.list_by_tag("crypto")
        results_upper = populated_registry.list_by_tag("CRYPTO")
        assert len(results_lower) == len(results_upper)

    def test_list_by_tag_nonexistent_returns_empty(self, populated_registry):
        assert populated_registry.list_by_tag("nonexistent-tag-xyz") == []


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:

    def test_save_and_load(self, tmp_path):
        path = tmp_path / "registry.json"
        reg = ServiceRegistry(registry_path=path, include_builtins=False)
        reg.register(make_service(name="Persisted API", url="http://localhost/api"))
        reg.save()

        assert path.exists()
        data = json.loads(path.read_text())
        assert any(s["name"] == "Persisted API" for s in data)

    def test_load_on_init(self, tmp_path):
        path = tmp_path / "registry.json"

        # Save first
        reg1 = ServiceRegistry(registry_path=path, include_builtins=False)
        reg1.register(make_service(name="Loaded API", url="http://localhost/loaded"))
        reg1.save()

        # Load on new instance
        reg2 = ServiceRegistry(registry_path=path, include_builtins=False)
        assert reg2.get("Loaded API").url == "http://localhost/loaded"

    def test_save_without_path_raises(self, empty_registry):
        with pytest.raises(ServiceRegistrationError, match="No path"):
            empty_registry.save()


# ---------------------------------------------------------------------------
# Marketplace reference
# ---------------------------------------------------------------------------

class TestMarketplace:

    def test_marketplace_url_base(self, empty_registry):
        url = empty_registry.marketplace_url()
        assert "agents.circle.com" in url

    def test_marketplace_url_with_query(self, empty_registry):
        url = empty_registry.marketplace_url("crypto prices")
        assert "agents.circle.com" in url
        assert "crypto" in url

    def test_suggest_marketplace_contains_query(self, empty_registry):
        suggestion = empty_registry.suggest_marketplace("weather data")
        assert "weather data" in suggestion
        assert "agents.circle.com" in suggestion