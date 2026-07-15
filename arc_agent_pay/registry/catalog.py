"""External service catalog: fetch + cache (P1-5).

Lets the registry sync services from an external HTTP catalog and cache them
locally with a TTL, so discovery isn't limited to the static builtins. The
builtins stay as a fallback for when the catalog is unavailable or empty.

Resolution order in `load_catalog`:
  1. fresh local cache (within TTL) — no network
  2. live fetch from the catalog URL → refresh the cache
  3. stale cache (any age) when the fetch fails or no URL is set
  4. empty list (callers keep their builtins as fallback)

Catalog format (either shape accepted):
    {"services": [ {service...}, ... ]}   or   [ {service...}, ... ]
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from ..models import Service

DEFAULT_CACHE_TTL_SECONDS = 3600.0


def catalog_url() -> str:
    """The configured external catalog URL (empty string when unset)."""
    return os.environ.get("ARC_REGISTRY_CATALOG_URL", "").strip()


def default_cache_path() -> Path:
    """Where the fetched catalog is cached on disk (XDG-aware, overridable)."""
    override = os.environ.get("ARC_REGISTRY_CACHE_PATH", "").strip()
    if override:
        return Path(override)
    root = os.environ.get("XDG_CACHE_HOME", "").strip() or str(Path.home() / ".cache")
    return Path(root) / "arc-agent-pay" / "service-catalog.json"


def _parse_services(items: Any) -> list[Service]:
    """Coerce a catalog payload into Service objects (never marked builtin)."""
    if isinstance(items, dict):
        items = items.get("services", [])
    if not isinstance(items, list):
        raise ValueError("catalog must be a list or an object with a 'services' list.")
    services: list[Service] = []
    for item in items:
        svc = Service(**item)
        svc.is_builtin = False  # external entries are never builtins
        services.append(svc)
    return services


def fetch_catalog(url: str, *, timeout: float = 10.0) -> list[Service]:
    """Fetch + parse the external catalog. Raises on network/parse failure."""
    import httpx

    resp = httpx.get(url, timeout=timeout)
    resp.raise_for_status()
    return _parse_services(resp.json())


def read_cache(path: Path, *, ttl: Optional[float]) -> Optional[list[Service]]:
    """Read cached services. `ttl=None` ignores expiry (stale reads allowed);
    a numeric ttl returns None when the cache is older than it. None on any error."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if ttl is not None:
            fetched_at = float(raw.get("fetched_at", 0))
            if (time.time() - fetched_at) > ttl:
                return None
        return _parse_services(raw.get("services", []))
    except Exception:
        return None


def write_cache(path: Path, services: list[Service]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": time.time(),
        "services": [s.model_dump() for s in services],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def load_catalog(
    *,
    url: Optional[str] = None,
    cache_path: Optional[Path] = None,
    ttl: float = DEFAULT_CACHE_TTL_SECONDS,
    force: bool = False,
) -> list[Service]:
    """Return external catalog services, preferring a fresh cache, then a live
    fetch, then a stale cache. Returns [] when nothing is available. Never raises
    — discovery must keep working (on builtins) even if the catalog is down.
    """
    url = catalog_url() if url is None else url.strip()
    cache_path = cache_path or default_cache_path()

    if not force:
        fresh = read_cache(cache_path, ttl=ttl)
        if fresh is not None:
            return fresh

    if not url:
        # No source configured: last-resort stale cache, else nothing.
        return read_cache(cache_path, ttl=None) or []

    try:
        services = fetch_catalog(url)
    except Exception:
        # Network/parse failure: degrade to any cached copy, however old.
        return read_cache(cache_path, ttl=None) or []

    write_cache(cache_path, services)
    return services
