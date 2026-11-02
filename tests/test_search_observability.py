"""Observability contract tests for the search API.

Pins the monitoring and health contracts the ticket calls for:

- **Metrics** — a search request flows into the collector: request /
  success / error / cache counters, search latency, and the new
  per-provider labelled counters (``search_provider_*_total``) plus the
  ``search_fallbacks_total`` counter;
- **Label hygiene** — user input (query text, result URLs) must never
  appear in ``/metrics`` output; only the bounded, operator-configured
  provider names may be labels;
- **Health** — ``/health`` stays I/O-free (never touches providers or the
  search subsystem); ``/health/deep`` reports the search subsystem
  (cache policy, providers, availability, SearXNG ``/config`` discovery
  state) and fails only on *local* infrastructure problems — external
  search backends are reported, never fatal.

All tests are offline: SearXNG runs against an ``httpx.MockTransport``
fake instance.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import (
    NoProviderAvailableError,
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
)
from app.main import app
from app.metrics import prometheus as prometheus_module
from app.metrics.prometheus import MetricsCollector
from app.search import SearchCache, SearchCacheConfig, SearchProviderRegistry, SearchRouter
from app.search.models import SearchQuery, Vertical
from app.search.providers.searxng import SearxngConfig, SearxngProvider
from tests.test_search_api import AUTH_HEADER, VALID_API_KEY, FakeProvider
from tests.test_searxng_provider import BASE_URL, MockSearxng

UNIQUE_QUERY = "needle-hygiene-777 unique query tokens"


# ====================================================================== fixtures


@pytest.fixture()
def fresh_metrics(monkeypatch) -> MetricsCollector:
    """A fresh shared collector installed as the ``app.metrics.prometheus``
    singleton.

    Every consumer (search route, router, cache, ``/metrics``, deep health)
    calls the same ``get_metrics()`` function, which resolves the
    ``prometheus._metrics`` module global — installing the collector there
    reaches all of them (monkeypatching the module attributes would not:
    FastAPI captured the function object at import time).
    """
    collector = MetricsCollector()
    monkeypatch.setattr(prometheus_module, "_metrics", collector)
    return collector


@pytest.fixture()
def pipeline(tmp_path):
    """Install a fresh search pipeline on ``app.state``.

    Objects are built *without* explicit metrics so they pick up the
    patched (shared) collector from :func:`fresh_metrics` — exactly what
    production wiring does via the lifespan.
    """
    app.state.settings = Settings(api_key=VALID_API_KEY)
    app.state.cache = MagicMock()
    app.state.cache.stats.return_value = {"total_entries": 0}

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
            )
            return app.state.search_cache

    yield _Installer()

    for store in installed:
        try:
            store.close()
        except Exception:
            pass
    for attr in ("cache", "search_cache"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def _router(providers: list[Any]) -> SearchRouter:
    return SearchRouter(SearchProviderRegistry(providers))


def _searxng_provider(mock: MockSearxng) -> SearxngProvider:
    return SearxngProvider(
        SearxngConfig(enabled=True, base_url=BASE_URL, config_cache_ttl_seconds=300),
        client=mock.client(),
    )


def _search_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"query": UNIQUE_QUERY}
    body.update(overrides)
    return body


def _client() -> TestClient:
    return TestClient(app)


def _metrics_lines(collector: MetricsCollector, *needles: str) -> list[str]:
    """Metric lines whose name matches any needle (values only)."""
    lines = [
        line
        for line in collector.render().splitlines()
        if line and not line.startswith("#") and any(needle in line for needle in needles)
    ]
    return lines


def _counter(collector: MetricsCollector, name: str, **labels: str) -> int:
    """Read a counter value, optionally for a specific label series."""
    if labels:
        key = tuple(sorted(labels.items()))
        return collector._labeled_counters.get(name, {}).get(key, 0)
    return collector._counters.get(name, 0)


# ==================================================================== metrics


class TestMetricsCollectorLabels:
    def test_labeled_series_render_and_escape(self):
        collector = MetricsCollector()
        collector.inc("search_provider_attempts_total", provider="searxng")
        collector.inc("search_provider_attempts_total", provider="searxng")
        collector.inc("search_provider_attempts_total", provider='we"ird\\name')

        output = collector.render()
        assert 'search_provider_attempts_total{"provider"="searxng"} 2' in output
        # The quote and backslash must be escaped in the label value.
        assert 'search_provider_attempts_total{"provider"="we\\"ird\\\\name"} 1' in output

    def test_label_series_are_sorted(self):
        collector = MetricsCollector()
        collector.inc("search_provider_success_total", provider="zeta")
        collector.inc("search_provider_success_total", provider="alpha")

        rendered = collector.render().splitlines()
        lines = [line for line in rendered if line.startswith("search_provider_success_total")]
        assert lines == [
            'search_provider_success_total{"provider"="alpha"} 1',
            'search_provider_success_total{"provider"="zeta"} 1',
        ]

    def test_unlabeled_counters_stay_plain(self):
        collector = MetricsCollector()
        collector.inc("search_requests_total")
        lines = _metrics_lines(collector, "search_requests_total")
        assert lines == ["search_requests_total 1"]


class TestRouterMetrics:
    """The router records per-provider attempt/success/error + fallbacks."""

    def _query(self, provider: str = "auto") -> SearchQuery:
        return SearchQuery(query="q", verticals=[Vertical.WEB], provider=provider)

    async def test_success_counts_provider_attempt_and_success(self):
        collector = MetricsCollector()
        provider = FakeProvider(name="alpha")
        router = SearchRouter(SearchProviderRegistry([provider]), metrics=collector)

        response = await router.route(self._query())

        assert response.metadata.provider == "alpha"
        assert _counter(collector, "search_provider_attempts_total", provider="alpha") == 1
        assert _counter(collector, "search_provider_success_total", provider="alpha") == 1
        assert _counter(collector, "search_provider_error_total") == 0
        assert _counter(collector, "search_fallbacks_total") == 0

    async def test_explicit_failure_counts_error_without_fallback(self):
        collector = MetricsCollector()
        router = SearchRouter(
            SearchProviderRegistry(
                [
                    FakeProvider(
                        name="alpha", search_error=SearchProviderTimeoutError("alpha down")
                    ),
                    FakeProvider(name="beta"),
                ]
            ),
            metrics=collector,
        )

        with pytest.raises(SearchProviderTimeoutError):
            await router.route(self._query(provider="alpha"))

        assert _counter(collector, "search_provider_attempts_total", provider="alpha") == 1
        assert _counter(collector, "search_provider_error_total", provider="alpha") == 1
        assert _counter(collector, "search_provider_attempts_total", provider="beta") == 0
        assert _counter(collector, "search_fallbacks_total") == 0  # explicit never falls back

    async def test_auto_fallback_counts_every_attempt_and_the_fallback(self):
        collector = MetricsCollector()
        router = SearchRouter(
            SearchProviderRegistry(
                [
                    FakeProvider(
                        name="alpha", search_error=SearchProviderTimeoutError("alpha down")
                    ),
                    FakeProvider(name="beta"),
                ]
            ),
            metrics=collector,
        )

        response = await router.route(self._query())

        assert response.metadata.provider == "beta"
        assert response.metadata.fallback_occurred is True
        assert _counter(collector, "search_provider_attempts_total", provider="alpha") == 1
        assert _counter(collector, "search_provider_error_total", provider="alpha") == 1
        assert _counter(collector, "search_provider_attempts_total", provider="beta") == 1
        assert _counter(collector, "search_provider_success_total", provider="beta") == 1
        assert _counter(collector, "search_fallbacks_total") == 1

    async def test_all_providers_failing_counts_every_error(self):
        collector = MetricsCollector()
        router = SearchRouter(
            SearchProviderRegistry(
                [
                    FakeProvider(
                        name="alpha", search_error=SearchProviderTimeoutError("alpha down")
                    ),
                    FakeProvider(
                        name="beta", search_error=SearchProviderUnavailableError("beta down")
                    ),
                ]
            ),
            metrics=collector,
        )

        with pytest.raises(NoProviderAvailableError):
            await router.route(self._query())

        assert _counter(collector, "search_provider_error_total", provider="alpha") == 1
        assert _counter(collector, "search_provider_error_total", provider="beta") == 1
        assert _counter(collector, "search_fallbacks_total") == 1  # alpha -> beta


class TestMetricsEndpoint:
    def test_search_request_reaches_all_search_counters(self, fresh_metrics, pipeline):
        pipeline.install(_router([FakeProvider(name="alpha")]))

        response = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text

        assert _counter(fresh_metrics, "search_requests_total") == 1
        assert _counter(fresh_metrics, "search_success_total") == 1
        assert _counter(fresh_metrics, "search_error_total") == 0
        assert _counter(fresh_metrics, "search_cache_misses_total") == 1
        assert _counter(fresh_metrics, "search_cache_hits_total") == 0
        assert _counter(fresh_metrics, "search_provider_attempts_total", provider="alpha") == 1
        assert _counter(fresh_metrics, "search_provider_success_total", provider="alpha") == 1
        assert fresh_metrics._latencies["search_duration_ms_sum"] >= 0.0

        output = fresh_metrics.render()
        assert "search_requests_total 1" in output
        assert 'search_provider_attempts_total{"provider"="alpha"} 1' in output

    def test_error_response_counts_error_not_success(self, fresh_metrics, pipeline):
        provider = FakeProvider(name="alpha", search_error=SearchProviderUnavailableError("down"))
        pipeline.install(_router([provider]))

        response = _client().post("/v1/search", json=_search_body(), headers=AUTH_HEADER)
        assert response.status_code == 503, response.text

        assert _counter(fresh_metrics, "search_requests_total") == 1
        assert _counter(fresh_metrics, "search_success_total") == 0
        assert _counter(fresh_metrics, "search_error_total") == 1
        assert _counter(fresh_metrics, "search_provider_error_total", provider="alpha") == 1

    def test_metrics_output_never_contains_user_input(self, fresh_metrics, pipeline):
        """Label hygiene: query text and result URLs must never appear in
        ``/metrics`` — only the bounded provider name may be a label."""
        provider = FakeProvider(name="alpha")
        pipeline.install(_router([provider]))

        _client().post(
            "/v1/search",
            json=_search_body(limit=10),
            headers=AUTH_HEADER,
        )
        # A second, different query, to prove repeated series do not grow.
        _client().post(
            "/v1/search", json=_search_body(query="another-query-with-urls"), headers=AUTH_HEADER
        )

        output = fresh_metrics.render()
        assert UNIQUE_QUERY not in output
        assert "another-query-with-urls" not in output
        assert "example.com" not in output  # result URLs stay out of labels

        # The provider series count is bounded by the provider *names*.
        provider_lines = [
            line
            for line in output.splitlines()
            if line.startswith("search_provider_attempts_total{")
        ]
        assert len(provider_lines) == 1  # one provider -> one series


# ====================================================================== health


class TestHealthContracts:
    def test_liveness_is_io_free(self, fresh_metrics, pipeline):
        provider = FakeProvider(name="alpha", capability_error=RuntimeError("boom"))
        provider.capability_calls = 0

        async def _counting_capabilities():
            provider.capability_calls += 1
            raise RuntimeError("boom")

        provider.capabilities = _counting_capabilities
        pipeline.install(_router([provider]))

        response = _client().get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert provider.capability_calls == 0  # liveness never probes providers

    def test_deep_health_reports_healthy_search_subsystem(self, fresh_metrics, pipeline):
        pipeline.install(_router([FakeProvider(name="alpha")]))

        response = _client().get("/health/deep")

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "ok"
        assert data["checks"]["cache"] == "ok"
        assert data["checks"]["search"] == {
            "status": "ok",
            "configured": True,
            "cache_enabled": True,
            "providers": [{"name": "alpha", "available": True, "probe": "ok"}],
        }

    def test_deep_health_reports_down_provider_but_stays_ok(self, fresh_metrics, pipeline):
        provider = FakeProvider(name="alpha", capability_error=RuntimeError("down"))
        pipeline.install(_router([provider]))

        response = _client().get("/health/deep")

        # External backends are reported, never fatal.
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "ok"
        search = data["checks"]["search"]
        assert search["status"] == "degraded (no provider available)"
        assert search["providers"] == [{"name": "alpha", "available": False, "probe": "failed"}]

    def test_deep_health_reports_searxng_discovery_state(self, fresh_metrics, pipeline):
        pipeline.install(_router([_searxng_provider(MockSearxng())]))

        response = _client().get("/health/deep")

        assert response.status_code == 200, response.text
        provider_report = response.json()["checks"]["search"]["providers"][0]
        assert provider_report["name"] == "searxng"
        assert provider_report["available"] is True
        assert provider_report["probe"] == "ok"
        assert provider_report["discovery_state"] == "ok"
        assert provider_report["enabled_engines"] == 3  # brave is disabled in the fake
        assert provider_report["instance_version"] == "4.0.0"
        # The instance URL is configuration, not a public health detail.
        assert BASE_URL not in response.text

    def test_deep_health_reports_searxng_unreachable_but_stays_ok(self, fresh_metrics, pipeline):
        pipeline.install(_router([_searxng_provider(MockSearxng(config_status=500))]))

        response = _client().get("/health/deep")

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "ok"
        search = data["checks"]["search"]
        assert search["status"] == "degraded (no provider available)"
        provider_report = search["providers"][0]
        assert provider_report["available"] is False
        assert provider_report["discovery_state"] == "not_discovered"

    def test_deep_health_503_when_search_subsystem_missing(self, fresh_metrics):
        # No search cache installed at all: a local infrastructure problem.
        if hasattr(app.state, "search_cache"):
            delattr(app.state, "search_cache")

        response = _client().get("/health/deep")

        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert response.json()["checks"]["search"] == "unavailable"

    def test_deep_health_reports_no_providers_configured(self, fresh_metrics, pipeline):
        pipeline.install(_router([]))

        response = _client().get("/health/deep")

        assert response.status_code == 200, response.text
        assert response.json()["checks"]["search"]["status"] == "no providers configured"
