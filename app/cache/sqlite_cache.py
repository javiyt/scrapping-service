"""SQLite-based persistent HTML cache.

Supports WAL mode for concurrent reads, expiration-based eviction,
and configurable table size limits.
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.cache.models import CacheCleanupResult, CacheEntry, CacheVacuumResult

logger = logging.getLogger("scraper-api.cache")

# Schema for the cache table.
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cache (
    cache_key        TEXT PRIMARY KEY,
    url              TEXT NOT NULL,
    final_url        TEXT NOT NULL DEFAULT '',
    status_code      INTEGER NOT NULL DEFAULT 0,
    html             TEXT NOT NULL DEFAULT '',
    fetched_at       TEXT NOT NULL,
    expires_at       TEXT,
    mode             TEXT NOT NULL DEFAULT 'http',
    content_length   INTEGER NOT NULL DEFAULT 0,
    headers          TEXT,
    error_metadata   TEXT
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at);
"""

# Schema for the search result cache table.  Search entries are logically
# separated from scrape entries: they live in their own table (own key
# space, own size budget, own cleanup phases) inside the same database
# file, so one connection and one maintenance loop serve both caches.
CREATE_SEARCH_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS search_cache (
    cache_key        TEXT PRIMARY KEY,
    request          TEXT NOT NULL,
    response         TEXT NOT NULL,
    fetched_at       TEXT NOT NULL,
    expires_at       TEXT,
    content_length   INTEGER NOT NULL DEFAULT 0
);
"""

CREATE_SEARCH_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_search_cache_expires ON search_cache(expires_at);
"""

# Schema for the transcript result cache table.  Transcript entries are
# logically separated from scrape and search entries: they live in their own
# table (own key space, own size budget, own cleanup phases) inside the same
# database file, so one connection and one maintenance loop serve all
# caches.
CREATE_TRANSCRIPT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS transcript_cache (
    cache_key        TEXT PRIMARY KEY,
    request          TEXT NOT NULL,
    response         TEXT NOT NULL,
    fetched_at       TEXT NOT NULL,
    expires_at       TEXT,
    content_length   INTEGER NOT NULL DEFAULT 0
);
"""

CREATE_TRANSCRIPT_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_transcript_cache_expires ON transcript_cache(expires_at);
"""

# Schema for the crawl job cache table.  Crawl jobs persist their state and
# results in this table, enabling cancel/resume and across-restart durability.
CREATE_CRAWL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS crawl_jobs (
    job_id           TEXT PRIMARY KEY,
    state_json       TEXT NOT NULL,
    summary_json     TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
"""

CREATE_CRAWL_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_crawl_jobs_updated ON crawl_jobs(updated_at);
"""


class SqliteCache:
    """Persistent HTML cache backed by SQLite.

    Thread-safe for reads via WAL mode.  Writes are serialised by SQLite's
    built-in locking.
    """

    def __init__(self, db_path: str, max_size_mb: int = 10) -> None:
        self.db_path = db_path
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self._ensure_dir()
        self._conn: sqlite3.Connection | None = None

    # --------------------------------------------------------------- lifecycle

    def _ensure_dir(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def open(self) -> None:
        """Open (or create) the database and ensure the schema exists."""
        if self._conn is not None:
            return
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute(CREATE_TABLE_SQL)
        self._conn.execute(CREATE_INDEX_SQL)
        self._conn.execute(CREATE_SEARCH_TABLE_SQL)
        self._conn.execute(CREATE_SEARCH_INDEX_SQL)
        self._conn.execute(CREATE_TRANSCRIPT_TABLE_SQL)
        self._conn.execute(CREATE_TRANSCRIPT_INDEX_SQL)
        self._conn.execute(CREATE_CRAWL_TABLE_SQL)
        self._conn.execute(CREATE_CRAWL_INDEX_SQL)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # --------------------------------------------------------------- internal

    @contextmanager
    def _cursor(self):
        """Yield a cursor, committing on success."""
        if self._conn is None:
            raise RuntimeError("Cache not opened — call open() first")
        cursor = self._conn.cursor()
        try:
            yield cursor
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cursor.close()

    # ------------------------------------------------------------------ CRUD

    def get(self, cache_key: str) -> CacheEntry | None:
        """Retrieve a cache entry by its key, or ``None``."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM cache WHERE cache_key = ?",
                (cache_key,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    def set(self, entry: CacheEntry) -> None:
        """Insert or replace a cache entry."""
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO cache
                   (cache_key, url, final_url, status_code, html,
                    fetched_at, expires_at, mode, content_length,
                    headers, error_metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.cache_key,
                    entry.url,
                    entry.final_url,
                    entry.status_code,
                    entry.html,
                    entry.fetched_at.isoformat(),
                    entry.expires_at.isoformat() if entry.expires_at else None,
                    entry.mode,
                    entry.content_length,
                    entry.headers or None,
                    entry.error_metadata or None,
                ),
            )
        self._maybe_evict()

    def delete(self, cache_key: str) -> bool:
        """Remove one entry.  Returns ``True`` if something was deleted."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM cache WHERE cache_key = ?", (cache_key,))
            return cur.rowcount > 0

    def delete_by_url(self, url: str) -> bool:
        """Remove entries whose URL matches exactly."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM cache WHERE url = ?", (url,))
            return cur.rowcount > 0

    def purge(self, domain: str | None = None) -> int:
        """Remove all (or domain-scoped) cache entries.  Returns count."""
        with self._cursor() as cur:
            if domain:
                cur.execute("DELETE FROM cache WHERE url LIKE ?", (f"%{domain}%",))
            else:
                cur.execute("DELETE FROM cache")
            return cur.rowcount

    def stats(self) -> dict[str, Any]:
        """Return cache statistics (scrape entries plus the search table)."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(content_length), 0) AS total_bytes FROM cache"
            )
            row = cur.fetchone()
            total = row["cnt"]
            total_bytes = row["total_bytes"]

            now = datetime.now(UTC).isoformat()
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            expired = cur.fetchone()["cnt"]

        stats = {
            "total_entries": total,
            "total_size_bytes": total_bytes,
            "expired_entries": expired,
            "cache_path": self.db_path,
        }
        stats.update(self.search_stats())
        stats.update(self.transcript_stats())
        return stats

    # ----------------------------------------------- search result cache table
    #
    # The search_cache table stores normalized search responses (JSON) for
    # the provider-agnostic search domain.  These methods deal in raw rows
    # (primitives only); the search layer (``app.search.cache``) maps them
    # onto typed domain models.  Scraping and search entries never mix.

    def get_search_entry(self, cache_key: str) -> dict[str, Any] | None:
        """Retrieve a raw search_cache row by key, or ``None``.

        Timestamps are ISO-8601 strings; ``request`` and ``response`` are
        JSON strings (canonical key material and the normalized response).
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM search_cache WHERE cache_key = ?",
                (cache_key,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def set_search_entry(
        self,
        *,
        cache_key: str,
        request: str,
        response: str,
        fetched_at: datetime,
        expires_at: datetime | None,
        content_length: int,
    ) -> None:
        """Insert or replace one search_cache row."""
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO search_cache
                   (cache_key, request, response, fetched_at, expires_at,
                    content_length)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    cache_key,
                    request,
                    response,
                    fetched_at.isoformat(),
                    expires_at.isoformat() if expires_at else None,
                    content_length,
                ),
            )

    def delete_search_entry(self, cache_key: str) -> bool:
        """Remove one search_cache entry.  Returns ``True`` if deleted."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM search_cache WHERE cache_key = ?", (cache_key,))
            return cur.rowcount > 0

    def purge_search(self) -> int:
        """Remove all search_cache entries.  Returns count."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM search_cache")
            return cur.rowcount

    def search_stats(self) -> dict[str, Any]:
        """Return statistics for the search result cache table only."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(content_length), 0) AS total_bytes "
                "FROM search_cache"
            )
            row = cur.fetchone()

            now = datetime.now(UTC).isoformat()
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM search_cache "
                "WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            expired = cur.fetchone()["cnt"]

        return {
            "search_total_entries": row["cnt"],
            "search_total_size_bytes": row["total_bytes"],
            "search_expired_entries": expired,
        }

    # ------------------------------------------------ transcript result cache table
    #
    # The transcript_cache table stores normalized transcript responses
    # (JSON) for the provider-agnostic transcript domain.  These methods
    # deal in raw rows (primitives only); the transcript layer
    # (``app.transcript.cache``) maps them onto typed domain models.  Scrape,
    # search and transcript entries never mix.

    def get_transcript_entry(self, cache_key: str) -> dict[str, Any] | None:
        """Retrieve a raw transcript_cache row by key, or ``None``.

        Timestamps are ISO-8601 strings; ``request`` and ``response`` are
        JSON strings (canonical key material and the normalized response).
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM transcript_cache WHERE cache_key = ?",
                (cache_key,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def set_transcript_entry(
        self,
        *,
        cache_key: str,
        request: str,
        response: str,
        fetched_at: datetime,
        expires_at: datetime | None,
        content_length: int,
    ) -> None:
        """Insert or replace one transcript_cache row."""
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO transcript_cache
                   (cache_key, request, response, fetched_at, expires_at,
                    content_length)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    cache_key,
                    request,
                    response,
                    fetched_at.isoformat(),
                    expires_at.isoformat() if expires_at else None,
                    content_length,
                ),
            )

    def delete_transcript_entry(self, cache_key: str) -> bool:
        """Remove one transcript_cache entry.  Returns ``True`` if deleted."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM transcript_cache WHERE cache_key = ?", (cache_key,))
            return cur.rowcount > 0

    def purge_transcript(self) -> int:
        """Remove all transcript_cache entries.  Returns count."""
        with self._cursor() as cur:
            cur.execute("DELETE FROM transcript_cache")
            return cur.rowcount

    def transcript_stats(self) -> dict[str, Any]:
        """Return statistics for the transcript result cache table only."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(content_length), 0) AS total_bytes "
                "FROM transcript_cache"
            )
            row = cur.fetchone()

            now = datetime.now(UTC).isoformat()
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM transcript_cache "
                "WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            )
            expired = cur.fetchone()["cnt"]

        return {
            "transcript_total_entries": row["cnt"],
            "transcript_total_size_bytes": row["total_bytes"],
            "transcript_expired_entries": expired,
        }

    def exists(self, cache_key: str) -> bool:
        """Check if a key exists without loading content."""
        with self._cursor() as cur:
            cur.execute("SELECT 1 FROM cache WHERE cache_key = ?", (cache_key,))
            return cur.fetchone() is not None

    def is_expired(self, cache_key: str) -> bool:
        """Check if a key exists but is past its expiration time."""
        with self._cursor() as cur:
            cur.execute(
                "SELECT expires_at FROM cache WHERE cache_key = ?",
                (cache_key,),
            )
            row = cur.fetchone()
        if row is None:
            return True  # doesn't exist → treat as expired
        expires = row["expires_at"]
        if expires is None:
            return False  # no expiration → never expires
        return datetime.now(UTC).isoformat() > expires

    # --------------------------------------------------------------- eviction

    def _maybe_evict(self) -> None:
        """If total size exceeds the limit, remove oldest expired entries first."""
        with self._cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(content_length), 0) FROM cache")
            total = cur.fetchone()[0]
            if total <= self.max_size_bytes:
                return

            target = total - int(self.max_size_bytes * 0.8)
            now = datetime.now(UTC).isoformat()
            cur.execute(
                """DELETE FROM cache
                   WHERE expires_at IS NOT NULL AND expires_at < ?
                   ORDER BY expires_at ASC
                   LIMIT CASE WHEN ? > 0 THEN ? ELSE 0 END""",
                (now, target, target),
            )
            if cur.rowcount > 0:
                logger.info("Evicted %d expired entries (cache size > limit)", cur.rowcount)

            # If still over limit, evict LRU-style (oldest fetched_at)
            cur.execute("SELECT COALESCE(SUM(content_length), 0) FROM cache")
            total = cur.fetchone()[0]
            if total > self.max_size_bytes:
                excess = total - self.max_size_bytes
                cur.execute(
                    """DELETE FROM cache
                       WHERE cache_key IN (
                           SELECT cache_key FROM cache
                           ORDER BY fetched_at ASC
                           LIMIT ?
                       )""",
                    (max(1, excess // (self.max_size_bytes // 100 + 1) + 1),),
                )
                logger.info("Evicted %d additional entries to meet size limit", cur.rowcount)

    # --------------------------------------------------------------- cleanup

    def cleanup_expired(self, delete_expired_after_seconds: int) -> int:
        """Delete entries that expired more than *delete_expired_after_seconds* ago.

        This provides a grace period: entries whose ``expires_at`` is within
        the window are retained.
        """
        cutoff = (datetime.now(UTC) - timedelta(seconds=delete_expired_after_seconds)).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                (cutoff,),
            )
            deleted = cur.rowcount
        if deleted:
            logger.info("Cleanup expired deleted %d entries (cutoff=%s)", deleted, cutoff)
        return deleted

    def cleanup_by_max_entries(self, max_entries: int) -> int:
        """If total entries exceed *max_entries*, delete the oldest entries."""
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM cache")
            total = cur.fetchone()[0]
            if total <= max_entries:
                return 0
            to_delete = total - max_entries
            cur.execute(
                """DELETE FROM cache WHERE cache_key IN (
                       SELECT cache_key FROM cache ORDER BY fetched_at ASC LIMIT ?
                   )""",
                (to_delete,),
            )
            deleted = cur.rowcount
        if deleted:
            logger.info("Cleanup max_entries deleted %d entries", deleted)
        return deleted

    def cleanup_by_max_size(self, max_size_bytes: int) -> int:
        """Delete oldest entries in batches until total content length is under limit.

        Uses ``SUM(content_length)`` as a proxy for database size since the
        actual SQLite file does not shrink when rows are deleted.
        The batch size is calculated from the excess so that each iteration
        deletes a reasonable number of rows without overshooting.
        """
        total_deleted = 0
        while True:
            with self._cursor() as cur:
                cur.execute("SELECT COALESCE(SUM(content_length), 0), COUNT(*) FROM cache")
                row = cur.fetchone()
                total_size = row[0]
                count = row[1]
                if total_size <= max_size_bytes or count == 0:
                    break
                excess = total_size - max_size_bytes
                avg_size = max(1, total_size // count)
                batch = min(100, max(1, excess // avg_size + 1))
                cur.execute(
                    """DELETE FROM cache WHERE cache_key IN (
                           SELECT cache_key FROM cache ORDER BY fetched_at ASC LIMIT ?
                       )""",
                    (batch,),
                )
                if cur.rowcount == 0:
                    break
                total_deleted += cur.rowcount
        if total_deleted:
            logger.info("Cleanup max_size deleted %d entries", total_deleted)
        return total_deleted

    # Search-cache cleanup phases.  Same semantics as the scrape variants
    # above, applied to the search_cache table, so one cleanup() call keeps
    # both tables within budget.

    def cleanup_expired_search(self, delete_expired_after_seconds: int) -> int:
        """Delete search entries that expired more than *delete_expired_after_seconds* ago."""
        cutoff = (datetime.now(UTC) - timedelta(seconds=delete_expired_after_seconds)).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM search_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                (cutoff,),
            )
            deleted = cur.rowcount
        if deleted:
            logger.info("Cleanup expired deleted %d search entries (cutoff=%s)", deleted, cutoff)
        return deleted

    def cleanup_by_max_entries_search(self, max_entries: int) -> int:
        """If search entries exceed *max_entries*, delete the oldest entries."""
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM search_cache")
            total = cur.fetchone()[0]
            if total <= max_entries:
                return 0
            to_delete = total - max_entries
            cur.execute(
                """DELETE FROM search_cache WHERE cache_key IN (
                       SELECT cache_key FROM search_cache ORDER BY fetched_at ASC LIMIT ?
                   )""",
                (to_delete,),
            )
            deleted = cur.rowcount
        if deleted:
            logger.info("Cleanup max_entries deleted %d search entries", deleted)
        return deleted

    def cleanup_by_max_size_search(self, max_size_bytes: int) -> int:
        """Delete oldest search entries in batches until content length is under the limit."""
        total_deleted = 0
        while True:
            with self._cursor() as cur:
                cur.execute("SELECT COALESCE(SUM(content_length), 0), COUNT(*) FROM search_cache")
                row = cur.fetchone()
                total_size = row[0]
                count = row[1]
                if total_size <= max_size_bytes or count == 0:
                    break
                excess = total_size - max_size_bytes
                avg_size = max(1, total_size // count)
                batch = min(100, max(1, excess // avg_size + 1))
                cur.execute(
                    """DELETE FROM search_cache WHERE cache_key IN (
                           SELECT cache_key FROM search_cache ORDER BY fetched_at ASC LIMIT ?
                       )""",
                    (batch,),
                )
                if cur.rowcount == 0:
                    break
                total_deleted += cur.rowcount
        if total_deleted:
            logger.info("Cleanup max_size deleted %d search entries", total_deleted)
        return total_deleted

    # Transcript-cache cleanup phases.  Same semantics as the search
    # variants above, applied to the transcript_cache table, so one
    # cleanup() call keeps every table within budget.

    def cleanup_expired_transcript(self, delete_expired_after_seconds: int) -> int:
        """Delete transcript entries that expired more than *delete_expired_after_seconds* ago."""
        cutoff = (datetime.now(UTC) - timedelta(seconds=delete_expired_after_seconds)).isoformat()
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM transcript_cache WHERE expires_at IS NOT NULL AND expires_at < ?",
                (cutoff,),
            )
            deleted = cur.rowcount
        if deleted:
            logger.info(
                "Cleanup expired deleted %d transcript entries (cutoff=%s)", deleted, cutoff
            )
        return deleted

    def cleanup_by_max_entries_transcript(self, max_entries: int) -> int:
        """If transcript entries exceed *max_entries*, delete the oldest entries."""
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM transcript_cache")
            total = cur.fetchone()[0]
            if total <= max_entries:
                return 0
            to_delete = total - max_entries
            cur.execute(
                """DELETE FROM transcript_cache WHERE cache_key IN (
                       SELECT cache_key FROM transcript_cache ORDER BY fetched_at ASC LIMIT ?
                   )""",
                (to_delete,),
            )
            deleted = cur.rowcount
        if deleted:
            logger.info("Cleanup max_entries deleted %d transcript entries", deleted)
        return deleted

    def cleanup_by_max_size_transcript(self, max_size_bytes: int) -> int:
        """Delete oldest transcript entries in batches until content length is under the limit."""
        total_deleted = 0
        while True:
            with self._cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM(content_length), 0), COUNT(*) FROM transcript_cache"
                )
                row = cur.fetchone()
                total_size = row[0]
                count = row[1]
                if total_size <= max_size_bytes or count == 0:
                    break
                excess = total_size - max_size_bytes
                avg_size = max(1, total_size // count)
                batch = min(100, max(1, excess // avg_size + 1))
                cur.execute(
                    """DELETE FROM transcript_cache WHERE cache_key IN (
                           SELECT cache_key FROM transcript_cache ORDER BY fetched_at ASC LIMIT ?
                       )""",
                    (batch,),
                )
                if cur.rowcount == 0:
                    break
                total_deleted += cur.rowcount
        if total_deleted:
            logger.info("Cleanup max_size deleted %d transcript entries", total_deleted)
        return total_deleted

    def cleanup(
        self,
        delete_expired_after_seconds: int = 86400,
        max_entries: int = 10000,
        max_size_bytes: int = 512 * 1024 * 1024,
        vacuum: bool = False,
    ) -> CacheCleanupResult:
        """Run all cleanup phases on both tables and return a structured result.

        1. Delete entries past the expiry grace period.
        2. If entries exceed *max_entries*, delete the oldest.
        3. If content size exceeds *max_size_bytes*, delete in batches.
        4. Optionally run VACUUM.

        Each phase runs against the scrape table (``cache``), the search
        table (``search_cache``) and the transcript table
        (``transcript_cache``) with the same limits.  Scrape-only counters
        are reported in the base fields; search-table counters in the
        ``search_*`` fields; transcript-table counters in the
        ``transcript_*`` fields (additive — existing consumers unaffected).
        """
        file_exists = os.path.exists(self.db_path)
        size_before = os.path.getsize(self.db_path) if file_exists else 0

        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM cache")
            entries_before = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM search_cache")
            search_entries_before = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM transcript_cache")
            transcript_entries_before = cur.fetchone()[0]

        deleted_expired = self.cleanup_expired(delete_expired_after_seconds)
        deleted_by_max_entries = self.cleanup_by_max_entries(max_entries)
        deleted_by_max_size = self.cleanup_by_max_size(max_size_bytes)

        search_deleted_expired = self.cleanup_expired_search(delete_expired_after_seconds)
        search_deleted_by_max_entries = self.cleanup_by_max_entries_search(max_entries)
        search_deleted_by_max_size = self.cleanup_by_max_size_search(max_size_bytes)

        transcript_deleted_expired = self.cleanup_expired_transcript(delete_expired_after_seconds)
        transcript_deleted_by_max_entries = self.cleanup_by_max_entries_transcript(max_entries)
        transcript_deleted_by_max_size = self.cleanup_by_max_size_transcript(max_size_bytes)

        vacuumed = False
        if vacuum and file_exists:
            self.vacuum()
            vacuumed = True

        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM cache")
            entries_after = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM search_cache")
            search_entries_after = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM transcript_cache")
            transcript_entries_after = cur.fetchone()[0]

        size_after = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        return CacheCleanupResult(
            deleted_expired=deleted_expired,
            deleted_by_max_entries=deleted_by_max_entries,
            deleted_by_max_size=deleted_by_max_size,
            total_deleted=deleted_expired + deleted_by_max_entries + deleted_by_max_size,
            size_before_bytes=size_before,
            size_after_bytes=size_after,
            entries_before=entries_before,
            entries_after=entries_after,
            vacuumed=vacuumed,
            search_deleted_expired=search_deleted_expired,
            search_deleted_by_max_entries=search_deleted_by_max_entries,
            search_deleted_by_max_size=search_deleted_by_max_size,
            search_total_deleted=(
                search_deleted_expired + search_deleted_by_max_entries + search_deleted_by_max_size
            ),
            search_entries_before=search_entries_before,
            search_entries_after=search_entries_after,
            transcript_deleted_expired=transcript_deleted_expired,
            transcript_deleted_by_max_entries=transcript_deleted_by_max_entries,
            transcript_deleted_by_max_size=transcript_deleted_by_max_size,
            transcript_total_deleted=(
                transcript_deleted_expired
                + transcript_deleted_by_max_entries
                + transcript_deleted_by_max_size
            ),
            transcript_entries_before=transcript_entries_before,
            transcript_entries_after=transcript_entries_after,
        )

    def vacuum(self) -> CacheVacuumResult:
        """Run SQLite VACUUM to reclaim disk space.

        Returns the file size before and after.
        """
        size_before = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        with self._cursor() as cur:
            cur.execute("VACUUM")
        size_after = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        logger.info("VACUUM completed: %d -> %d bytes", size_before, size_after)
        return CacheVacuumResult(
            vacuumed=True,
            size_before_bytes=size_before,
            size_after_bytes=size_after,
        )

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> CacheEntry:
        return CacheEntry(
            cache_key=row["cache_key"],
            url=row["url"],
            final_url=row["final_url"],
            status_code=row["status_code"],
            html=row["html"],
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            mode=row["mode"],
            content_length=row["content_length"],
            headers=row["headers"],
            error_metadata=row["error_metadata"],
        )
