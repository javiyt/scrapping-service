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


class TestCacheEdgeCases:
    def test_open_twice_is_noop(self, cache):
        cache.open()
        assert cache._conn is not None

    def test_close_after_open(self, cache):
        cache.close()
        assert cache._conn is None

    def test_delete_by_url_no_match(self, cache):
        assert not cache.delete_by_url("https://nonexistent.com")

    def test_purge_with_domain(self, cache):
        from datetime import UTC, datetime, timedelta

        entry = CacheEntry(
            cache_key="dom1",
            url="https://example.com/page",
            final_url="https://example.com/page",
            status_code=200,
            html="<html>domain test</html>",
            fetched_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            mode="http",
            content_length=22,
            headers=None,
            error_metadata=None,
        )
        cache.set(entry)
        purged = cache.purge(domain="example.com")
        assert purged >= 1

    def test_is_expired_nonexistent_key(self, cache):
        assert cache.is_expired("no-such-key") is True

    def test_delete_by_url_no_match_returns_false(self, cache):
        assert cache.delete_by_url("https://does-not-exist.com") is False

    def test_delete_returns_false_for_missing(self, cache):
        assert cache.delete("no-such-key") is False

    def test_stats_path_in_result(self, cache):
        stats = cache.stats()
        assert "cache_path" in stats

    def test_to_cache_dict(self):
        from datetime import UTC, datetime, timedelta

        entry = CacheEntry(
            cache_key="k",
            url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            html="<html>test</html>",
            fetched_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            mode="http",
            content_length=14,
            headers=None,
            error_metadata=None,
        )
        d = entry.to_cache_dict()
        assert d["cache_key"] == "k"
        assert d["url"] == "https://example.com"
        assert d["status_code"] == 200
        assert "fetched_at" in d
        assert "expires_at" in d


class TestCacheCursorErrors:
    def test_cursor_raises_when_not_opened(self):
        with pytest.raises(RuntimeError, match="not opened"):
            c = SqliteCache(db_path=":memory:", max_size_mb=10)
            with c._cursor():
                pass

    def test_cursor_rollback_on_exception(self, cache):
        """When an exception occurs inside a _cursor block, it should rollback."""
        from datetime import UTC, datetime

        with pytest.raises(ValueError, match="intentional"):
            with cache._cursor() as cur:
                cur.execute(
                    """INSERT INTO cache
                       (cache_key, url, status_code, fetched_at, mode, content_length)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        "rollback_test",
                        "https://example.com",
                        200,
                        datetime.now(UTC).isoformat(),
                        "http",
                        0,
                    ),
                )
                raise ValueError("intentional rollback test")
        # After rollback, the entry should not exist.
        assert cache.get("rollback_test") is None


class TestCacheEviction:
    def test_eviction_removes_entries_when_over_limit(self):
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mktemp(suffix=".db"))
        c = SqliteCache(db_path=str(tmp), max_size_mb=1)
        c.open()
        try:
            large_html = "<html>" + "x" * 900000 + "</html>"
            entry1 = CacheEntry(
                cache_key="large1",
                url="https://example.com/large1",
                final_url="https://example.com/large1",
                status_code=200,
                html=large_html,
                fetched_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) - timedelta(hours=2),
                mode="http",
                content_length=len(large_html),
                headers=None,
                error_metadata=None,
            )
            c.set(entry1)
            entry2 = CacheEntry(
                cache_key="large2",
                url="https://example.com/large2",
                final_url="https://example.com/large2",
                status_code=200,
                html=large_html,
                fetched_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=2),
                mode="http",
                content_length=len(large_html),
                headers=None,
                error_metadata=None,
            )
            c.set(entry2)
            stats = c.stats()
            assert stats["total_entries"] <= 1
        finally:
            c.close()
            tmp.unlink(missing_ok=True)

    def test_eviction_not_triggered_under_limit(self):
        import tempfile
        from pathlib import Path

        tmp = Path(tempfile.mktemp(suffix=".db"))
        c = SqliteCache(db_path=str(tmp), max_size_mb=10)
        c.open()
        try:
            entry = CacheEntry(
                cache_key="small",
                url="https://example.com",
                final_url="https://example.com",
                status_code=200,
                html="<html>small</html>",
                fetched_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                mode="http",
                content_length=16,
                headers=None,
                error_metadata=None,
            )
            c.set(entry)
            stats = c.stats()
            assert stats["total_entries"] == 1
        finally:
            c.close()
            tmp.unlink(missing_ok=True)
