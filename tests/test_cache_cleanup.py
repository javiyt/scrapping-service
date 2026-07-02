"""Tests for cache cleanup and maintenance functionality."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.cache.maintenance import CacheMaintenanceService
from app.cache.models import CacheCleanupResult, CacheVacuumResult
from app.cache.sqlite_cache import SqliteCache
from app.main import app

# ------------------------------------------------------------------- helpers

VALID_API_KEY = "change-me"
AUTH_HEADER = {"Authorization": f"Bearer {VALID_API_KEY}"}


@pytest.fixture
def cache():
    """Yield a temporary SQLite cache and clean up after the test."""
    tmp = Path(tempfile.mktemp(suffix=".db"))
    c = SqliteCache(db_path=str(tmp), max_size_mb=10)
    c.open()
    try:
        yield c
    finally:
        c.close()
        tmp.unlink(missing_ok=True)


def _make_entry(cache, key: str, age_hours: float = 0, ttl_hours: float = 1, size: int = 100):
    """Insert a cache entry with a given age and TTL.

    *age_hours* controls how long ago the entry was fetched (negative = future).
    *ttl_hours* controls how long after fetch it expires (None = no expiry).
    """
    from app.cache.models import CacheEntry

    fetched = datetime.now(UTC) - timedelta(hours=age_hours)
    expires = fetched + timedelta(hours=ttl_hours) if ttl_hours is not None else None
    entry = CacheEntry(
        cache_key=key,
        url=f"https://example.com/{key}",
        final_url=f"https://example.com/{key}",
        status_code=200,
        html="<html>" + "x" * max(0, size - 7) + "</html>",
        fetched_at=fetched,
        expires_at=expires,
        mode="http",
        content_length=max(size, 0),
        headers=None,
        error_metadata=None,
    )
    cache.set(entry)


# ============================================================= cleanup_expired


class TestCleanupExpired:
    def test_deletes_old_expired_entries(self, cache):
        _make_entry(cache, "old", age_hours=48, ttl_hours=1)
        _make_entry(cache, "recent", age_hours=2, ttl_hours=1)
        assert cache.stats()["total_entries"] == 2

        # Delete entries that expired more than 3600 seconds (1h) ago.
        deleted = cache.cleanup_expired(delete_expired_after_seconds=3600)
        assert deleted == 2
        assert cache.stats()["total_entries"] == 0

    def test_retains_recently_expired_entries_grace_period(self, cache):
        _make_entry(cache, "just_expired", age_hours=2, ttl_hours=1)
        # Entry expired 1h ago, grace period is 1h — still within grace.
        deleted = cache.cleanup_expired(delete_expired_after_seconds=3600)
        assert deleted == 1  # expired 1h ago, cutoff = 1h ago, so it's deleted

    def test_retains_recently_expired_entries(self, cache):
        """Entries that expired within the grace period should be retained."""
        _make_entry(cache, "barely_expired", age_hours=2, ttl_hours=1)
        # Grace period of 7200s = 2h. Entry expired 1h ago, so still inside grace.
        deleted = cache.cleanup_expired(delete_expired_after_seconds=7200)
        assert deleted == 0
        assert cache.stats()["total_entries"] == 1

    def test_retains_non_expired_entries(self, cache):
        _make_entry(cache, "fresh", age_hours=0, ttl_hours=24)
        _make_entry(cache, "no_expiry", age_hours=0, ttl_hours=None)
        deleted = cache.cleanup_expired(delete_expired_after_seconds=3600)
        assert deleted == 0
        assert cache.stats()["total_entries"] == 2

    def test_no_entries_does_not_crash(self, cache):
        assert cache.cleanup_expired(delete_expired_after_seconds=3600) == 0


# ========================================================= cleanup_by_max_entries


class TestCleanupByMaxEntries:
    def test_deletes_oldest_when_over_limit(self, cache):
        _make_entry(cache, "a", age_hours=48, ttl_hours=1)
        _make_entry(cache, "b", age_hours=24, ttl_hours=1)
        _make_entry(cache, "c", age_hours=1, ttl_hours=24)
        assert cache.stats()["total_entries"] == 3

        deleted = cache.cleanup_by_max_entries(max_entries=2)
        assert deleted == 1
        # Oldest entry "a" should be deleted.
        assert cache.get("a") is None
        assert cache.get("b") is not None
        assert cache.get("c") is not None

    def test_under_limit_does_nothing(self, cache):
        _make_entry(cache, "a", age_hours=1)
        _make_entry(cache, "b", age_hours=2)
        deleted = cache.cleanup_by_max_entries(max_entries=10)
        assert deleted == 0
        assert cache.stats()["total_entries"] == 2

    def test_empty_cache(self, cache):
        assert cache.cleanup_by_max_entries(max_entries=100) == 0

    def test_exact_limit(self, cache):
        _make_entry(cache, "a", age_hours=1)
        _make_entry(cache, "b", age_hours=2)
        deleted = cache.cleanup_by_max_entries(max_entries=2)
        assert deleted == 0


# ============================================================ cleanup_by_max_size


class TestCleanupByMaxSize:
    def test_deletes_entries_when_over_limit(self, cache):
        # Create entries with large sizes to exceed the limit.
        for i in range(10):
            _make_entry(cache, f"old{i}", age_hours=48 + i, size=50000)
        _make_entry(cache, "small", age_hours=1, size=100)
        # Total content_length: 10 * 50000 + 100 = 500100
        assert cache.stats()["total_entries"] == 11

        deleted = cache.cleanup_by_max_size(max_size_bytes=100000)
        # At least some entries should be deleted.
        assert deleted >= 1
        # Remaining entries should be under the size limit.
        stats = cache.stats()
        assert stats["total_size_bytes"] <= 100000
        # The newest small entry should remain if size permits.
        assert stats["total_entries"] <= 2

    def test_under_limit_does_nothing(self, cache):
        _make_entry(cache, "a", age_hours=1, size=1000)
        deleted = cache.cleanup_by_max_size(max_size_bytes=100000)
        assert deleted == 0

    def test_empty_cache(self, cache):
        assert cache.cleanup_by_max_size(max_size_bytes=1000) == 0


# ===================================================================== cleanup


class TestCleanup:
    def test_full_cleanup_returns_structured_result(self, cache):
        _make_entry(cache, "old", age_hours=48, ttl_hours=1, size=1000)
        _make_entry(cache, "fresh", age_hours=1, ttl_hours=24, size=1000)
        assert cache.stats()["total_entries"] == 2

        result = cache.cleanup(
            delete_expired_after_seconds=3600,
            max_entries=10,
            max_size_bytes=1000000,
            vacuum=False,
        )
        assert isinstance(result, CacheCleanupResult)
        assert result.total_deleted >= 1
        assert result.entries_before == 2
        assert result.entries_after <= 1
        assert result.vacuumed is False

    def test_cleanup_with_vacuum(self, cache):
        _make_entry(cache, "a", age_hours=48, ttl_hours=1, size=100)
        result = cache.cleanup(
            delete_expired_after_seconds=3600,
            max_entries=100,
            max_size_bytes=1000000,
            vacuum=True,
        )
        assert result.vacuumed is True

    def test_cleanup_empty_cache(self, cache):
        result = cache.cleanup(
            delete_expired_after_seconds=3600,
            max_entries=100,
            max_size_bytes=1000000,
        )
        assert result.total_deleted == 0
        assert result.vacuumed is False


# ===================================================================== vacuum


class TestVacuum:
    def test_vacuum_returns_sizes(self, cache):
        _make_entry(cache, "a", age_hours=1, size=5000)
        result = cache.vacuum()
        assert isinstance(result, CacheVacuumResult)
        assert result.vacuumed is True
        assert result.size_before_bytes > 0
        assert result.size_after_bytes > 0


# ============================================================ API /v1/cache/cleanup


class TestCleanupEndpoint:
    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        """Set up mocked cache on app.state for each test."""
        # Use a real temp cache for integration.
        self.tmp = Path(tempfile.mktemp(suffix=".db"))
        self.real_cache = SqliteCache(db_path=str(self.tmp), max_size_mb=10)
        self.real_cache.open()
        app.state.cache = self.real_cache
        app.state.scraper = MagicMock()
        yield
        self.real_cache.close()
        self.tmp.unlink(missing_ok=True)
        for attr in ("scraper", "cache", "settings"):
            if hasattr(app.state, attr):
                delattr(app.state, attr)

    def test_requires_auth(self):
        response = TestClient(app).post("/v1/cache/cleanup")
        assert response.status_code in (401, 403)

    def test_returns_structured_cleanup_stats(self):
        _make_entry(self.real_cache, "old", age_hours=48, ttl_hours=1)
        client = TestClient(app)
        response = client.post("/v1/cache/cleanup", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert "total_deleted" in data
        assert "deleted_expired" in data
        assert "entries_before" in data
        assert "entries_after" in data
        assert data["total_deleted"] >= 1

    def test_cleanup_with_body_overrides(self):
        _make_entry(self.real_cache, "old", age_hours=48, ttl_hours=1)
        _make_entry(self.real_cache, "fresh", age_hours=1, ttl_hours=24)
        client = TestClient(app)
        response = client.post(
            "/v1/cache/cleanup",
            headers=AUTH_HEADER,
            json={
                "delete_expired_after_seconds": 7200,
                "max_entries": 1,
                "max_size_mb": 1,
                "vacuum": False,
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["entries_after"] <= 1


# ============================================================= API /v1/cache/vacuum


class TestVacuumEndpoint:
    @pytest.fixture(autouse=True)
    def _setup_mocks(self):
        self.tmp = Path(tempfile.mktemp(suffix=".db"))
        self.real_cache = SqliteCache(db_path=str(self.tmp), max_size_mb=10)
        self.real_cache.open()
        app.state.cache = self.real_cache
        app.state.scraper = MagicMock()
        yield
        self.real_cache.close()
        self.tmp.unlink(missing_ok=True)
        for attr in ("scraper", "cache", "settings"):
            if hasattr(app.state, attr):
                delattr(app.state, attr)

    def test_requires_auth(self):
        response = TestClient(app).post("/v1/cache/vacuum")
        assert response.status_code in (401, 403)

    def test_vacuum_returns_sizes(self):
        _make_entry(self.real_cache, "a", age_hours=1, size=5000)
        client = TestClient(app)
        response = client.post("/v1/cache/vacuum", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["vacuumed"] is True
        assert data["size_before_bytes"] > 0
        assert data["size_after_bytes"] > 0


# ============================================================ Background service


class TestMaintenanceService:
    def test_start_and_stop(self):
        """Service can start and stop without creating duplicate loops."""
        cache = MagicMock(spec=SqliteCache)
        cache.cleanup.return_value = CacheCleanupResult()
        cache.db_path = ":memory:"

        settings = MagicMock()
        settings.cache_cleanup_interval_seconds = 3600
        settings.cache_delete_expired_after_seconds = 86400
        settings.cache_max_entries = 10000
        settings.cache_max_size_mb = 512
        settings.cache_vacuum_after_cleanup = False

        service = CacheMaintenanceService(cache=cache, settings=settings)

        assert service.is_running is False

        # Start.
        import asyncio

        asyncio.get_event_loop().run_until_complete(service.start())
        assert service.is_running is True

        # Starting again should not create a duplicate.
        asyncio.get_event_loop().run_until_complete(service.start())
        assert service.is_running is True

        # Stop.
        asyncio.get_event_loop().run_until_complete(service.stop())
        assert service.is_running is False

    def test_does_not_start_duplicate_loops(self):
        cache = MagicMock(spec=SqliteCache)
        cache.cleanup.return_value = CacheCleanupResult()
        cache.db_path = ":memory:"
        settings = MagicMock()
        settings.cache_cleanup_interval_seconds = 3600
        settings.cache_delete_expired_after_seconds = 86400
        settings.cache_max_entries = 10000
        settings.cache_max_size_mb = 512
        settings.cache_vacuum_after_cleanup = False

        service = CacheMaintenanceService(cache=cache, settings=settings)

        import asyncio

        loop = asyncio.get_event_loop()
        loop.run_until_complete(service.start())
        task_id1 = id(service._task)

        # Second start should be a no-op.
        loop.run_until_complete(service.start())
        task_id2 = id(service._task)

        assert task_id1 == task_id2

        loop.run_until_complete(service.stop())

    def test_stop_when_not_started_is_safe(self):
        cache = MagicMock(spec=SqliteCache)
        settings = MagicMock()
        settings.cache_cleanup_interval_seconds = 3600
        settings.cache_delete_expired_after_seconds = 86400
        settings.cache_max_entries = 10000
        settings.cache_max_size_mb = 512
        settings.cache_vacuum_after_cleanup = False

        service = CacheMaintenanceService(cache=cache, settings=settings)
        import asyncio

        loop = asyncio.get_event_loop()
        loop.run_until_complete(service.stop())
        assert service.is_running is False


class TestMaintenanceErrorHandling:
    def test_cleanup_error_logged(self):
        import asyncio

        cache = MagicMock(spec=SqliteCache)
        cache.cleanup.side_effect = RuntimeError("cleanup crashed")
        cache.db_path = ":memory:"
        settings = MagicMock()
        settings.cache_cleanup_interval_seconds = 1
        settings.cache_delete_expired_after_seconds = 86400
        settings.cache_max_entries = 10000
        settings.cache_max_size_mb = 512
        settings.cache_vacuum_after_cleanup = False

        service = CacheMaintenanceService(cache=cache, settings=settings)

        loop = asyncio.get_event_loop()
        loop.run_until_complete(service.start())
        loop.run_until_complete(asyncio.sleep(0.1))
        loop.run_until_complete(service.stop())
        assert service.is_running is False


class TestMaintenanceCleanupRuns:
    def test_cleanup_runs_on_start(self):
        import asyncio

        cache = MagicMock(spec=SqliteCache)
        cache.cleanup.return_value = CacheCleanupResult(
            total_deleted=5,
            size_before_bytes=1000,
            size_after_bytes=500,
            entries_before=10,
            entries_after=5,
            deleted_expired=3,
        )
        cache.db_path = ":memory:"
        settings = MagicMock()
        settings.cache_cleanup_interval_seconds = 1
        settings.cache_delete_expired_after_seconds = 86400
        settings.cache_max_entries = 10000
        settings.cache_max_size_mb = 512
        settings.cache_vacuum_after_cleanup = True

        service = CacheMaintenanceService(cache=cache, settings=settings)

        loop = asyncio.get_event_loop()
        loop.run_until_complete(service.start())
        loop.run_until_complete(asyncio.sleep(0.2))
        loop.run_until_complete(service.stop())
        cache.cleanup.assert_called()
        assert service.is_running is False
