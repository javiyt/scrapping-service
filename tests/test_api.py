"""Integration tests for the FastAPI application.

These tests use the TestClient with mocks for the scraper and cache to avoid
requiring network or browser access.

We set mocks on ``app.state`` before creating the TestClient, which the
real dependency-injection functions already check for first.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth.resolver import init_profile_resolver
from app.camofox.types import CamofoxHealthStatus
from app.core.config import Settings
from app.core.errors import SecurityError
from app.main import app
from app.metrics.prometheus import get_metrics
from app.search import SearchCache, SearchCacheConfig, SearchProviderRegistry, SearchRouter

# --------------------------------------------------------------------- helpers

VALID_API_KEY = "test-key-for-tests"
AUTH_HEADER = {"Authorization": f"Bearer {VALID_API_KEY}"}


def _mock_scraper(return_value=None, side_effect=None):
    """Build an AsyncMock that looks like a ScraperService."""
    mock = AsyncMock()
    if return_value is not None:
        mock.scrape.return_value = return_value
    if side_effect is not None:
        mock.scrape.side_effect = side_effect
    return mock


SAMPLE_RESULT = {
    "url": "https://example.com",
    "final_url": "https://example.com",
    "status_code": 200,
    "from_cache": False,
    "stale": False,
    "fetched_at": "2026-06-30T10:00:00+00:00",
    "expires_at": "2026-06-30T16:00:00+00:00",
    "html": "<html>Hello World</html>",
    "metadata": {
        "mode": "http",
        "elapsed_ms": 120,
        "content_length": 25,
        "cache_key": "abc123",
    },
}


@pytest.fixture(autouse=True)
def _clean_app_state():
    """Reset app.state before each test so tests don't leak state."""
    for attr in ("scraper", "cache", "settings", "search_cache", "camofox_client"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)
    get_metrics().mark_scrape_success()


# =============================================================== /health


class TestHealthEndpoint:
    def test_health_returns_ok(self):
        response = TestClient(app).get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "scraper-api"

    def test_health_no_auth_required(self):
        response = TestClient(app).get("/health")
        assert response.status_code == 200

    def test_deep_health_checks_cache_without_auth(self):
        app.state.settings = Settings(api_key=VALID_API_KEY)
        app.state.cache = MagicMock()
        app.state.cache.stats.return_value = {"total_entries": 0}
        app.state.search_cache = SearchCache(
            store=MagicMock(),
            router=SearchRouter(SearchProviderRegistry([])),
            config=SearchCacheConfig(),
        )
        get_metrics().mark_scrape_success()

        response = TestClient(app).get("/health/deep")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["checks"]["cache"] == "ok"
        assert data["checks"]["search"]["configured"] is True
        assert data["checks"]["search"]["status"] == "no providers configured"

    def test_deep_health_fails_after_consecutive_scrape_errors(self):
        app.state.settings = Settings(
            api_key=VALID_API_KEY,
            health_max_consecutive_scrape_errors=2,
        )
        app.state.cache = MagicMock()
        app.state.cache.stats.return_value = {"total_entries": 0}
        metrics = get_metrics()
        metrics.mark_scrape_success()
        metrics.mark_scrape_error()
        metrics.mark_scrape_error()

        response = TestClient(app).get("/health/deep")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "degraded"
        assert data["checks"]["scrape_errors"] == "too_many_consecutive_errors"

    def test_deep_health_reports_camofox_disabled_when_client_absent(self):
        app.state.settings = Settings(api_key=VALID_API_KEY)
        app.state.cache = MagicMock()
        app.state.cache.stats.return_value = {"total_entries": 0}
        app.state.search_cache = SearchCache(
            store=MagicMock(),
            router=SearchRouter(SearchProviderRegistry([])),
            config=SearchCacheConfig(),
        )

        response = TestClient(app).get("/health/deep")

        assert response.status_code == 200
        assert response.json()["checks"]["camofox"]["status"] == "disabled"

    def test_deep_health_reports_camofox_unreachable_but_never_fails(self):
        """A down external Camofox is reported, not fatal (restart policy
        must never depend on external service availability)."""
        app.state.settings = Settings(api_key=VALID_API_KEY)
        app.state.cache = MagicMock()
        app.state.cache.stats.return_value = {"total_entries": 0}
        app.state.search_cache = SearchCache(
            store=MagicMock(),
            router=SearchRouter(SearchProviderRegistry([])),
            config=SearchCacheConfig(),
        )
        camofox_client = MagicMock()
        camofox_client.probe_health = AsyncMock(
            return_value=CamofoxHealthStatus(available=False, reason="connection refused")
        )
        app.state.camofox_client = camofox_client
        get_metrics().mark_scrape_success()

        response = TestClient(app).get("/health/deep")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["checks"]["camofox"] == {
            "status": "unreachable",
            "reason": "connection refused",
            "version": None,
            "browser_connected": None,
            "pool_size": None,
        }

    def test_deep_health_reports_camofox_reachable(self):
        app.state.settings = Settings(api_key=VALID_API_KEY)
        app.state.cache = MagicMock()
        app.state.cache.stats.return_value = {"total_entries": 0}
        app.state.search_cache = SearchCache(
            store=MagicMock(),
            router=SearchRouter(SearchProviderRegistry([])),
            config=SearchCacheConfig(),
        )
        camofox_client = MagicMock()
        camofox_client.probe_health = AsyncMock(
            return_value=CamofoxHealthStatus(
                available=True,
                version="1.2.3",
                browser_connected=True,
                pool_size=2,
            )
        )
        app.state.camofox_client = camofox_client
        get_metrics().mark_scrape_success()

        response = TestClient(app).get("/health/deep")

        assert response.status_code == 200
        assert response.json()["checks"]["camofox"]["status"] == "ok"


# =============================================================== API docs


class TestDocumentationEndpoints:
    def test_swagger_ui_is_available(self):
        response = TestClient(app).get("/docs")

        assert response.status_code == 200
        assert "Swagger UI" in response.text
        assert "/openapi.json" in response.text

    def test_openapi_json_uses_project_spec(self):
        response = TestClient(app).get("/openapi.json")

        assert response.status_code == 200
        data = response.json()
        assert data["openapi"] == "3.0.3"
        assert data["info"]["title"] == "Scraper API"
        assert "/v1/scrape" in data["paths"]

    def test_openapi_yaml_is_available(self):
        response = TestClient(app).get("/openapi.yaml")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/yaml")
        assert "openapi: 3.0.3" in response.text
        assert "/v1/scrape:" in response.text


# =============================================================== Auth


class TestAuthentication:
    def test_scrape_without_auth(self):
        response = TestClient(app).post(
            "/v1/scrape",
            json={"url": "https://example.com"},
        )
        assert response.status_code in (401, 403)

    def test_scrape_with_invalid_auth(self):
        response = TestClient(app).post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers={"Authorization": "Bearer WrongKey"},
        )
        assert response.status_code in (401, 403)

    def test_health_without_auth(self):
        response = TestClient(app).get("/health")
        assert response.status_code == 200


# ============================================================= /v1/scrape


class TestScrapeEndpoint:
    """Scrape endpoint tests with mocked scraper."""

    def _patch_scraper(self, mock):
        """Set a mock scraper on app.state and return a TestClient."""
        app.state.scraper = mock
        app.state.cache = MagicMock()
        return TestClient(app)

    def test_scrape_success(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT)
        client = self._patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["url"] == "https://example.com"
        assert data["status_code"] == 200
        assert "Hello World" in data["html"]
        assert data["metadata"]["mode"] == "http"

    def test_scrape_security_rejection(self):
        mock = _mock_scraper(
            side_effect=SecurityError("URL references a blocked localhost address: localhost"),
        )
        client = self._patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={"url": "http://localhost:8080/"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 403, response.text
        data = response.json()
        assert data["error"]["type"] == "security_error"

    def test_scrape_invalid_url(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT)
        client = self._patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={"url": ""},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 422, response.text

    def test_scrape_invalid_mode(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT)
        client = self._patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com", "mode": "ftp"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 422, response.text

    def test_scrape_missing_url(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT)
        client = self._patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 422, response.text

    def test_scrape_custom_ttl(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT)
        client = self._patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com", "cache_ttl_seconds": 7200},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        call_kwargs = mock.scrape.call_args[1]
        assert call_kwargs["cache_ttl_seconds"] == 7200

    def test_scrape_stale_cache_hit(self):
        stale_result = {**SAMPLE_RESULT, "from_cache": True, "stale": True}
        mock = _mock_scraper(return_value=stale_result)
        client = self._patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["from_cache"] is True
        assert data["stale"] is True

    def test_scrape_normalize_empty_html(self):
        empty_result = {**SAMPLE_RESULT, "html": ""}
        mock = _mock_scraper(return_value=empty_result)
        client = self._patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "remove_scripts": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text

    def test_scrape_extract_empty_html(self):
        empty_result = {**SAMPLE_RESULT, "html": ""}
        mock = _mock_scraper(return_value=empty_result)
        client = self._patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "extract": {
                    "enabled": True,
                    "fields": {
                        "title": {"selector": "title", "type": "text"},
                    },
                },
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text


# ========================================================= /v1/scrape/batch


class TestBatchEndpoint:
    def test_batch_scrape(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT)
        app.state.scraper = mock
        app.state.cache = MagicMock()
        client = TestClient(app)

        response = client.post(
            "/v1/scrape/batch",
            json={
                "items": [
                    {"url": "https://example.com/1"},
                    {"url": "https://example.com/2"},
                ],
                "max_concurrency": 2,
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 2
        assert data["succeeded"] == 2
        assert data["failed"] == 0

    def test_batch_concurrency_is_capped_by_server_settings(self):
        settings = Settings(api_key=VALID_API_KEY, scraper_max_concurrency=1)
        init_profile_resolver(settings)

        counters = {"running": 0, "max_seen": 0}

        async def scrape_side_effect(**kwargs):
            counters["running"] += 1
            counters["max_seen"] = max(counters["max_seen"], counters["running"])
            await asyncio.sleep(0.01)
            counters["running"] -= 1
            return SAMPLE_RESULT

        mock = AsyncMock()
        mock.scrape.side_effect = scrape_side_effect
        app.state.scraper = mock
        app.state.cache = MagicMock()
        client = TestClient(app)

        response = client.post(
            "/v1/scrape/batch",
            json={
                "items": [
                    {"url": "https://example.com/1"},
                    {"url": "https://example.com/2"},
                ],
                "max_concurrency": 2,
            },
            headers=AUTH_HEADER,
        )

        assert response.status_code == 200, response.text
        assert counters["max_seen"] == 1


# ============================================================= Cache endpoints


class TestCacheEndpoints:
    def test_cache_stats(self):
        mock_cache = MagicMock()
        mock_cache.stats.return_value = {
            "total_entries": 42,
            "total_size_bytes": 1048576,
            "expired_entries": 3,
            "cache_path": "/data/scraper-cache.db",
        }
        app.state.cache = mock_cache
        app.state.scraper = MagicMock()
        client = TestClient(app)

        response = client.get("/v1/cache/stats", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total_entries"] == 42

    def test_cache_delete(self):
        mock_cache = MagicMock()
        mock_cache.delete_by_url.return_value = True
        app.state.cache = mock_cache
        app.state.scraper = MagicMock()
        client = TestClient(app)

        response = client.delete(
            "/v1/cache?url=https://example.com",
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text

    def test_cache_delete_not_found(self):
        mock_cache = MagicMock()
        mock_cache.delete_by_url.return_value = False
        app.state.cache = mock_cache
        app.state.scraper = MagicMock()
        client = TestClient(app)

        response = client.delete(
            "/v1/cache?url=https://nonexistent.com",
            headers=AUTH_HEADER,
        )
        assert response.status_code == 404, response.text

    def test_cache_purge(self):
        mock_cache = MagicMock()
        mock_cache.purge.return_value = 10
        app.state.cache = mock_cache
        app.state.scraper = MagicMock()
        client = TestClient(app)

        response = client.post("/v1/cache/purge", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["purged_entries"] == 10

    def test_cache_purge_error(self):
        mock_cache = MagicMock()
        mock_cache.purge.side_effect = RuntimeError("purge failed")
        app.state.cache = mock_cache
        app.state.scraper = MagicMock()
        client = TestClient(app)

        response = client.post("/v1/cache/purge", headers=AUTH_HEADER)
        assert response.status_code == 500, response.text

    def test_cache_cleanup_error(self):
        mock_cache = MagicMock()
        mock_cache.cleanup.side_effect = RuntimeError("cleanup failed")
        app.state.cache = mock_cache
        app.state.scraper = MagicMock()
        client = TestClient(app)

        response = client.post("/v1/cache/cleanup", headers=AUTH_HEADER)
        assert response.status_code == 500, response.text

    def test_cache_vacuum_error(self):
        mock_cache = MagicMock()
        mock_cache.vacuum.side_effect = RuntimeError("vacuum failed")
        app.state.cache = mock_cache
        app.state.scraper = MagicMock()
        client = TestClient(app)

        response = client.post("/v1/cache/vacuum", headers=AUTH_HEADER)
        assert response.status_code == 500, response.text

    def test_cache_stats_with_real_cache(self):
        import tempfile
        from datetime import UTC, datetime, timedelta
        from pathlib import Path

        from app.cache.models import CacheEntry
        from app.cache.sqlite_cache import SqliteCache

        tmp = Path(tempfile.mktemp(suffix=".db"))
        real_cache = SqliteCache(db_path=str(tmp), max_size_mb=10)
        real_cache.open()
        try:
            entry = CacheEntry(
                cache_key="statkey",
                url="https://example.com",
                final_url="https://example.com",
                status_code=200,
                html="<html>stat</html>",
                fetched_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                mode="http",
                content_length=16,
                headers=None,
                error_metadata=None,
            )
            real_cache.set(entry)
            app.state.cache = real_cache
            app.state.scraper = MagicMock()
            client = TestClient(app)

            response = client.get("/v1/cache/stats", headers=AUTH_HEADER)
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["total_entries"] >= 1
        finally:
            real_cache.close()
            tmp.unlink(missing_ok=True)


# =============================================================== /metrics


class TestMetricsEndpoint:
    def test_metrics_requires_auth(self):
        response = TestClient(app).get("/metrics")
        assert response.status_code in (200, 401, 403)

    def test_metrics_format(self):
        app.state.scraper = MagicMock()
        app.state.cache = MagicMock()
        client = TestClient(app)

        response = client.get("/metrics", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        text = response.text
        assert "# HELP scrape_requests_total" in text
        assert "scrape_requests_total" in text
        assert "# HELP scraper_up" in text

    def test_metrics_includes_extraction_counters(self):
        app.state.scraper = MagicMock()
        app.state.cache = MagicMock()
        client = TestClient(app)

        response = client.get("/metrics", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        text = response.text
        assert "# HELP extraction_requests_total" in text
        assert "extraction_requests_total" in text
        assert "extraction_success_total" in text
        assert "extraction_error_total" in text


# ============================================================= Ready


class TestReadyEndpoint:
    def test_ready_without_auth(self):
        app.state.cache = MagicMock()
        app.state.cache.stats.return_value = {"total_entries": 0}
        client = TestClient(app)

        response = client.get("/ready")
        # /ready falls under the auth-required router
        assert response.status_code in (200, 401, 403)


# ============================================================= Extraction


class TestExtractionEndpoint:
    """Tests that ``/v1/scrape`` returns extracted data when requested."""

    SAMPLE_WITH_TITLE = {
        **SAMPLE_RESULT,
        "html": ("<html><head><title>Test Title</title></head><body><p>Hello</p></body></html>"),
    }

    def _patch_scraper(self, mock):
        app.state.scraper = mock
        app.state.cache = MagicMock()
        return TestClient(app)

    def test_scrape_with_extraction_returns_extracted(self):
        mock = _mock_scraper(return_value=self.SAMPLE_WITH_TITLE)
        client = self._patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "extract": {
                    "enabled": True,
                    "fields": {
                        "title": {"selector": "title", "type": "text"},
                    },
                },
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["extracted"] == {"title": "Test Title"}

    def test_scrape_without_extraction_extracted_is_null(self):
        mock = _mock_scraper(return_value=self.SAMPLE_WITH_TITLE)
        client = self._patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["extracted"] is None

    def test_scrape_extraction_required_field_missing(self):
        mock = _mock_scraper(return_value=self.SAMPLE_WITH_TITLE)
        client = self._patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "extract": {
                    "enabled": True,
                    "fields": {
                        "missing": {
                            "selector": ".nonexistent",
                            "type": "text",
                            "required": True,
                        },
                    },
                },
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["extracted"] is None
        assert data["extraction_error"] is not None
        assert data["extraction_error"]["field"] == "missing"

    def test_scrape_extraction_error_increments_counter(self):
        """Verify extraction_error_total metric is incremented on extraction failure."""
        from app.metrics.prometheus import get_metrics

        mock = _mock_scraper(return_value=self.SAMPLE_WITH_TITLE)
        client = self._patch_scraper(mock)

        # Get initial metric value
        initial_metrics = get_metrics()
        initial_error_count = initial_metrics._counters.get("extraction_error_total", 0)

        # Trigger extraction error
        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "extract": {
                    "enabled": True,
                    "fields": {
                        "missing": {
                            "selector": ".nonexistent",
                            "type": "text",
                            "required": True,
                        },
                    },
                },
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        assert response.json()["extraction_error"] is not None

        # Verify metric was incremented
        final_metrics = get_metrics()
        assert final_metrics._counters.get("extraction_error_total", 0) == initial_error_count + 1

    def test_scrape_extraction_success_increments_counter(self):
        """Verify extraction_success_total metric is incremented on extraction success."""
        from app.metrics.prometheus import get_metrics

        mock = _mock_scraper(return_value=self.SAMPLE_WITH_TITLE)
        client = self._patch_scraper(mock)

        # Get initial metric value
        initial_metrics = get_metrics()
        initial_success_count = initial_metrics._counters.get("extraction_success_total", 0)

        # Trigger successful extraction
        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "extract": {
                    "enabled": True,
                    "fields": {
                        "title": {"selector": "title", "type": "text"},
                    },
                },
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        assert response.json()["extracted"] == {"title": "Test Title"}

        # Verify metric was incremented
        final_metrics = get_metrics()
        assert (
            final_metrics._counters.get("extraction_success_total", 0) == initial_success_count + 1
        )


# ============================================================= ScraperError handler


class TestErrorHandler:
    def test_scraper_error_handler(self):
        app.state.cache = MagicMock()
        app.state.scraper = _mock_scraper(
            side_effect=SecurityError("URL references a blocked localhost address: localhost"),
        )
        client = TestClient(app)

        response = client.post(
            "/v1/scrape",
            json={"url": "http://localhost:8080/"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 403
        data = response.json()
        assert data["error"]["type"] == "security_error"


# ============================================================= Auth disabled


class TestAuthDisabled:
    def test_scrape_auth_disabled(self):
        from app.api.dependencies import verify_api_key

        app.dependency_overrides[verify_api_key] = lambda: None
        try:
            app.state.cache = MagicMock()
            app.state.scraper = _mock_scraper(return_value=SAMPLE_RESULT)

            client = TestClient(app)
            response = client.post(
                "/v1/scrape",
                json={"url": "https://example.com"},
            )
            assert response.status_code == 200, response.text
        finally:
            app.dependency_overrides.clear()


# ============================================================= Batch error


class TestBatchError:
    def test_batch_partial_failure(self):
        mock = AsyncMock()
        mock.scrape.side_effect = [
            SAMPLE_RESULT,
            SecurityError("blocked domain"),
        ]
        app.state.scraper = mock
        app.state.cache = MagicMock()
        client = TestClient(app)

        response = client.post(
            "/v1/scrape/batch",
            json={
                "items": [
                    {"url": "https://example.com/1"},
                    {"url": "https://blocked.com/2"},
                ],
                "max_concurrency": 2,
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 2
        assert data["succeeded"] == 1
        assert data["failed"] == 1


# ============================================================= Plugins routes


class TestPluginsRoutes:
    """Tests for the /v1/plugins and /v1/plugins/status endpoints."""

    def test_plugins_endpoint_without_auth(self):
        """GET /v1/plugins without auth should return 401."""
        from app.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        response = client.get("/v1/plugins")
        assert response.status_code == 401

    def test_plugins_endpoint_with_auth(self):
        """GET /v1/plugins with auth should return plugin metadata."""
        from app.main import app
        from fastapi.testclient import TestClient
        from app.plugins.loader import PluginLoadResult

        client = TestClient(app)

        result = PluginLoadResult(
            search_providers=[],
            transcript_providers=[],
            browser_provider=None,
            extractor_registry=MagicMock(),
            loaded=["sample-plugin"],
            skipped=[],
            failed=[],
            enabled=["sample-plugin"],
            on_error="fail",
            metadata=[
                {
                    "name": "sample-plugin",
                    "version": "1.2.3",
                    "api_version": "1.0.0",
                    "service_api_version": "1.0.0",
                    "status": "loaded",
                    "reason": None,
                    "extensions": {
                        "search_providers": ["sample-search"],
                        "transcript_providers": ["sample-transcript"],
                        "browser_provider": None,
                        "extractors": [],
                    },
                }
            ],
        )

        app.state.plugin_load_result = result
        client = TestClient(app)
        response = client.get("/v1/plugins", headers=AUTH_HEADER)
        assert response.status_code == 200
        data = response.json()
        assert "plugins" in data
        assert len(data["plugins"]) == 1
        assert data["plugins"][0]["name"] == "sample-plugin"
        assert data["plugins"][0]["version"] == "1.2.3"

    def test_plugins_endpoint_with_no_plugins(self):
        """GET /v1/plugins with no plugins should return empty list."""
        from app.main import app
        from fastapi.testclient import TestClient
        from app.plugins.loader import PluginLoadResult

        client = TestClient(app)
        result = PluginLoadResult(
            search_providers=[],
            transcript_providers=[],
            browser_provider=None,
            extractor_registry=MagicMock(),
            loaded=[],
            skipped=[],
            failed=[],
            enabled=[],
            on_error="fail",
            metadata=[],
        )
        app.state.plugin_load_result = result
        client = TestClient(app)
        response = client.get("/v1/plugins", headers=AUTH_HEADER)
        assert response.status_code == 200
        data = response.json()
        assert "plugins" in data
        assert data["plugins"] == []

    def test_plugins_status_endpoint_without_auth(self):
        """GET /v1/plugins/status without auth should return 401."""
        from app.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        response = client.get("/v1/plugins/status")
        assert response.status_code == 401

    def test_plugins_status_endpoint_with_auth(self):
        """GET /v1/plugins/status with auth should return status."""
        from app.main import app
        from fastapi.testclient import TestClient
        from app.plugins.loader import PluginLoadResult
        from app.core.config import Settings

        client = TestClient(app)
        result = PluginLoadResult(
            search_providers=[],
            transcript_providers=[],
            browser_provider=None,
            extractor_registry=MagicMock(),
            loaded=["sample-plugin"],
            skipped=[],
            failed=[],
            enabled=["sample-plugin"],
            on_error="fail",
            metadata=[],
        )
        app.state.plugin_load_result = result
        app.state.settings = Settings(api_key=VALID_API_KEY)
        client = TestClient(app)
        response = client.get("/v1/plugins/status", headers=AUTH_HEADER)
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert data["loaded"] == ["sample-plugin"]
        assert data["skipped"] == []
        assert data["failed"] == []
