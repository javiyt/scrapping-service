"""End-to-end integration tests for the unified SEARCH path (ticket #129).

Exercises the real production composition as a unit, with **no network and
no external services** (SearXNG/Camofox stand-ins are in-memory fakes)::

    TestClient (real FastAPI app)
      → POST /v1/search  (real request validation)
      → SearchCache      (real SQLite-backed result cache)
      → SearchRouter     (real capability-aware routing + fallback policy)
      → SearchProvider   (structural fake / real client on a dead transport)

This is the same search stack the future MCP adapter consumes (the MCP
contract tests pin the MCP side, this file pins the shared service side).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.metrics.prometheus import MetricsCollector
from app.search import (
    SearchCache,
    SearchCacheConfig,
    SearchProviderRegistry,
    SearchRouter,
    SearchRouterConfig,
)
from app.search.models import Vertical
from tests.fakes import FakeSearchProvider

API_KEY = "test-key-for-tests"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture()
def pipeline(tmp_path):
    """Fresh in-memory search pipeline on ``app.state`` (leaves no state)."""

    class _Installer:
        def __init__(self) -> None:
            self.stores: list = []
            self.cache = None  # SearchCache | None

        def install(
            self,
            *providers: FakeSearchProvider,
            provider_order: list[str] | None = None,
            cache_config: SearchCacheConfig | None = None,
        ) -> SearchCache:
            router = SearchRouter(
                SearchProviderRegistry(list(providers)),
                SearchRouterConfig(provider_order=provider_order or []),
                metrics=MetricsCollector(),
            )
            from app.cache.sqlite_cache import SqliteCache

            store = SqliteCache(db_path=str(tmp_path / f"search-{len(self.stores)}.db"))
            store.open()
            self.stores.append(store)
            self.cache = SearchCache(
                store=store,
                router=router,
                config=cache_config or SearchCacheConfig(),
                metrics=MetricsCollector(),
            )
            app.state.search_cache = self.cache
            return self.cache

    installer = _Installer()
    app.state.settings = Settings(api_key=API_KEY)
    yield installer
    for store in installer.stores:
        try:
            store.close()
        except Exception:
            pass
    if hasattr(app.state, "search_cache"):
        delattr(app.state, "search_cache")


def test_search_cache_hit_is_served_without_provider_call(pipeline, tmp_path):
    """Scenario 1 — a valid request answered from a fresh cache entry never
    touches the provider (zero I/O after the first response)."""
    provider = FakeSearchProvider(name="searxng")
    pipeline.install(provider)
    client = TestClient(app)

    body = client.post(
        "/v1/search", json={"query": "Miami Dolphins", "cache_ttl_seconds": 60}, headers=AUTH
    )
    assert body.status_code == 200
    first = body.json()
    assert first["metadata"]["from_cache"] is False
    assert len(provider.search_calls) == 1

    body = client.post(
        "/v1/search", json={"query": "Miami Dolphins", "cache_ttl_seconds": 60}, headers=AUTH
    )
    assert body.status_code == 200
    second = body.json()
    assert second["metadata"]["from_cache"] is True
    assert len(provider.search_calls) == 1  # never called again

    # Identical semantics (whitespace/case/field order) share the same entry.
    same = client.post("/v1/search", json={"query": "  miami   DOLPHINS "}, headers=AUTH)
    assert same.status_code == 200
    assert same.json()["metadata"]["from_cache"] is True
    assert len(provider.search_calls) == 1


def test_searxng_success_end_to_end(pipeline):
    """Scenario 2 — a healthy primary provider answers the whole stack in one
    attempt (no fallback, no cache, deterministic envelope)."""
    provider = FakeSearchProvider(
        name="searxng",
        result_url="https://example.com/story",
        result_title="Dolphins story",
    )
    pipeline.install(provider)
    client = TestClient(app)

    body = client.post(
        "/v1/search",
        json={"query": "Dolphins", "verticals": ["web"]},
        headers=AUTH,
    )
    assert body.status_code == 200
    data = body.json()
    assert data["query"] == "Dolphins"
    assert data["results"][0]["title"] == "Dolphins story"
    assert data["metadata"]["providers_attempted"] == ["searxng"]
    assert data["metadata"]["fallback_occurred"] is False
    assert data["metadata"]["from_cache"] is False
    assert len(provider.search_calls) == 1


def test_searxng_failure_falls_back_to_camofox(pipeline):
    """Scenario 3 — a *transient* primary failure triggers the documented
    fallback to the next provider, which answers successfully."""
    searxng = FakeSearchProvider(name="searxng")
    camofox = FakeSearchProvider(
        name="camofox",
        result_url="https://example.com/camo",
        result_title="Camofox answer",
    )
    from app.core.errors import SearchProviderUnavailableError

    searxng.set_search_error(SearchProviderUnavailableError("primary is down"))
    pipeline.install(searxng, camofox, provider_order=["searxng", "camofox"])
    client = TestClient(app)

    body = client.post("/v1/search", json={"query": "Dolphins"}, headers=AUTH)
    assert body.status_code == 200
    data = body.json()
    assert data["results"][0]["title"] == "Camofox answer"
    assert data["metadata"]["providers_attempted"] == ["searxng", "camofox"]
    assert data["metadata"]["fallback_occurred"] is True
    assert len(data["metadata"]["warnings"]) >= 1


def test_explicit_provider_failure_never_falls_back_silently(pipeline):
    """Scenario 4 — requesting the failing provider *explicitly* fails with a
    clear error; the fallback provider is never attempted silently."""
    searxng = FakeSearchProvider(name="searxng")
    camofox = FakeSearchProvider(name="camofox")
    from app.core.errors import SearchProviderUnavailableError

    searxng.set_search_error(SearchProviderUnavailableError("primary is down"))
    pipeline.install(searxng, camofox, provider_order=["searxng", "camofox"])
    client = TestClient(app)

    body = client.post(
        "/v1/search", json={"query": "Dolphins", "provider": "searxng"}, headers=AUTH
    )
    assert body.status_code == 503
    assert body.json()["error"]["type"] == "search_provider_unavailable"
    assert len(searxng.search_calls) == 1
    assert len(camofox.search_calls) == 0  # no silent fallback


def test_reddit_and_youtube_search_route_through_camofox(pipeline):
    """Scenarios 5 + 6 — social/vertical requests that only Camofox can serve
    are routed *directly* to Camofox (no fallback, capability-matched)."""
    searxng = FakeSearchProvider(name="searxng", verticals=[Vertical.WEB])
    camofox = FakeSearchProvider(
        name="camofox",
        verticals=[Vertical.WEB, Vertical.SOCIAL, Vertical.VIDEOS],
        result_url="https://www.reddit.com/r/nfl",
        result_title="Reddit thread",
    )
    pipeline.install(searxng, camofox, provider_order=["searxng", "camofox"])
    client = TestClient(app)

    for vertical in ("social", "videos"):
        body = client.post(
            "/v1/search", json={"query": "Dolphins", "verticals": [vertical]}, headers=AUTH
        )
        assert body.status_code == 200, body.text
        data = body.json()
        assert data["metadata"]["providers_attempted"] == ["camofox"]
        assert data["metadata"]["fallback_occurred"] is False


def test_stale_if_error_serves_expired_entry_when_all_providers_down(pipeline):
    """Scenario 7 — when the live run fails retryably, an (expired) cached
    entry is served with ``stale`` metadata and a warning."""
    provider = FakeSearchProvider(name="searxng")
    from app.core.errors import SearchProviderUnavailableError

    pipeline.install(
        provider,
        cache_config=SearchCacheConfig(enabled=True, default_ttl_seconds=60, stale_if_error=True),
    )
    client = TestClient(app)

    ok = client.post("/v1/search", json={"query": "Dolphins"}, headers=AUTH)
    assert ok.status_code == 200

    # Force expiry of the stored entry, then take the provider down.
    assert len(provider.search_calls) == 1
    provider.set_search_error(SearchProviderUnavailableError("providers are down"))
    cache: SearchCache = pipeline.cache
    # Backdate the entry past its expiry in the real SQLite table.
    key = cache.router  # noqa: F841 - cache instance is the real one
    from app.search.cache import make_search_cache_key
    from app.search.models import SearchQuery

    k = make_search_cache_key(SearchQuery(query="Dolphins"))
    cache._store._conn.execute(
        "UPDATE search_cache SET fetched_at = '2020-01-01T00:00:00+00:00', "
        "expires_at = '2020-01-01T00:01:00+00:00' WHERE cache_key = ?",
        (k,),
    )
    cache._store._conn.commit()

    body = client.post("/v1/search", json={"query": "Dolphins"}, headers=AUTH)
    assert body.status_code == 200
    data = body.json()
    assert data["metadata"]["from_cache"] is True
    assert data["metadata"]["stale"] is True
    assert any("stale" in w for w in data["metadata"]["warnings"])


def test_invalid_request_never_touched_cache_and_never_masked_by_stale(pipeline):
    """Guard — deterministic request errors are returned as-is; stale entries
    must never mask an invalid request (and the cache is not written)."""
    provider = FakeSearchProvider(name="searxng")
    pipeline.install(provider)
    client = TestClient(app)

    # Prime the cache with a valid response first.
    assert client.post("/v1/search", json={"query": "Dolphins"}, headers=AUTH).status_code == 200

    bad = client.post("/v1/search", json={"query": "Dolphins", "limit": 1_000_000}, headers=AUTH)
    # The public schema rejects the invalid limit (422); the stale entry must
    # never be served in its place.
    assert bad.status_code == 422
    assert len(provider.search_calls) == 1  # validation never reaches the provider

    # An invalid request with *valid* shape but a stale cache behind it also
    # never surfaces stale data: the error is reported as-is.
    invalid = client.post(
        "/v1/search", json={"query": "Dolphins", "verticals": ["web", "web"]}, headers=AUTH
    )
    assert invalid.status_code == 422
    assert len(provider.search_calls) == 1
    assert "stale" not in invalid.text
