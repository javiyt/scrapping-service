"""Observability contract tests for the transcript API.

Mirrors ``tests/test_search_observability.py`` (ticket #122) for the
transcript stack (ticket #148).  Pins the monitoring and health
contracts:

- **Metrics** — a transcript request flows into the shared
  :class:`app.metrics.prometheus.MetricsCollector`: request / success /
  error counters, transcript latency, the cache counters, the per-provider
  labelled counters (``transcript_provider_*_total``) and the
  ``transcript_fallbacks_total`` counter — all of them *rendered* in the
  ``/metrics`` Prometheus output;
- **Label hygiene** — user input (URLs, video IDs) must never appear in
  ``/metrics`` output; only the bounded, operator-configured provider
  names may be labels;
- **Health** — ``/health`` stays I/O-free (never touches transcript
  providers); ``/health/deep`` reports the transcript subsystem (cache
  policy, providers, per-provider availability) and fails only on *local*
  infrastructure problems — external transcript backends are reported,
  never fatal.

All tests are offline: fake in-memory providers (the
:class:`app.transcript.TranscriptProvider` protocol is structural); no
live YouTube is ever contacted.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import (
    TranscriptProviderTimeoutError,
    TranscriptProviderUnavailableError,
)
from app.main import app
from app.metrics import prometheus as prometheus_module
from app.metrics.prometheus import MetricsCollector
from app.search import SearchCache, SearchCacheConfig, SearchProviderRegistry, SearchRouter
from app.transcript import (
    TranscriptCache,
    TranscriptCacheConfig,
    TranscriptProviderRegistry,
    TranscriptRouter,
)
from app.transcript.cache import TranscriptCacheConfig as CacheConfigAlias  # noqa: F401
from tests.test_transcript_api import (
    AUTH_HEADER,
    VALID_API_KEY,
    FakeProvider,
)

UNIQUE_URL = "https://www.youtube.com/watch?v=hyg7hyg77zz&list=hazard-list"
UNIQUE_VIDEO_ID = "hyg7hyg77zz"  # 11 characters; must never leak into /metrics


# ====================================================================== fixtures


@pytest.fixture()
def fresh_metrics(monkeypatch) -> MetricsCollector:
    """A fresh shared collector installed as the ``app.metrics.prometheus``
    singleton.

    Every consumer (transcript route, router, cache, ``/metrics``, deep
    health) calls the same ``get_metrics()`` function, which resolves the
    ``prometheus._metrics`` module global — installing the collector there
    reaches all of them (monkeypatching the module attributes would not:
    FastAPI captured the function object at import time).
    """
    collector = MetricsCollector()
    monkeypatch.setattr(prometheus_module, "_metrics", collector)
    return collector


@pytest.fixture()
def pipeline(tmp_path):
    """Install a fresh transcript pipeline on ``app.state``.

    Objects are built *without* explicit metrics so they pick up the
    patched (shared) collector from :func:`fresh_metrics` — exactly what
    production wiring does via the lifespan.
    """
    app.state.settings = Settings(api_key=VALID_API_KEY)
    app.state.cache = MagicMock()
    app.state.cache.stats.return_value = {"total_entries": 0}
    # The transcript stack now owns a fatal check in /health/deep (the search
    # stack does too); these transcript-focused tests install a bare search
    # subsystem so the report has everything to inspect.
    _search_store = SqliteCache(db_path=str(tmp_path / "search-bridge.db"))
    _search_store.open()
    app.state.search_cache = SearchCache(
        store=_search_store,
        router=SearchRouter(SearchProviderRegistry([])),
        config=SearchCacheConfig(),
    )
    installed: list[SqliteCache] = [_search_store]

    class _Installer:
        def install(
            self,
            router: TranscriptRouter,
            cache_config: TranscriptCacheConfig | None = None,
        ) -> TranscriptCache:
            store = SqliteCache(db_path=str(tmp_path / f"transcript-{len(installed)}.db"))
            store.open()
            installed.append(store)
            app.state.transcript_router = router
            app.state.transcript_cache = TranscriptCache(
                store=store,
                router=router,
                config=cache_config or TranscriptCacheConfig(),
            )
            return app.state.transcript_cache

    yield _Installer()

    for store in installed:
        try:
            store.close()
        except Exception:
            pass
    for attr in ("cache", "search_cache", "transcript_cache", "transcript_router"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def _router(providers: list[Any], provider_order: list[str] | None = None) -> TranscriptRouter:
    from app.transcript.router import TranscriptRouterConfig

    config = None
    if provider_order is not None:
        config = TranscriptRouterConfig(provider_order=provider_order)
    return TranscriptRouter(TranscriptProviderRegistry(list(providers)), config)


def _transcript_query(**overrides: Any) -> str:
    """Build a query string for GET /v1/transcript (no leading ``?``)."""
    params: dict[str, Any] = {"url": UNIQUE_URL}
    params.update(overrides)
    pairs = []
    for key, value in params.items():
        if value is None:
            continue
        pairs.append(f"{key}={value}")
    return "&".join(pairs)


def _client() -> TestClient:
    return TestClient(app)


def _counter(collector: MetricsCollector, name: str, **labels: str) -> int:
    """Read a counter value, optionally for a specific label series."""
    if labels:
        key = tuple(sorted(labels.items()))
        return collector._labeled_counters.get(name, {}).get(key, 0)
    return collector._counters.get(name, 0)


def _metrics_lines(collector: MetricsCollector, *needles: str) -> list[str]:
    """Metric lines whose name matches any needle (values only)."""
    lines = [
        line
        for line in collector.render().splitlines()
        if line and not line.startswith("#") and any(needle in line for needle in needles)
    ]
    return lines


# ==================================================================== metrics


class TestTranscriptMetricsCollector:
    def test_request_counters_render(self):
        collector = MetricsCollector()
        collector.inc("transcript_requests_total")
        collector.inc("transcript_success_total")
        collector.inc("transcript_error_total")
        collector.inc("transcript_fallbacks_total")
        collector.observe_transcript_latency(1234)

        output = collector.render()
        assert "transcript_requests_total 1" in output
        assert "transcript_success_total 1" in output
        assert "transcript_error_total 1" in output
        assert "transcript_fallbacks_total 1" in output
        assert "transcript_duration_ms_sum 1234.0" in output
        assert "transcript_cache_hits_total 0" in output
        assert "transcript_cache_misses_total 0" in output

    def test_labeled_provider_series_render_and_escape(self):
        collector = MetricsCollector()
        collector.inc("transcript_provider_attempts_total", provider="camofox")
        collector.inc("transcript_provider_attempts_total", provider="camofox")
        collector.inc("transcript_provider_attempts_total", provider='we"ird\\name')

        output = collector.render()
        assert 'transcript_provider_attempts_total{"provider"="camofox"} 2' in output
        # The quote and backslash must be escaped in the label value.
        assert 'transcript_provider_attempts_total{"provider"="we\\"ird\\\\name"} 1' in output

    def test_label_series_are_sorted(self):
        collector = MetricsCollector()
        collector.inc("transcript_provider_success_total", provider="yt-dlp")
        collector.inc("transcript_provider_success_total", provider="camofox")

        rendered = collector.render().splitlines()
        lines = [line for line in rendered if line.startswith("transcript_provider_success_total")]
        assert lines == [
            'transcript_provider_success_total{"provider"="camofox"} 1',
            'transcript_provider_success_total{"provider"="yt-dlp"} 1',
        ]


class TestRouterMetrics:
    """The router records per-provider attempt/success/error + fallbacks."""

    def _request(self, provider: str = "auto") -> Any:
        from app.transcript.models import TranscriptRequest

        return TranscriptRequest(video_id="dQw4w9WgXcQ", languages=["en"], provider=provider)

    async def test_success_counts_provider_attempt_and_success(self):
        collector = MetricsCollector()
        provider = FakeProvider(name="alpha")
        router = TranscriptRouter(TranscriptProviderRegistry([provider]), metrics=collector)

        response = await router.route(self._request())

        assert response.metadata.provider == "alpha"
        assert _counter(collector, "transcript_provider_attempts_total", provider="alpha") == 1
        assert _counter(collector, "transcript_provider_success_total", provider="alpha") == 1
        assert _counter(collector, "transcript_provider_error_total") == 0
        assert _counter(collector, "transcript_fallbacks_total") == 0

    async def test_explicit_failure_counts_error_without_fallback(self):
        collector = MetricsCollector()
        router = TranscriptRouter(
            TranscriptProviderRegistry(
                [
                    FakeProvider(
                        name="alpha",
                        fetch_error=TranscriptProviderTimeoutError("alpha down"),
                    ),
                    FakeProvider(name="beta"),
                ]
            ),
            metrics=collector,
        )

        with pytest.raises(TranscriptProviderTimeoutError):
            await router.route(self._request(provider="alpha"))

        assert _counter(collector, "transcript_provider_attempts_total", provider="alpha") == 1
        assert _counter(collector, "transcript_provider_error_total", provider="alpha") == 1
        assert _counter(collector, "transcript_provider_attempts_total", provider="beta") == 0
        assert _counter(collector, "transcript_fallbacks_total") == 0  # explicit never falls back

    async def test_auto_fallback_counts_every_attempt_and_the_fallback(self):
        collector = MetricsCollector()
        router = TranscriptRouter(
            TranscriptProviderRegistry(
                [
                    FakeProvider(
                        name="alpha",
                        fetch_error=TranscriptProviderTimeoutError("alpha down"),
                    ),
                    FakeProvider(name="beta"),
                ]
            ),
            metrics=collector,
        )

        response = await router.route(self._request())

        assert response.metadata.provider == "beta"
        assert response.metadata.fallback_occurred is True
        assert _counter(collector, "transcript_provider_attempts_total", provider="alpha") == 1
        assert _counter(collector, "transcript_provider_error_total", provider="alpha") == 1
        assert _counter(collector, "transcript_provider_attempts_total", provider="beta") == 1
        assert _counter(collector, "transcript_provider_success_total", provider="beta") == 1
        assert _counter(collector, "transcript_fallbacks_total") == 1


class TestMetricsEndpoint:
    def test_transcript_request_reaches_all_transcript_counters(self, fresh_metrics, pipeline):
        pipeline.install(_router([FakeProvider(name="alpha")]))

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text

        assert _counter(fresh_metrics, "transcript_requests_total") == 1
        assert _counter(fresh_metrics, "transcript_success_total") == 1
        assert _counter(fresh_metrics, "transcript_error_total") == 0
        assert _counter(fresh_metrics, "transcript_cache_misses_total") == 1
        assert _counter(fresh_metrics, "transcript_cache_hits_total") == 0
        assert _counter(fresh_metrics, "transcript_provider_attempts_total", provider="alpha") == 1
        assert _counter(fresh_metrics, "transcript_provider_success_total", provider="alpha") == 1
        assert fresh_metrics._latencies["transcript_duration_ms_sum"] >= 0.0

        output = fresh_metrics.render()
        assert "transcript_requests_total 1" in output
        assert "transcript_success_total 1" in output
        assert 'transcript_provider_attempts_total{"provider"="alpha"} 1' in output

    def test_cache_hit_is_counted(self, fresh_metrics, pipeline):
        provider = FakeProvider(name="alpha")
        pipeline.install(
            _router([provider]),
            cache_config=TranscriptCacheConfig(enabled=True, default_ttl_seconds=3600),
        )
        client = _client()
        query = "/v1/transcript?" + _transcript_query()

        first = client.get(query, headers=AUTH_HEADER)
        assert first.status_code == 200
        second = client.get(query, headers=AUTH_HEADER)
        assert second.status_code == 200
        assert second.json()["metadata"]["from_cache"] is True
        assert provider.fetch_calls == [(UNIQUE_VIDEO_ID, ["en"])]  # the cache absorbed the repeat

        assert _counter(fresh_metrics, "transcript_cache_hits_total") == 1
        assert _counter(fresh_metrics, "transcript_cache_misses_total") == 1
        assert _counter(fresh_metrics, "transcript_requests_total") == 2

    def test_error_response_counts_error_not_success(self, fresh_metrics, pipeline):
        provider = FakeProvider(
            name="alpha", fetch_error=TranscriptProviderUnavailableError("down")
        )
        pipeline.install(_router([provider]))

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 503, response.text

        assert _counter(fresh_metrics, "transcript_requests_total") == 1
        assert _counter(fresh_metrics, "transcript_success_total") == 0
        assert _counter(fresh_metrics, "transcript_error_total") == 1
        assert _counter(fresh_metrics, "transcript_provider_error_total", provider="alpha") == 1

    def test_metrics_output_never_contains_user_input(self, fresh_metrics, pipeline):
        """Label hygiene: URLs and video IDs must never appear in
        ``/metrics`` — only the bounded provider name may be a label."""
        provider = FakeProvider(name="alpha")
        pipeline.install(_router([provider]))

        client = _client()
        client.get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        # A second, different video, to prove repeated series do not grow.
        client.get("/v1/transcript?video_id=aaaaaaaaaaa&force_refresh=true", headers=AUTH_HEADER)

        output = fresh_metrics.render()
        assert UNIQUE_URL not in output
        assert UNIQUE_VIDEO_ID not in output
        assert "aaaaaaaaaaa" not in output
        assert "example.com" not in output

        # The provider series count is bounded by the provider *names*.
        provider_lines = [
            line
            for line in output.splitlines()
            if line.startswith("transcript_provider_attempts_total{")
        ]
        assert len(provider_lines) == 1  # one provider -> one series


# ====================================================================== health


class TestHealthContracts:
    def test_liveness_is_io_free(self, fresh_metrics, pipeline):
        provider = FakeProvider(name="alpha")
        provider.health_calls = 0

        async def _counting_health():
            provider.health_calls += 1
            return FakeProvider(name="alpha").health()

        provider.health = _counting_health
        pipeline.install(_router([provider]))

        response = _client().get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert provider.health_calls == 0  # liveness never probes providers

    def test_deep_health_reports_healthy_transcript_subsystem(self, fresh_metrics, pipeline):
        pipeline.install(_router([FakeProvider(name="alpha")]))

        response = _client().get("/health/deep")

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "ok"
        assert data["checks"]["cache"] == "ok"
        assert data["checks"]["transcript"] == {
            "status": "ok",
            "configured": True,
            "cache_enabled": True,
            "providers": [{"name": "alpha", "available": True, "probe": "ok"}],
        }

    def test_deep_health_reports_down_provider_but_stays_ok(self, fresh_metrics, pipeline):
        provider = FakeProvider(name="alpha", available=False, reason="backend down")
        pipeline.install(_router([provider]))

        response = _client().get("/health/deep")

        # External backends are reported, never fatal.
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "ok"
        transcript = data["checks"]["transcript"]
        assert transcript["status"] == "degraded (no provider available)"
        assert transcript["providers"] == [
            {"name": "alpha", "available": False, "probe": "ok", "reason": "backend down"}
        ]

    def test_deep_health_reports_probe_failure(self, fresh_metrics, pipeline):
        provider = FakeProvider(name="alpha")

        async def _health():
            raise RuntimeError("boom")

        provider.health = _health
        pipeline.install(_router([provider]))

        response = _client().get("/health/deep")

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "ok"  # a probe failure is a health signal, not fatal
        transcript = data["checks"]["transcript"]
        assert transcript["status"] == "degraded (no provider available)"
        assert transcript["providers"] == [{"name": "alpha", "available": False, "probe": "failed"}]

    def test_deep_health_reports_missing_subsystem_without_failing(self, fresh_metrics, pipeline):
        # A search-only fixture that removed the transcript stack: surfaced,
        # never fatal (the app always builds one at startup, so this means
        # the caller replaced app.state rather than the service being broken).
        pipeline.install(_router([FakeProvider(name="alpha")]))
        delattr(app.state, "transcript_cache")

        response = _client().get("/health/deep")

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["checks"]["transcript"] == "unavailable"

    def test_deep_health_reports_no_providers_configured(self, fresh_metrics, pipeline):
        pipeline.install(_router([]))

        response = _client().get("/health/deep")

        assert response.status_code == 200, response.text
        assert response.json()["checks"]["transcript"]["status"] == "no providers configured"
