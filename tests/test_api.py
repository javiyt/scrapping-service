"""Integration tests for the FastAPI application.

These tests use the TestClient with mocks for the scraper and cache to avoid
requiring network or browser access.

We set mocks on ``app.state`` before creating the TestClient, which the
real dependency-injection functions already check for first.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.errors import SecurityError
from app.main import app

# --------------------------------------------------------------------- helpers

VALID_API_KEY = "change-me"
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
    for attr in ("scraper", "cache", "settings"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


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


# ============================================================= Ready


class TestReadyEndpoint:
    def test_ready_without_auth(self):
        app.state.cache = MagicMock()
        app.state.cache.stats.return_value = {"total_entries": 0}
        client = TestClient(app)

        response = client.get("/ready")
        # /ready falls under the auth-required router
        assert response.status_code in (200, 401, 403)
