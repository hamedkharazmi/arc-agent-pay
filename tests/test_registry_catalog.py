"""Tests for the external service catalog: fetch + cache + fallback (P1-5)."""

from __future__ import annotations

import json
import time

import pytest

from arc_agent_pay.registry import ServiceRegistry, catalog


def _svc(name: str, tags: list[str], url: str | None = None) -> dict:
    return {
        "name": name,
        "url": url or f"https://catalog.example.com/{name.lower().replace(' ', '-')}",
        "description": f"{name} description",
        "price_usdc": "0.003",
        "tags": tags,
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# Parsing + cache primitives
# ---------------------------------------------------------------------------

class TestParsing:
    def test_accepts_bare_list_and_object_shapes(self):
        items = [_svc("Alpha", ["x"]), _svc("Beta", ["y"])]
        assert len(catalog._parse_services(items)) == 2
        assert len(catalog._parse_services({"services": items})) == 2

    def test_external_entries_are_never_builtins(self):
        svcs = catalog._parse_services([{**_svc("Alpha", ["x"]), "is_builtin": True}])
        assert svcs[0].is_builtin is False

    def test_bad_shape_raises(self):
        with pytest.raises(ValueError):
            catalog._parse_services(42)


class TestCache:
    def test_write_then_read_roundtrip(self, tmp_path):
        path = tmp_path / "cat.json"
        catalog.write_cache(path, catalog._parse_services([_svc("Alpha", ["x"])]))
        loaded = catalog.read_cache(path, ttl=3600)
        assert loaded is not None and loaded[0].name == "Alpha"

    def test_expired_cache_returns_none_but_stale_read_allows(self, tmp_path):
        path = tmp_path / "cat.json"
        catalog.write_cache(path, catalog._parse_services([_svc("Alpha", ["x"])]))
        # Backdate fetched_at so it's older than the ttl.
        raw = json.loads(path.read_text())
        raw["fetched_at"] = time.time() - 10_000
        path.write_text(json.dumps(raw))

        assert catalog.read_cache(path, ttl=3600) is None  # expired
        assert catalog.read_cache(path, ttl=None) is not None  # stale read OK

    def test_missing_cache_returns_none(self, tmp_path):
        assert catalog.read_cache(tmp_path / "nope.json", ttl=None) is None


# ---------------------------------------------------------------------------
# load_catalog resolution order
# ---------------------------------------------------------------------------

class TestLoadCatalog:
    def test_no_url_no_cache_returns_empty(self, tmp_path):
        assert catalog.load_catalog(url="", cache_path=tmp_path / "c.json") == []

    def test_fresh_cache_skips_fetch(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"
        catalog.write_cache(path, catalog._parse_services([_svc("Cached", ["x"])]))

        def _boom(*a, **k):
            raise AssertionError("should not fetch when cache is fresh")

        monkeypatch.setattr(catalog, "fetch_catalog", _boom)
        out = catalog.load_catalog(url="https://x", cache_path=path, ttl=3600)
        assert [s.name for s in out] == ["Cached"]

    def test_live_fetch_writes_cache(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"
        monkeypatch.setattr(
            catalog, "fetch_catalog",
            lambda url, **k: catalog._parse_services([_svc("Live", ["x"])]),
        )
        out = catalog.load_catalog(url="https://x", cache_path=path, ttl=3600, force=True)
        assert [s.name for s in out] == ["Live"]
        assert path.exists()  # cache refreshed

    def test_fetch_failure_falls_back_to_stale_cache(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"
        catalog.write_cache(path, catalog._parse_services([_svc("Stale", ["x"])]))
        raw = json.loads(path.read_text())
        raw["fetched_at"] = time.time() - 10_000  # expired
        path.write_text(json.dumps(raw))

        def _boom(*a, **k):
            raise RuntimeError("catalog down")

        monkeypatch.setattr(catalog, "fetch_catalog", _boom)
        out = catalog.load_catalog(url="https://x", cache_path=path, ttl=3600)
        assert [s.name for s in out] == ["Stale"]  # degraded to stale cache

    def test_fetch_catalog_uses_httpx(self, monkeypatch):
        import httpx

        monkeypatch.setattr(
            httpx, "get", lambda url, **k: _FakeResponse({"services": [_svc("Net", ["x"])]})
        )
        out = catalog.fetch_catalog("https://x")
        assert out[0].name == "Net"


# ---------------------------------------------------------------------------
# ServiceRegistry integration
# ---------------------------------------------------------------------------

class TestRegistrySync:
    def test_sync_merges_external_and_flags_external(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"
        monkeypatch.setattr(
            catalog, "fetch_catalog",
            lambda url, **k: catalog._parse_services(
                [_svc("External Oracle", ["oracle", "data"])]
            ),
        )
        reg = ServiceRegistry(include_builtins=True, catalog_url="https://x",
                              catalog_cache_path=path)
        # Auto-synced in __init__ since a URL was given.
        assert reg._has_external is True
        assert any(s.name == "External Oracle" for s in reg.list_all())

    def test_builtins_become_fallback_when_external_present(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"
        # External catalog also covers "prices", competing with the builtin feed.
        monkeypatch.setattr(
            catalog, "fetch_catalog",
            lambda url, **k: catalog._parse_services(
                [_svc("Pro Price Oracle", ["crypto", "prices"])]
            ),
        )
        reg = ServiceRegistry(include_builtins=True, catalog_url="https://x",
                              catalog_cache_path=path)
        results = reg.search("prices")
        assert results, "expected a match"
        # Builtins are demoted: the external match wins and no builtin is returned.
        assert all(not s.is_builtin for s in results)
        assert results[0].name == "Pro Price Oracle"

    def test_builtin_used_when_no_external_match(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"
        monkeypatch.setattr(
            catalog, "fetch_catalog",
            lambda url, **k: catalog._parse_services([_svc("Weather Feed", ["weather"])]),
        )
        reg = ServiceRegistry(include_builtins=True, catalog_url="https://x",
                              catalog_cache_path=path)
        # Nothing external matches "whale", so the builtin tracker is the fallback.
        results = reg.search("whale")
        assert any(s.is_builtin and "Whale" in s.name for s in results)

    def test_no_autosync_without_url(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ARC_REGISTRY_CATALOG_URL", raising=False)

        def _boom(*a, **k):
            raise AssertionError("must not fetch without a configured URL")

        monkeypatch.setattr(catalog, "load_catalog", _boom)
        reg = ServiceRegistry(include_builtins=True)
        assert reg._has_external is False

    def test_sync_is_best_effort_on_failure(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"

        def _boom(*a, **k):
            raise RuntimeError("down")

        monkeypatch.setattr(catalog, "fetch_catalog", _boom)
        # No cache + failing fetch → no external, builtins remain, no exception.
        reg = ServiceRegistry(include_builtins=True, catalog_url="https://x",
                              catalog_cache_path=path)
        assert reg._has_external is False
        assert reg.count == len(reg.list_all())
        assert reg.search("price")  # builtins still discoverable

    def test_env_url_triggers_autosync(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"
        monkeypatch.setenv("ARC_REGISTRY_CATALOG_URL", "https://env-catalog")
        monkeypatch.setenv("ARC_REGISTRY_CACHE_PATH", str(path))
        monkeypatch.setattr(
            catalog, "fetch_catalog",
            lambda url, **k: catalog._parse_services([_svc("Env Service", ["env"])]),
        )
        reg = ServiceRegistry(include_builtins=True)
        assert reg._has_external is True
        assert any(s.name == "Env Service" for s in reg.list_all())
