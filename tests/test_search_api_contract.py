"""End-to-end contract tests for the public search API.

These tests pin the *contract* of the search API across the full pipeline,
exactly the chain the ticket calls out:

    REST request -> validation -> cache -> router -> provider -> normalized response

as one unit, through the real FastAPI app (TestClient) with:

- real validation (Pydantic ``extra="forbid"`` request schema),
- a real :class:`app.search.SearchCache` backed by a temporary SQLite
  database,
- the real capability-aware router,
- fake in-memory providers for routing behaviour, and a *real*
  :class:`app.search.providers.searxng.SearxngProvider` for SearXNG
  behaviour — backed by an ``httpx.MockTransport`` fake instance, so **no
  public internet** is ever contacted,
- the real authentication dependency.

The centrepiece is the schema-leak guard: every key in the JSON response —
recursively, at every nesting level — must belong to the documented public
schema.  A SearXNG ``/search`` response packed with engine-specific fields
(scores, ``engines``, ``answers``, ``infoboxes``, ``suggestions``,
``corrections``, ``unresponsive_engines``, result ``metadata``, ...) must
produce a public response containing none of them.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import SearchProviderUnavailableError
from app.main import app
from app.metrics.prometheus import MetricsCollector
from app.search import (
    SearchCache,
    SearchCacheConfig,
    SearchProviderRegistry,
    SearchRouter,
    SearchRouterConfig,
    make_search_cache_key,
)
from app.search.models import Vertical
from app.search.providers.searxng import SearxngConfig, SearxngProvider
from tests.test_search_api import AUTH_HEADER, VALID_API_KEY, FakeProvider
from tests.test_searxng_provider import BASE_URL, MockSearxng

# ================================================================== contract

#: Keys allowed **anywhere** in the public JSON response (envelope, results,
#: pagination, metadata, and the contents of any list of them).
PUBLIC_KEYS: set[str] = {
    # Envelope
    "query",
    "results",
    "pagination",
    "metadata",
    # Result items
    "title",
    "url",
    "snippet",
    "rank",
    "vertical",
    "source",
    "provider",
    "thumbnail_url",
    "published_at",
    # Pagination
    "page",
    "has_more",
    "total_pages",
    "total_results",
    # Metadata
    "providers_attempted",
    "fallback_occurred",
    "from_cache",
    "stale",
    "cached_at",
    "cache_age_seconds",
    "elapsed_ms",
    "warnings",
    "degraded_capabilities",
    "auth_profile",
}

#: Keys that result *items* may contain (the envelope's ``metadata`` key must
#: not be reproducible at result level).
RESULT_KEYS: set[str] = {
    "title",
    "url",
    "snippet",
    "rank",
    "vertical",
    "source",
    "provider",
    "thumbnail_url",
    "published_at",
}

#: SearXNG-specific keys that must never appear anywhere in the public API.
#: (``metadata`` is deliberately *not* listed — the public envelope owns that
#: name; the :data:`PUBLIC_KEYS` guard covers any leaked result-level
#: ``metadata`` content instead.)
SEARXNG_SPECIFIC_KEYS: set[str] = {
    "score",
    "heuristic",
    "is_lang_detected",
    "positions",
    "engines",
    "engine",
    "img_src",
    "category",
    "answers",
    "answers_parts",
    "infoboxes",
    "infobox",
    "attributes",
    "suggestions",
    "corrections",
    "unresponsive_engines",
    "number_of_results",
    "safesearch",
    "time_range",  # SearXNG's parameter name (the public API uses filters)
    "pageno",
}

ENVELOPE_KEYS = {"query", "results", "pagination", "metadata"}


def collect_keys(node: Any) -> set[str]:
    """Collect every key appearing in ``node``, recursively."""
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            keys |= collect_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= collect_keys(item)
    return keys


def assert_public_contraction(body: dict[str, Any]) -> None:
    """Assert the whole response body obeys the public schema contract."""
    keys = collect_keys(body)
    leaked = keys - PUBLIC_KEYS
    assert not leaked, f"non-public keys leaked into the response: {sorted(leaked)}"
    searxng = keys & SEARXNG_SPECIFIC_KEYS
    assert not searxng, f"SearXNG-specific keys leaked into the response: {sorted(searxng)}"
    diff = sorted(set(body) ^ ENVELOPE_KEYS)
    assert set(body) == ENVELOPE_KEYS, f"unexpected envelope keys: {diff}"
    for result in body["results"]:
        assert set(result) <= RESULT_KEYS, (
            f"result item has non-public keys: {sorted(set(result) - RESULT_KEYS)}"
        )


# ================================================================== payload

#: A SearXNG ``/search`` (JSON) response packed with engine-specific fields.
RICH_SEARXNG_PAYLOAD: dict[str, Any] = {
    "query": "Miami Dolphins injury report",
    "number_of_results": 21,
    "results": [
        {
            "url": "https://example.com/injury-report",
            "title": "Dolphins injury report — Week 9",
            "content": "Quarterback is questionable for Sunday.",
            "engine": "google",
            "engines": ["google", "duckduckgo"],
            "category": "news",
            "img_src": "https://img.example/injury.jpg",
            "publishedDate": "2026-07-05T15:00:00Z",
            "score": 4.71,
            "heuristic": True,
            "is_lang_detected": True,
            "metadata": {"id": "s3-abc123"},
            "positions": [1, 2],
        },
        {
            "url": "https://news.example/dolphins-update",
            "title": "Dolphins roster update",
            "content": "Running back out for the season.",
            "engine": "brave",
            "engines": ["brave"],
            "category": "news",
            "score": 2.1,
        },
    ],
    "answers": [{"answer": "The Dolphins play Sunday.", "source": "wikipedia"}],
    "answers_parts": [{"text": "They play", "url": "https://x.example/play"}],
    "infoboxes": [
        {
            "infobox": "Miami Dolphins",
            "content": "American football franchise.",
            "img_src": "https://img.example/dolphins.jpg",
            "attributes": {"Founded": "1966", "Arena": "Hard Rock Stadium"},
        }
    ],
    "suggestions": ["dolphins injury", "dolphins 2026 roster"],
    "corrections": ["dolphines"],
    "unresponsive_engines": [
        ["brave", "timeout: 5s"],
        ["bing", "HTTP 503"],
    ],
}


def _searxng_config() -> SearxngConfig:
    return SearxngConfig(
        enabled=True,
        base_url=BASE_URL,
        config_cache_ttl_seconds=300,
    )


def _searxng_provider(mock: MockSearxng) -> SearxngProvider:
    return SearxngProvider(_searxng_config(), client=mock.client())


def _search_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"query": "Miami Dolphins injury report"}
    body.update(overrides)
    return body


def _client() -> TestClient:
    return TestClient(app)


def _router(
    providers: list[Any],
    config: SearchRouterConfig | None = None,
) -> SearchRouter:
    return SearchRouter(SearchProviderRegistry(providers), config or SearchRouterConfig())


@pytest.fixture()
def search_pipeline(tmp_path):
    """Install a fresh search pipeline (real SQLite-backed cache) on
    ``app.state`` and tear it down again.

    Usage: ``search_pipeline.install(router, cache_config=...)``.
    """
    app.state.settings = Settings(api_key=VALID_API_KEY)
    installed: list[SqliteCache] = []

    class _Installer:
        def install(
            self,
            router: SearchRouter,
            cache_config: SearchCacheConfig | None = None,
        ) -> SearchCache:
            store = SqliteCache(db_path=str(tmp_path / f"search-{len(installed)}.db"))
            store.open()
            installed.append(store)
            app.state.search_cache = SearchCache(
                store=store,
                router=router,
                config=cache_config or SearchCacheConfig(),
                metrics=MetricsCollector(),
            )
            return app.state.search_cache

    yield _Installer()

    for store in installed:
        try:
            store.close()
        except Exception:
            pass
    if hasattr(app.state, "search_cache"):
        delattr(app.state, "search_cache")


# ================================================= full pipeline (fake backend)


class TestFullPipeline:
    """REST -> validation -> cache -> router -> provider -> response."""

    def test_pipeline_normalizes_and_caches(self, search_pipeline):
        provider = FakeProvider(
            name="alpha",
            verticals=[Vertical.WEB],
        )
        search_pipeline.install(
            _router([provider]),
            cache_config=SearchCacheConfig(enabled=True, default_ttl_seconds=900),
        )

        first = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert first.status_code == 200, first.text
        body = first.json()
        assert_public_contraction(body)
        assert body["query"] == "Miami Dolphins injury report"
        assert body["results"][0]["provider"] == "alpha"
        assert body["metadata"]["provider"] == "alpha"
        assert body["metadata"]["from_cache"] is False
        assert provider.search_calls == 1

        second = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert second.status_code == 200, second.text
        assert_public_contraction(second.json())
        assert second.json()["metadata"]["from_cache"] is True
        assert provider.search_calls == 1  # cache absorbed the repeat

    def test_validation_rejection_never_touches_cache_or_provider(self, search_pipeline):
        provider = FakeProvider(name="alpha")
        search_pipeline.install(_router([provider]))
        store = app.state.search_cache._store

        response = _client().post(
            "/v1/search",
            json=_search_body(query="x" * 1001),  # exceeds max_length=1000
            headers=AUTH_HEADER,
        )
        assert response.status_code == 422, response.text
        assert "detail" in response.json()  # schema violation, pre-cache/provider
        assert provider.search_calls == 0
        assert store._conn.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0] == 0

        # The cache is still usable afterwards.
        ok = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert ok.status_code == 200
        assert provider.search_calls == 1

    def test_stale_if_error_serves_previous_results_end_to_end(self, search_pipeline):
        provider = FakeProvider(name="alpha")
        search_pipeline.install(
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
        body = response.json()
        assert_public_contraction(body)
        assert body["metadata"]["from_cache"] is True
        assert body["metadata"]["stale"] is True
        assert any("stale" in warning for warning in body["metadata"]["warnings"])
        assert body["results"] == first.json()["results"]  # same normalized content

    def test_identical_requests_are_deterministic(self, search_pipeline):
        provider = FakeProvider(
            name="alpha",
            supports_paging=True,
            total_pages=2,
            total_results=17,
        )
        search_pipeline.install(
            _router([provider]),
            cache_config=SearchCacheConfig(enabled=False),
        )

        bodies = []
        for _ in range(2):
            response = _client().post(
                "/v1/search", json=_search_body(force_refresh=True), headers=AUTH_HEADER
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert_public_contraction(body)
            bodies.append(
                {
                    "query": body["query"],
                    "results": body["results"],
                    "pagination": body["pagination"],
                    "metadata": {
                        key: value
                        for key, value in body["metadata"].items()
                        if key != "elapsed_ms"  # wall-clock timing is excluded
                    },
                }
            )

        assert bodies[0] == bodies[1]


# ================================================= full pipeline (real SearXNG)


class TestSearxngPipeline:
    """The same pipeline with a real SearxngProvider (mocked instance)."""

    def test_pipeline_returns_normalized_results(self, search_pipeline):
        mock = MockSearxng()
        search_pipeline.install(
            _router([_searxng_provider(mock)]),
            cache_config=SearchCacheConfig(enabled=True, default_ttl_seconds=900),
        )

        response = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        body = response.json()
        assert_public_contraction(body)

        # The mapping produced real normalized content.
        assert len(body["results"]) == 3
        first = body["results"][0]
        assert first["provider"] == "searxng"
        assert first["source"] == "google"  # engine -> source
        assert first["vertical"] == "web"  # "general" -> web
        assert body["results"][1]["vertical"] == "news"  # "news" -> news
        assert body["results"][2]["vertical"] == "maps"  # "map" -> maps
        assert first["thumbnail_url"] == "https://img.example/first.jpg"
        assert first["published_at"] == "2025-06-01T10:30:00Z"

    def test_searxng_specific_fields_never_leak(self, search_pipeline):
        """The ticket's core contract: SearXNG-specific fields must never
        appear in the public API — not at the envelope, result, pagination
        or metadata level, and not nested anywhere."""
        mock = MockSearxng(search_payload=RICH_SEARXNG_PAYLOAD)
        search_pipeline.install(
            _router([_searxng_provider(mock)]),
            cache_config=SearchCacheConfig(enabled=True, default_ttl_seconds=900),
        )

        body = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER).json()
        assert_public_contraction(body)

        # The engine-specific surface made it nowhere: no scores, no answers,
        # no infoboxes, no suggestions, no unresponsive-engine *structures*.
        assert "infoboxes" not in collect_keys(body)
        assert "answers" not in collect_keys(body)
        assert "suggestions" not in collect_keys(body)
        assert "score" not in collect_keys(body)

    def test_leak_guard_holds_on_cached_and_stale_paths(self, search_pipeline):
        mock = MockSearxng(search_payload=RICH_SEARXNG_PAYLOAD)
        search_pipeline.install(
            _router([_searxng_provider(mock)]),
            cache_config=SearchCacheConfig(
                enabled=True, default_ttl_seconds=900, stale_if_error=True
            ),
        )
        client = _client()
        body = client.post("/v1/search", json=_search_body(), headers=AUTH_HEADER).json()
        assert_public_contraction(body)  # fresh

        # Cached re-issue must stay inside the contract.
        cached = client.post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert cached.json()["metadata"]["from_cache"] is True
        assert_public_contraction(cached.json())

        # Expired + provider down: the *stored* normalized response is served
        # stale — still inside the contract.
        store = app.state.search_cache._store
        row = store._conn.execute("SELECT cache_key FROM search_cache LIMIT 1").fetchone()
        entry = store.get_search_entry(row["cache_key"])
        store.set_search_entry(
            cache_key=row["cache_key"],
            request=entry["request"],
            response=entry["response"],
            fetched_at=datetime.fromisoformat(entry["fetched_at"]),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            content_length=entry["content_length"],
        )
        mock._search_status = 500
        stale = client.post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert stale.status_code == 200, stale.text
        assert stale.json()["metadata"]["stale"] is True
        assert_public_contraction(stale.json())

    def test_unresponsive_engines_surface_as_human_warnings(self, search_pipeline):
        mock = MockSearxng(search_payload=RICH_SEARXNG_PAYLOAD)
        search_pipeline.install(_router([_searxng_provider(mock)]))

        body = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER).json()
        assert_public_contraction(body)
        warnings = " | ".join(body["metadata"]["warnings"])
        assert "brave" in warnings  # surfaced as text...
        assert "unresponsive_engines" not in collect_keys(body)  # ...not as structure


# ================================================= capabilities contract


class TestCapabilitiesContract:
    def test_capabilities_shape_is_stable(self, search_pipeline):
        """The capabilities endpoint is part of the public contract: its
        keys must stay stable and provider-private details must not leak
        (no raw /config data, no instance URLs)."""
        mock = MockSearxng()
        search_pipeline.install(_router([_searxng_provider(mock)]))

        response = _client().get("/v1/search/capabilities", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {
            "default_provider",
            "provider_order",
            "providers",
            "verticals",
            "time_ranges",
            "filters",
            "safe_search_levels",
            "cache",
        }
        provider_entry = body["providers"][0]
        assert set(provider_entry) == {
            "name",
            "available",
            "verticals",
            "time_ranges",
            "filters",
            "max_results",
            "sources",
            "warnings",
        }
        assert provider_entry["name"] == "searxng"
        assert provider_entry["available"] is True
        # The mocked instance URL must not leak into the public report.
        assert BASE_URL not in str(body)
