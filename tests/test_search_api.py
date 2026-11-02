"""Integration tests for the public search API.

Covers ``POST /v1/search`` and ``GET /v1/search/capabilities`` end to end
through the FastAPI app (TestClient), with:

- fake in-memory providers (the :class:`app.search.SearchProvider` protocol
  is structural) — no live SearXNG is ever contacted;
- a real :class:`app.search.SearchCache` backed by a temporary SQLite
  database, so caching behaviour is exercised for real;
- the real authentication dependency (API key / profiles).

Coverage follows the ticket:

- successful search
- cached search (hit / force refresh)
- validation errors (bounded query, known verticals, page, limit,
  safe-search, time range, provider, source bounds)
- explicit provider
- auto provider (selection, skipping unhealthy)
- provider failure / fallback (auto fallback, no explicit fallback,
  timeout)
- capabilities endpoint (shape, unions, unavailable, probe failure)
- authentication (missing / invalid / valid key, profile exposure)
- no provider available
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth.resolver import init_profile_resolver
from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import (
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
)
from app.main import app
from app.metrics.prometheus import MetricsCollector
from app.search import (
    SearchCache,
    SearchCacheConfig,
    SearchCapabilities,
    SearchProviderRegistry,
    SearchRouter,
    SearchRouterConfig,
    make_search_cache_key,
)
from app.search.models import (
    SafeSearch,
    SearchMetadata,
    SearchQuery,
    SearchResponse,
    SearchResult,
    TimeRange,
    Vertical,
)

# --------------------------------------------------------------------- helpers

VALID_API_KEY = "test-key-for-tests"
AUTH_HEADER = {"Authorization": f"Bearer {VALID_API_KEY}"}


class FakeProvider:
    """In-memory provider satisfying the structural SearchProvider protocol."""

    def __init__(
        self,
        name: str = "fake",
        available: bool = True,
        verticals: list[Vertical] | None = None,
        time_ranges: list[TimeRange] | None = None,
        supports_language: bool = True,
        supports_safe_search: bool = True,
        supports_paging: bool = True,
        supports_source_selection: bool = False,
        max_results: int | None = None,
        sources: list[str] | None = None,
        search_error: Exception | None = None,
        capability_error: Exception | None = None,
        total_pages: int | None = None,
        total_results: int | None = None,
    ) -> None:
        self._name = name
        self._available = available
        self._verticals = verticals or [Vertical.WEB]
        self._time_ranges = time_ranges or []
        self._supports_language = supports_language
        self._supports_safe_search = supports_safe_search
        self._supports_paging = supports_paging
        self._supports_source_selection = supports_source_selection
        self._max_results = max_results
        self._sources = sources
        self._search_error = search_error
        self._capability_error = capability_error
        self._total_pages = total_pages
        self._total_results = total_results
        self.search_calls = 0
        self.last_request: SearchQuery | None = None

    @property
    def name(self) -> str:
        return self._name

    async def capabilities(self) -> SearchCapabilities:
        if self._capability_error is not None:
            raise self._capability_error
        return SearchCapabilities(
            verticals=self._verticals,
            time_ranges=self._time_ranges,
            supports_language=self._supports_language,
            supports_safe_search=self._supports_safe_search,
            supports_paging=self._supports_paging,
            supports_source_selection=self._supports_source_selection,
            max_results=self._max_results,
            sources=self._sources,
            available=self._available,
        )

    async def search(self, request: SearchQuery) -> SearchResponse:
        self.search_calls += 1
        self.last_request = request
        if self._search_error is not None:
            raise self._search_error
        return SearchResponse(
            query=request.query,
            verticals=request.verticals,
            results=[
                SearchResult(
                    title=f"First result ({self._name})",
                    url="https://example.com/first",
                    snippet="A snippet",
                    rank=1,
                    vertical=request.verticals[0],
                    source="fake-engine",
                    provider=self._name,
                    thumbnail_url="https://example.com/thumb.png",
                ),
                SearchResult(
                    title=f"Second result ({self._name})",
                    url="https://example.com/second",
                    rank=2,
                    vertical=request.verticals[0],
                    provider=self._name,
                ),
            ],
            metadata=SearchMetadata(
                provider=self._name,
                providers_attempted=[self._name],
                elapsed_ms=42,
                page=request.page,
                total_pages=self._total_pages,
                total_results=self._total_results,
            ),
        )


def _client() -> TestClient:
    return TestClient(app)


def _search_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"query": "Miami Dolphins injury report"}
    body.update(overrides)
    return body


@pytest.fixture()
def search_state(tmp_path):
    """Install a fresh SearchCache (temp SQLite store) on app.state.

    Usage: ``search_state.install(router, cache_config=...)``.
    Tears the cache down after each test.
    """
    installed: list[SqliteCache] = []

    class _Installer:
        def install(
            self,
            router: SearchRouter,
            cache_config: SearchCacheConfig | None = None,
        ) -> None:
            store = SqliteCache(db_path=str(tmp_path / f"search-{len(installed)}.db"))
            store.open()
            installed.append(store)
            app.state.search_cache = SearchCache(
                store=store,
                router=router,
                config=cache_config or SearchCacheConfig(),
                metrics=MetricsCollector(),
            )

    yield _Installer()

    for store in installed:
        try:
            store.close()
        except Exception:
            pass
    if hasattr(app.state, "search_cache"):
        delattr(app.state, "search_cache")


def _router(
    providers: list[FakeProvider],
    config: SearchRouterConfig | None = None,
) -> SearchRouter:
    return SearchRouter(SearchProviderRegistry(providers), config or SearchRouterConfig())


# ===================================================== /v1/search — success


class TestSuccessfulSearch:
    def test_successful_search_returns_normalized_results(self, search_state):
        provider = FakeProvider(
            name="alpha",
            verticals=[Vertical.WEB, Vertical.NEWS],
            time_ranges=list(TimeRange),
            supports_source_selection=True,
            sources=["google"],
            total_pages=3,
            total_results=123,
        )
        search_state.install(_router([provider]))

        response = _client().post(
            "/v1/search",
            json=_search_body(
                verticals=["news", "web"],
                language="en-US",
                time_range="day",
                safe_search="strict",
                page=1,
                limit=10,
                provider="alpha",
                sources=["google"],
            ),
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        data = response.json()

        # Public envelope shape.
        assert data["query"] == "Miami Dolphins injury report"
        assert data["pagination"] == {
            "page": 1,
            "has_more": True,
            "total_pages": 3,
            "total_results": 123,
        }
        assert data["metadata"]["provider"] == "alpha"
        assert data["metadata"]["providers_attempted"] == ["alpha"]
        assert data["metadata"]["fallback_occurred"] is False
        assert data["metadata"]["from_cache"] is False
        assert data["metadata"]["stale"] is False
        assert data["metadata"]["elapsed_ms"] >= 0
        assert data["metadata"]["warnings"] == []
        assert data["metadata"]["degraded_capabilities"] == []

        # Request semantics reach the provider as a normalized query.
        domain = provider.last_request
        assert domain is not None
        assert domain.verticals == [Vertical.NEWS, Vertical.WEB]
        assert domain.language == "en-US"
        assert domain.time_range == TimeRange.DAY
        assert domain.safe_search == SafeSearch.STRICT
        assert domain.page == 1
        assert domain.limit == 10
        assert domain.sources == ["google"]
        assert domain.provider == "alpha"

    def test_result_items_are_provider_agnostic(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.NEWS])
        search_state.install(_router([provider]))

        response = _client().post(
            "/v1/search",
            json=_search_body(verticals=["news"]),
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        first = response.json()["results"][0]
        assert first == {
            "title": "First result (alpha)",
            "url": "https://example.com/first",
            "snippet": "A snippet",
            "rank": 1,
            "vertical": "news",
            "source": "fake-engine",
            "provider": "alpha",
            "thumbnail_url": "https://example.com/thumb.png",
            "published_at": None,
        }

    def test_public_contract_has_no_searxng_parameter_names(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.NEWS])
        search_state.install(_router([provider]))

        response = _client().post(
            "/v1/search",
            json=_search_body(verticals=["news"]),
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        text = response.text
        for searxng_name in ('"pageno"', '"safesearch"', '"categories"'):
            assert searxng_name not in text

    def test_unknown_vertical_rejected(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(_router([provider]))

        response = _client().post(
            "/v1/search",
            json=_search_body(verticals=["not-a-vertical"]),
            headers=AUTH_HEADER,
        )
        assert response.status_code == 422
        assert provider.search_calls == 0


# ====================================================== /v1/search — caching


class TestCachedSearch:
    def test_second_identical_request_is_served_from_cache(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(
            _router([provider]),
            cache_config=SearchCacheConfig(
                enabled=True, default_ttl_seconds=900, stale_if_error=True
            ),
        )
        client = _client()

        first = client.post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        second = client.post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert first.status_code == 200
        assert second.status_code == 200
        assert provider.search_calls == 1  # live run only once
        assert first.json()["metadata"]["from_cache"] is False
        assert second.json()["metadata"]["from_cache"] is True
        assert second.json()["metadata"]["stale"] is False
        # Same normalized results both times.
        assert first.json()["results"] == second.json()["results"]

    def test_equivalent_queries_share_one_cache_entry(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(
            _router([provider]),
            cache_config=SearchCacheConfig(
                enabled=True, default_ttl_seconds=900, stale_if_error=True
            ),
        )
        client = _client()

        # Case/whitespace-equivalent query → same key.
        client.post("/v1/search", json={"query": "  Miami   dolphins "}, headers=AUTH_HEADER)
        response = client.post("/v1/search", json={"query": "miami dolphins"}, headers=AUTH_HEADER)
        assert response.status_code == 200
        assert provider.search_calls == 1
        assert response.json()["metadata"]["from_cache"] is True

    def test_different_filters_miss_the_cache(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB, Vertical.NEWS])
        search_state.install(
            _router([provider]),
            cache_config=SearchCacheConfig(
                enabled=True, default_ttl_seconds=900, stale_if_error=True
            ),
        )
        client = _client()

        client.post("/v1/search", json=_search_body(verticals=["web"]), headers=AUTH_HEADER)
        response = client.post(
            "/v1/search", json=_search_body(verticals=["news"]), headers=AUTH_HEADER
        )
        assert response.status_code == 200
        assert provider.search_calls == 2
        assert response.json()["metadata"]["from_cache"] is False

    def test_force_refresh_bypasses_the_cache(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(
            _router([provider]),
            cache_config=SearchCacheConfig(
                enabled=True, default_ttl_seconds=900, stale_if_error=True
            ),
        )
        client = _client()

        client.post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        response = client.post(
            "/v1/search",
            json=_search_body(force_refresh=True),
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        assert provider.search_calls == 2
        assert response.json()["metadata"]["from_cache"] is False

    def test_stale_entry_served_when_live_search_fails(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(
            _router([provider]),
            cache_config=SearchCacheConfig(
                enabled=True, default_ttl_seconds=900, stale_if_error=True
            ),
        )
        client = _client()

        first = client.post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert first.status_code == 200
        assert first.json()["metadata"]["from_cache"] is False

        # Back-date the entry until it is expired, then take the provider down.
        store = app.state.search_cache._store
        cache_key = make_search_cache_key(provider.last_request)
        row = store.get_search_entry(cache_key)
        assert row is not None
        store.set_search_entry(
            cache_key=cache_key,
            request=row["request"],
            response=row["response"],
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            content_length=row["content_length"],
        )
        provider._search_error = SearchProviderUnavailableError("alpha is down")

        response = client.post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        meta = response.json()["metadata"]
        assert meta["from_cache"] is True
        assert meta["stale"] is True
        assert any("stale" in warning for warning in meta["warnings"])

    def test_disabled_cache_never_caches(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(
            _router([provider]),
            cache_config=SearchCacheConfig(
                enabled=False, default_ttl_seconds=900, stale_if_error=True
            ),
        )
        client = _client()

        client.post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        response = client.post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert response.status_code == 200
        assert provider.search_calls == 2
        assert response.json()["metadata"]["from_cache"] is False


# ==================================================== /v1/search — validation


class TestValidationErrors:
    """Malformed requests are rejected before any provider is contacted."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {"query": ""},  # empty query
            {"query": "   "},  # whitespace-only query
            {"query": "x" * 1001},  # oversized query
            {"verticals": []},  # no verticals
            {"verticals": ["web", "web"]},  # duplicate verticals
            {"verticals": ["bogus"]},  # unknown vertical
            {"page": 0},  # page below 1
            {"page": 101},  # page above bound
            {"limit": 0},  # limit below 1
            {"limit": 101},  # limit above bound
            {"safe_search": "sometimes"},  # unknown safe-search level
            {"time_range": "decade"},  # unknown time range
            {"language": "not a language tag!!"},  # malformed language tag
            {"sources": [f"src{i}" for i in range(51)]},  # too many sources
            {"sources": ["x" * 101]},  # source too long
            {"sources": ["google", "google"]},  # duplicate sources
            {"cache_ttl_seconds": 0},  # TTL below bound
            {"cache_ttl_seconds": 2592001},  # TTL above bound
            {"provider": ""},  # empty provider name
            {"force_refresh": "sometimes"},  # non-coercible value
        ],
    )
    def test_malformed_request_rejected_without_provider_call(self, search_state, overrides):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(_router([provider]))

        response = _client().post("/v1/search", json=_search_body(**overrides), headers=AUTH_HEADER)
        assert response.status_code == 422, response.text
        assert provider.search_calls == 0

    def test_unknown_field_rejected(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(_router([provider]))

        response = _client().post(
            "/v1/search",
            json=_search_body(unknown_parameter="value"),
            headers=AUTH_HEADER,
        )
        assert response.status_code == 422
        assert provider.search_calls == 0

    def test_searxng_parameter_names_rejected(self, search_state):
        """Provider-specific names in the public API are unknown fields."""
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(_router([provider]))

        for searxng_field in ("pageno", "safesearch", "categories"):
            response = _client().post(
                "/v1/search", json=_search_body(**{searxng_field: 1}), headers=AUTH_HEADER
            )
            assert response.status_code == 422, searxng_field
        assert provider.search_calls == 0

    def test_unconfigured_provider_structured_400(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(_router([provider]))

        response = _client().post(
            "/v1/search",
            json=_search_body(provider="ghost"),
            headers=AUTH_HEADER,
        )
        assert response.status_code == 400, response.text
        error = response.json()["error"]
        assert error["type"] == "validation_error"
        assert "ghost" in error["message"]
        assert error["details"]["available_providers"] == ["alpha"]
        assert provider.search_calls == 0

    def test_explicit_unsupported_capability_structured_400(self, search_state):
        # Provider only searches web; the request explicitly asks for news.
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(_router([provider]))

        response = _client().post(
            "/v1/search",
            json=_search_body(verticals=["news"], provider="alpha"),
            headers=AUTH_HEADER,
        )
        assert response.status_code == 400, response.text
        error = response.json()["error"]
        assert error["type"] == "unsupported_search_capability"
        assert "alpha" in error["message"]
        assert provider.search_calls == 0


# ================================================== /v1/search — providers


class TestExplicitProvider:
    def test_explicit_provider_is_used(self, search_state):
        alpha = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        beta = FakeProvider(name="beta", verticals=[Vertical.WEB])
        search_state.install(_router([alpha, beta]))

        response = _client().post(
            "/v1/search", json=_search_body(provider="beta"), headers=AUTH_HEADER
        )
        assert response.status_code == 200, response.text
        assert beta.search_calls == 1
        assert alpha.search_calls == 0
        assert response.json()["metadata"]["provider"] == "beta"
        assert response.json()["metadata"]["providers_attempted"] == ["beta"]

    def test_explicit_failure_never_falls_back(self, search_state):
        alpha = FakeProvider(
            name="alpha",
            verticals=[Vertical.WEB],
            search_error=SearchProviderUnavailableError("alpha is down"),
        )
        beta = FakeProvider(name="beta", verticals=[Vertical.WEB])
        search_state.install(_router([alpha, beta]))

        response = _client().post(
            "/v1/search", json=_search_body(provider="alpha"), headers=AUTH_HEADER
        )
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["type"] == "search_provider_unavailable"
        assert beta.search_calls == 0  # explicit requests never fall back

    def test_explicit_provider_timeout_504(self, search_state):
        alpha = FakeProvider(
            name="alpha",
            verticals=[Vertical.WEB],
            search_error=SearchProviderTimeoutError("alpha timed out"),
        )
        search_state.install(_router([alpha]))

        response = _client().post(
            "/v1/search", json=_search_body(provider="alpha"), headers=AUTH_HEADER
        )
        assert response.status_code == 504, response.text
        assert response.json()["error"]["type"] == "search_provider_timeout"


class TestAutoProvider:
    def test_auto_selects_first_healthy_provider(self, search_state):
        alpha = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        beta = FakeProvider(name="beta", verticals=[Vertical.WEB])
        router = SearchRouter(
            SearchProviderRegistry([alpha, beta]),
            SearchRouterConfig(provider_order=["beta", "alpha"]),
        )
        search_state.install(router)

        response = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        assert beta.search_calls == 1
        assert alpha.search_calls == 0
        assert response.json()["metadata"]["provider"] == "beta"

    def test_auto_skips_unhealthy_provider(self, search_state):
        alpha = FakeProvider(name="alpha", verticals=[Vertical.WEB], available=False)
        beta = FakeProvider(name="beta", verticals=[Vertical.WEB])
        router = SearchRouter(
            SearchProviderRegistry([alpha, beta]),
            SearchRouterConfig(provider_order=["alpha", "beta"]),
        )
        search_state.install(router)

        response = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        assert beta.search_calls == 1
        assert alpha.search_calls == 0
        assert any("alpha" in warning for warning in response.json()["metadata"]["warnings"])

    def test_auto_falls_back_on_transient_failure(self, search_state):
        alpha = FakeProvider(
            name="alpha",
            verticals=[Vertical.WEB],
            search_error=SearchProviderUnavailableError("alpha flaked"),
        )
        beta = FakeProvider(name="beta", verticals=[Vertical.WEB])
        router = SearchRouter(
            SearchProviderRegistry([alpha, beta]),
            SearchRouterConfig(provider_order=["alpha", "beta"]),
        )
        search_state.install(router)

        response = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["metadata"]["provider"] == "beta"
        assert data["metadata"]["providers_attempted"] == ["alpha", "beta"]
        assert data["metadata"]["fallback_occurred"] is True
        assert any("alpha" in warning for warning in data["metadata"]["warnings"])
        assert any("beta" in result["title"] for result in data["results"])

    def test_auto_all_providers_fail_503(self, search_state):
        alpha = FakeProvider(
            name="alpha",
            verticals=[Vertical.WEB],
            search_error=SearchProviderUnavailableError("alpha down"),
        )
        beta = FakeProvider(
            name="beta",
            verticals=[Vertical.WEB],
            search_error=SearchProviderTimeoutError("beta slow"),
        )
        router = SearchRouter(
            SearchProviderRegistry([alpha, beta]),
            SearchRouterConfig(provider_order=["alpha", "beta"]),
        )
        search_state.install(router)

        response = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["type"] == "no_provider_available"
        assert alpha.search_calls == 1
        assert beta.search_calls == 1

    def test_auto_request_unsatisfiable_vertical_503(self, search_state):
        # The only provider cannot honor the requested vertical in auto mode:
        # it is skipped (capability mismatch), leaving nothing eligible.
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(_router([provider]))

        response = _client().post(
            "/v1/search", json=_search_body(verticals=["news"]), headers=AUTH_HEADER
        )
        assert response.status_code == 503, response.text
        assert response.json()["error"]["type"] == "no_provider_available"
        assert provider.search_calls == 0

    def test_no_provider_configured_503(self, search_state):
        search_state.install(_router([]))

        response = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["type"] == "no_provider_available"
        assert error["details"]["reason"] == "empty registry"


# ================================================ /v1/search/capabilities


class TestCapabilitiesEndpoint:
    def test_capabilities_shape(self, search_state):
        provider = FakeProvider(
            name="alpha",
            verticals=[Vertical.WEB, Vertical.NEWS],
            time_ranges=list(TimeRange),
            supports_language=True,
            supports_safe_search=True,
            supports_paging=True,
            supports_source_selection=True,
            max_results=20,
            sources=["google", "duckduckgo"],
        )
        router = SearchRouter(
            SearchProviderRegistry([provider]),
            SearchRouterConfig(provider_order=["alpha"]),
        )
        search_state.install(router)

        response = _client().get("/v1/search/capabilities", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()

        assert data["default_provider"] == "auto"
        assert data["provider_order"] == ["alpha"]
        assert data["safe_search_levels"] == ["off", "moderate", "strict"]
        assert data["cache"] == {
            "enabled": True,
            "default_ttl_seconds": 900,
            "stale_if_error": True,
        }

        entry = data["providers"][0]
        assert entry["name"] == "alpha"
        assert entry["available"] is True
        assert entry["verticals"] == ["web", "news"]
        assert entry["time_ranges"] == ["day", "week", "month", "year"]
        assert entry["filters"] == {
            "language": True,
            "safe_search": True,
            "time_range": True,
            "paging": True,
            "source_selection": True,
        }
        assert entry["max_results"] == 20
        assert entry["sources"] == ["google", "duckduckgo"]
        assert entry["warnings"] == []

        # Service-level unions reflect the single available provider.
        assert data["verticals"] == ["web", "news"]
        assert data["time_ranges"] == ["day", "week", "month", "year"]
        assert data["filters"]["language"] is True
        assert data["filters"]["source_selection"] is True

    def test_service_unions_merge_across_available_providers(self, search_state):
        alpha = FakeProvider(
            name="alpha",
            verticals=[Vertical.WEB],
            supports_language=False,
            sources=["google"],
        )
        beta = FakeProvider(
            name="beta",
            verticals=[Vertical.NEWS, Vertical.VIDEOS],
            time_ranges=[TimeRange.DAY],
            supports_language=True,
        )
        router = SearchRouter(
            SearchProviderRegistry([alpha, beta]),
            SearchRouterConfig(provider_order=["alpha", "beta"]),
        )
        search_state.install(router)

        response = _client().get("/v1/search/capabilities", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()

        assert [p["name"] for p in data["providers"]] == ["alpha", "beta"]
        assert data["verticals"] == ["web", "news", "videos"]
        assert data["time_ranges"] == ["day"]
        assert data["filters"]["language"] is True  # any available provider

    def test_unavailable_provider_excluded_from_unions(self, search_state):
        alpha = FakeProvider(name="alpha", verticals=[Vertical.WEB], available=False, sources=None)
        beta = FakeProvider(name="beta", verticals=[Vertical.NEWS])
        search_state.install(_router([alpha, beta]))

        response = _client().get("/v1/search/capabilities", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()

        assert [p["name"] for p in data["providers"]] == ["alpha", "beta"]
        assert data["providers"][0]["available"] is False
        # Only the available provider contributes to service-level unions.
        assert data["verticals"] == ["news"]

    def test_probe_failure_reported_as_unavailable(self, search_state):
        provider = FakeProvider(
            name="alpha",
            verticals=[Vertical.WEB],
            capability_error=RuntimeError("boom"),
        )
        search_state.install(_router([provider]))

        response = _client().get("/v1/search/capabilities", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()

        entry = data["providers"][0]
        assert entry["name"] == "alpha"
        assert entry["available"] is False
        assert entry["verticals"] == []
        assert entry["sources"] is None
        assert entry["warnings"]
        assert data["verticals"] == []

    def test_empty_registry(self, search_state):
        search_state.install(_router([]))

        response = _client().get("/v1/search/capabilities", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["providers"] == []
        assert data["verticals"] == []
        assert data["time_ranges"] == []

    def test_no_provider_discovery_proxy(self, search_state):
        """The endpoint reports normalized capabilities, never provider config."""
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        search_state.install(_router([provider]))

        response = _client().get("/v1/search/capabilities", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        for key in ("instance_name", "version", "engines", "safesearch"):
            assert key not in response.text
        # Documented top-level keys only.
        assert set(response.json()) == {
            "default_provider",
            "provider_order",
            "providers",
            "verticals",
            "time_ranges",
            "filters",
            "safe_search_levels",
            "cache",
        }


# ================================================================ auth


class TestSearchAuthentication:
    def _install(self, search_state, provider: FakeProvider | None = None) -> None:
        search_state.install(
            _router([provider or FakeProvider(name="alpha", verticals=[Vertical.WEB])])
        )

    def test_search_requires_auth(self, search_state):
        self._install(search_state)
        response = _client().post("/v1/search", json=_search_body())
        assert response.status_code == 401

    def test_search_rejects_invalid_key(self, search_state):
        self._install(search_state)
        response = _client().post(
            "/v1/search",
            json=_search_body(),
            headers={"Authorization": "Bearer WrongKey"},
        )
        assert response.status_code == 403

    def test_search_accepts_valid_key(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        self._install(search_state, provider)
        response = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert response.status_code == 200
        assert provider.search_calls == 1

    def test_capabilities_requires_auth(self, search_state):
        self._install(search_state)
        response = _client().get("/v1/search/capabilities")
        assert response.status_code == 401

    def test_capabilities_accepts_valid_key(self, search_state):
        self._install(search_state)
        response = _client().get("/v1/search/capabilities", headers=AUTH_HEADER)
        assert response.status_code == 200

    def test_profile_name_exposed_in_metadata_when_configured(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        self._install(search_state, provider)

        settings = Settings(api_key="test-key-for-tests", server_api_key_required=True)
        object.__setattr__(
            settings,
            "raw_yaml_auth",
            {
                "expose_profile_in_response": True,
                "api_keys": [{"name": "news-team", "key": "news-team-key"}],
            },
        )
        init_profile_resolver(settings)

        response = _client().post(
            "/v1/search",
            json=_search_body(),
            headers={"Authorization": "Bearer news-team-key"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["metadata"]["auth_profile"] == "news-team"

    def test_profile_name_hidden_by_default(self, search_state):
        provider = FakeProvider(name="alpha", verticals=[Vertical.WEB])
        self._install(search_state, provider)

        settings = Settings(api_key="test-key-for-tests", server_api_key_required=True)
        object.__setattr__(
            settings,
            "raw_yaml_auth",
            {
                "expose_profile_in_response": False,
                "api_keys": [{"name": "news-team", "key": "news-team-key"}],
            },
        )
        init_profile_resolver(settings)

        response = _client().post(
            "/v1/search",
            json=_search_body(),
            headers={"Authorization": "Bearer news-team-key"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["metadata"]["auth_profile"] is None


# ====================================================== backward compat


class TestExistingEndpointsUnaffected:
    def test_scrape_routes_still_registered_and_working(self, search_state):
        self._install_dummy_scraper()
        response = _client().post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        assert response.json()["url"] == "https://example.com"

    @staticmethod
    def _install_dummy_scraper() -> None:
        scraper = AsyncMock()
        scraper.scrape = AsyncMock(
            return_value={
                "url": "https://example.com",
                "final_url": "https://example.com",
                "status_code": 200,
                "from_cache": False,
                "stale": False,
                "fetched_at": "2026-01-01T00:00:00+00:00",
                "expires_at": None,
                "html": "<html>ok</html>",
                "metadata": {
                    "mode": "http",
                    "elapsed_ms": 1,
                    "content_length": 11,
                    "cache_key": "k",
                },
            }
        )
        app.state.scraper = scraper
        app.state.cache = MagicMock()
