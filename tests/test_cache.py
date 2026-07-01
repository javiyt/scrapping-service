"""Tests for the SQLite cache backend."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.cache.models import CacheEntry
from app.cache.sqlite_cache import SqliteCache


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


class TestCacheCrud:
    def test_set_and_get(self, cache):
        entry = CacheEntry(
            cache_key="abc123",
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            html="<html>hello</html>",
            fetched_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            mode="http",
            content_length=20,
            headers=None,
            error_metadata=None,
        )
        cache.set(entry)

        retrieved = cache.get("abc123")
        assert retrieved is not None
        assert retrieved.url == "https://example.com"
        assert retrieved.html == "<html>hello</html>"
        assert retrieved.status_code == 200
        assert retrieved.mode == "http"

    def test_get_nonexistent(self, cache):
        result = cache.get("nonexistent")
        assert result is None

    def test_delete(self, cache):
        entry = CacheEntry(
            cache_key="delkey",
            url="https://example.com/del",
            final_url="https://example.com/del",
            status_code=200,
            html="<html>delete me</html>",
            fetched_at=datetime.now(UTC),
            expires_at=None,
            mode="http",
            content_length=21,
            headers=None,
            error_metadata=None,
        )
        cache.set(entry)
        assert cache.get("delkey") is not None
        assert cache.delete("delkey")
        assert cache.get("delkey") is None

    def test_delete_nonexistent(self, cache):
        assert not cache.delete("no-such-key")

    def test_delete_by_url(self, cache):
        entry = CacheEntry(
            cache_key="urlkey",
            url="https://example.com/unique",
            final_url="https://example.com/unique",
            status_code=200,
            html="<html>url</html>",
            fetched_at=datetime.now(UTC),
            expires_at=None,
            mode="http",
            content_length=14,
            headers=None,
            error_metadata=None,
        )
        cache.set(entry)
        assert cache.delete_by_url("https://example.com/unique")
        assert cache.get("urlkey") is None

    def test_exists(self, cache):
        entry = CacheEntry(
            cache_key="existskey",
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            html="<html>exists</html>",
            fetched_at=datetime.now(UTC),
            expires_at=None,
            mode="http",
            content_length=16,
            headers=None,
            error_metadata=None,
        )
        cache.set(entry)
        assert cache.exists("existskey")
        assert not cache.exists("no-such-key")


class TestCacheExpiration:
    def test_expired_entry(self, cache):
        past = datetime.now(UTC) - timedelta(hours=2)
        entry = CacheEntry(
            cache_key="expired1",
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            html="<html>old</html>",
            fetched_at=past,
            expires_at=past,
            mode="http",
            content_length=14,
            headers=None,
            error_metadata=None,
        )
        cache.set(entry)
        assert cache.is_expired("expired1")

    def test_not_expired(self, cache):
        future = datetime.now(UTC) + timedelta(hours=2)
        entry = CacheEntry(
            cache_key="notexpired",
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            html="<html>fresh</html>",
            fetched_at=datetime.now(UTC),
            expires_at=future,
            mode="http",
            content_length=15,
            headers=None,
            error_metadata=None,
        )
        cache.set(entry)
        assert not cache.is_expired("notexpired")

    def test_no_expiry(self, cache):
        entry = CacheEntry(
            cache_key="noexpiry",
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            html="<html>never expires</html>",
            fetched_at=datetime.now(UTC),
            expires_at=None,
            mode="http",
            content_length=23,
            headers=None,
            error_metadata=None,
        )
        cache.set(entry)
        assert not cache.is_expired("noexpiry")


class TestCacheStats:
    def test_empty_stats(self, cache):
        stats = cache.stats()
        assert stats["total_entries"] == 0
        assert stats["total_size_bytes"] == 0
        assert stats["expired_entries"] == 0

    def test_stats_with_entries(self, cache):
        entry = CacheEntry(
            cache_key="stat1",
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            html="<html>a</html>",
            fetched_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            mode="http",
            content_length=12,
            headers=None,
            error_metadata=None,
        )
        cache.set(entry)
        stats = cache.stats()
        assert stats["total_entries"] == 1
        assert stats["total_size_bytes"] == 12

    def test_purge(self, cache):
        for i in range(3):
            cache.set(
                CacheEntry(
                    cache_key=f"purge{i}",
                    url=f"https://example.com/{i}",
                    final_url=f"https://example.com/{i}",
                    status_code=200,
                    html=f"<html>{i}</html>",
                    fetched_at=datetime.now(UTC),
                    expires_at=None,
                    mode="http",
                    content_length=12,
                    headers=None,
                    error_metadata=None,
                )
            )
        assert cache.stats()["total_entries"] == 3
        purged = cache.purge()
        assert purged == 3
        assert cache.stats()["total_entries"] == 0


class TestCacheEntryModel:
    def test_is_expired_property(self):
        past = datetime.now(UTC) - timedelta(hours=2)
        entry = CacheEntry(
            cache_key="k",
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            html="<html>x</html>",
            fetched_at=datetime.now(UTC),
            expires_at=past,
            mode="http",
            content_length=0,
            headers=None,
            error_metadata=None,
        )
        assert entry.is_expired
        assert entry.is_stale

    def test_not_expired(self):
        future = datetime.now(UTC) + timedelta(hours=2)
        entry = CacheEntry(
            cache_key="k",
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            html="<html>x</html>",
            fetched_at=datetime.now(UTC),
            expires_at=future,
            mode="http",
            content_length=0,
            headers=None,
            error_metadata=None,
        )
        assert not entry.is_expired
