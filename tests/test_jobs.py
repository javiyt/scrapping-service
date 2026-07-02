"""Tests for the async job service and endpoints.

All tests use mocked scrapers — no external network access.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import HttpError
from app.jobs.models import JobStatus
from app.jobs.service import JobService
from app.main import app
from app.metrics.prometheus import MetricsCollector

# --------------------------------------------------------------------- helpers

VALID_API_KEY = "change-me"
AUTH_HEADER = {"Authorization": f"Bearer {VALID_API_KEY}"}

SAMPLE_RESULT = {
    "url": "https://example.com",
    "final_url": "https://example.com",
    "status_code": 200,
    "from_cache": False,
    "stale": False,
    "fetched_at": "2026-07-01T10:00:00+00:00",
    "expires_at": "2026-07-01T16:00:00+00:00",
    "html": "<html>Hello World</html>",
    "metadata": {
        "mode": "http",
        "elapsed_ms": 120,
        "content_length": 25,
        "cache_key": "abc123",
    },
}


def _make_settings(**overrides: object) -> Settings:
    """Return a Settings instance for testing."""
    defaults = {
        "jobs_enabled": True,
        "jobs_max_retained": 10,
        "jobs_max_concurrency": 2,
        "jobs_result_ttl_seconds": 86400,
    }
    merged = {**defaults, **overrides}
    return MagicMock(**merged)


@pytest.fixture(autouse=True)
def _clean_app_state():
    """Reset app.state before each test."""
    for attr in ("scraper", "cache", "settings", "job_service", "cache_maintenance"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


# =========================================================================
# JobService unit tests
# =========================================================================


class TestJobServiceUnit:
    """Direct unit tests of the JobService (no HTTP layer)."""

    def _make_service(
        self,
        max_retained: int = 10,
        max_concurrency: int = 2,
    ) -> JobService:
        settings = _make_settings(
            jobs_max_retained=max_retained,
            jobs_max_concurrency=max_concurrency,
        )
        scraper = AsyncMock()
        metrics = MetricsCollector()
        return JobService(scraper=scraper, settings=settings, metrics=metrics)

    @pytest.mark.asyncio
    async def test_create_job_returns_queued(self):
        svc = self._make_service()
        job = await svc.create_job(
            url="https://example.com",
            scrape_config={"url": "https://example.com", "mode": "http"},
        )
        assert job.status == JobStatus.QUEUED
        assert job.job_id.startswith("job_")
        assert job.url == "https://example.com"

    @pytest.mark.asyncio
    async def test_get_job_by_id(self):
        svc = self._make_service()
        created = await svc.create_job(
            url="https://example.com",
            scrape_config={"url": "https://example.com", "mode": "http"},
        )
        fetched = svc.get_job(created.job_id)
        assert fetched is not None
        assert fetched.job_id == created.job_id

    def test_get_nonexistent_job_returns_none(self):
        svc = self._make_service()
        assert svc.get_job("does_not_exist") is None

    @pytest.mark.asyncio
    async def test_list_jobs_returns_newest_first(self):
        svc = self._make_service()
        await svc.create_job(
            url="https://a.com", scrape_config={"url": "https://a.com", "mode": "http"}
        )
        j2 = await svc.create_job(
            url="https://b.com", scrape_config={"url": "https://b.com", "mode": "http"}
        )
        jobs = svc.list_jobs()
        assert len(jobs) == 2
        assert jobs[0].job_id == j2.job_id  # newest first

    @pytest.mark.asyncio
    async def test_cancel_queued_job(self):
        svc = self._make_service()
        job = await svc.create_job(
            url="https://example.com",
            scrape_config={"url": "https://example.com", "mode": "http"},
        )
        assert job.status == JobStatus.QUEUED
        cancelled = await svc.cancel_job(job.job_id)
        assert cancelled is not None
        assert cancelled.status == JobStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_none(self):
        svc = self._make_service()
        result = await svc.cancel_job("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_job(self):
        svc = self._make_service()
        job = await svc.create_job(
            url="https://example.com",
            scrape_config={"url": "https://example.com", "mode": "http"},
        )
        deleted = await svc.delete_job(job.job_id)
        assert deleted is True
        assert svc.get_job(job.job_id) is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self):
        svc = self._make_service()
        assert await svc.delete_job("nonexistent") is False

    @pytest.mark.asyncio
    async def test_enforces_retention_limit(self):
        """When max_retained is 3 and we create 4 jobs, the oldest finished
        job is evicted."""
        svc = self._make_service(max_retained=3)

        # Create 3 jobs.
        j1 = await svc.create_job(
            url="https://a.com", scrape_config={"url": "https://a.com", "mode": "http"}
        )
        j2 = await svc.create_job(
            url="https://b.com", scrape_config={"url": "https://b.com", "mode": "http"}
        )
        j3 = await svc.create_job(
            url="https://c.com", scrape_config={"url": "https://c.com", "mode": "http"}
        )

        # Mark j1 and j2 as finished so they are eligible for eviction.
        j1.status = JobStatus.SUCCEEDED
        j1.finished_at = datetime.now(UTC) - timedelta(hours=1)
        j2.status = JobStatus.FAILED
        j2.finished_at = datetime.now(UTC) - timedelta(minutes=30)

        # Create 4th job — should evict the oldest finished (j1).
        j4 = await svc.create_job(
            url="https://d.com", scrape_config={"url": "https://d.com", "mode": "http"}
        )
        assert svc.get_job(j1.job_id) is None  # evicted
        assert svc.get_job(j2.job_id) is not None  # still kept (2nd oldest)
        assert svc.get_job(j3.job_id) is not None  # queued, not evictable
        assert svc.get_job(j4.job_id) is not None  # new job
        assert len(svc.list_jobs()) == 3


# =========================================================================
# Integration tests via TestClient
# =========================================================================


class TestJobEndpoints:
    """End-to-end tests using TestClient with a real JobService + mocked scraper."""

    @pytest.fixture(autouse=True)
    def _setup_job_service(self):
        """Create a real JobService with a mocked scraper, start it, and wire
        it into app.state so the routes can use it."""
        settings = _make_settings(jobs_max_retained=10, jobs_max_concurrency=2)
        mock_scraper = AsyncMock()
        mock_scraper.scrape.return_value = SAMPLE_RESULT
        metrics = MetricsCollector()

        svc = JobService(scraper=mock_scraper, settings=settings, metrics=metrics)
        app.state.scraper = mock_scraper
        app.state.settings = settings
        app.state.job_service = svc

        # Store references for test methods.
        self._mock_scraper = mock_scraper
        self._job_service = svc
        self._metrics = metrics

    def _client(self) -> TestClient:
        return TestClient(app)

    # --------------------------------------------------------------- create

    def test_create_job_returns_queued(self):
        response = self._client().post(
            "/v1/jobs",
            json={"url": "https://example.com"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] == "queued"
        assert data["job_id"].startswith("job_")
        assert data["url"] == "https://example.com"
        assert "created_at" in data
        assert "updated_at" in data
        assert "result" not in data or data["result"] is None

    def test_create_job_with_extract_and_normalize(self):
        response = self._client().post(
            "/v1/jobs",
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
        assert data["status"] == "queued"

    # --------------------------------------------------------------- get

    def test_get_job_by_id(self):
        client = self._client()
        create_resp = client.post(
            "/v1/jobs",
            json={"url": "https://example.com"},
            headers=AUTH_HEADER,
        )
        job_id = create_resp.json()["job_id"]

        get_resp = client.get(f"/v1/jobs/{job_id}", headers=AUTH_HEADER)
        assert get_resp.status_code == 200, get_resp.text
        data = get_resp.json()
        assert data["job_id"] == job_id
        assert data["status"] == "queued"

    def test_get_nonexistent_job_returns_404(self):
        response = self._client().get("/v1/jobs/nonexistent", headers=AUTH_HEADER)
        assert response.status_code == 404

    # --------------------------------------------------------------- list

    def test_list_jobs(self):
        client = self._client()
        client.post("/v1/jobs", json={"url": "https://a.com"}, headers=AUTH_HEADER)
        client.post("/v1/jobs", json={"url": "https://b.com"}, headers=AUTH_HEADER)

        response = client.get("/v1/jobs", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 2
        assert len(data["jobs"]) == 2

    def test_list_jobs_empty(self):
        response = self._client().get("/v1/jobs", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 0
        assert data["jobs"] == []

    # --------------------------------------------------------------- cancel

    def test_cancel_queued_job(self):
        client = self._client()
        create_resp = client.post(
            "/v1/jobs",
            json={"url": "https://example.com"},
            headers=AUTH_HEADER,
        )
        job_id = create_resp.json()["job_id"]

        cancel_resp = client.post(
            f"/v1/jobs/{job_id}/cancel",
            headers=AUTH_HEADER,
        )
        assert cancel_resp.status_code == 200, cancel_resp.text
        assert cancel_resp.json()["status"] == "cancelled"

    def test_cancel_nonexistent_returns_404(self):
        response = self._client().post(
            "/v1/jobs/nonexistent/cancel",
            headers=AUTH_HEADER,
        )
        assert response.status_code == 404

    # --------------------------------------------------------------- delete

    def test_delete_job(self):
        client = self._client()
        create_resp = client.post(
            "/v1/jobs",
            json={"url": "https://example.com"},
            headers=AUTH_HEADER,
        )
        job_id = create_resp.json()["job_id"]

        del_resp = client.delete(f"/v1/jobs/{job_id}", headers=AUTH_HEADER)
        assert del_resp.status_code == 200, del_resp.text
        assert del_resp.json()["message"] == f"Job {job_id} deleted"

        # Verify it's gone.
        get_resp = client.get(f"/v1/jobs/{job_id}", headers=AUTH_HEADER)
        assert get_resp.status_code == 404

    def test_delete_nonexistent_returns_404(self):
        response = self._client().delete("/v1/jobs/nonexistent", headers=AUTH_HEADER)
        assert response.status_code == 404

    # --------------------------------------------------------------- auth

    def test_create_job_requires_auth(self):
        response = self._client().post(
            "/v1/jobs",
            json={"url": "https://example.com"},
        )
        assert response.status_code in (401, 403)

    def test_get_job_requires_auth(self):
        response = self._client().get("/v1/jobs/some-id")
        assert response.status_code in (401, 403)

    def test_list_jobs_requires_auth(self):
        response = self._client().get("/v1/jobs")
        assert response.status_code in (401, 403)

    def test_delete_job_requires_auth(self):
        response = self._client().delete("/v1/jobs/some-id")
        assert response.status_code in (401, 403)

    def test_cancel_job_requires_auth(self):
        response = self._client().post("/v1/jobs/some-id/cancel")
        assert response.status_code in (401, 403)

    # --------------------------------------------------------------- successful processing

    @pytest.mark.asyncio
    async def test_successful_job_stores_result(self):
        """Process a job through the worker and verify the result is stored."""
        svc = self._job_service
        await svc.start()

        try:
            job = await svc.create_job(
                url="https://example.com",
                scrape_config={"url": "https://example.com", "mode": "http"},
            )
            # Give the worker a moment to process.
            await asyncio.sleep(0.2)

            fetched = svc.get_job(job.job_id)
            assert fetched is not None
            assert fetched.status == JobStatus.SUCCEEDED
            assert fetched.result is not None
            assert fetched.result["html"] == "<html>Hello World</html>"
            assert fetched.finished_at is not None
        finally:
            await svc.stop()

    @pytest.mark.asyncio
    async def test_failed_job_stores_structured_error(self):
        """Process a job whose scrape raises an error."""
        svc = self._job_service
        mock_scraper = AsyncMock()
        mock_scraper.scrape.side_effect = HttpError("Upstream server returned 500")
        svc._scraper = mock_scraper  # swap scraper
        await svc.start()

        try:
            job = await svc.create_job(
                url="https://error.com",
                scrape_config={"url": "https://error.com", "mode": "http"},
            )
            await asyncio.sleep(0.2)

            fetched = svc.get_job(job.job_id)
            assert fetched is not None
            assert fetched.status == JobStatus.FAILED
            assert fetched.error is not None
            assert fetched.error["error"]["type"] == "http_error"
            assert "500" in fetched.error["error"]["message"]
            assert fetched.finished_at is not None
        finally:
            await svc.stop()
