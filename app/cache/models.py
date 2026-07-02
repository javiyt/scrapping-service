"""SQLite row models for the cache backend."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel


@dataclass
class CacheEntry:
    """A single cached scraping result."""

    cache_key: str
    url: str
    final_url: str
    status_code: int
    html: str
    fetched_at: datetime
    expires_at: datetime | None
    mode: str
    content_length: int
    headers: str | None  # JSON-encoded dict
    error_metadata: str | None  # JSON-encoded dict for stale-fallback info

    @property
    def is_expired(self) -> bool:
        """Return ``True`` if the entry is past its ``expires_at`` time."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) > self.expires_at.replace(tzinfo=UTC)

    @property
    def is_stale(self) -> bool:
        """Alias for :meth:`is_expired`."""
        return self.is_expired

    def to_cache_dict(self) -> dict[str, Any]:
        """Return a dict representation suitable for the cache API."""
        return {
            "cache_key": self.cache_key,
            "url": self.url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "html": self.html,
            "fetched_at": self.fetched_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "mode": self.mode,
            "content_length": self.content_length,
        }


class CacheCleanupResult(BaseModel):
    """Result of a cache cleanup operation.

    Reports how many entries were deleted in each phase and the
    state of the cache before and after.
    """

    deleted_expired: int = 0
    deleted_by_max_entries: int = 0
    deleted_by_max_size: int = 0
    total_deleted: int = 0
    size_before_bytes: int = 0
    size_after_bytes: int = 0
    entries_before: int = 0
    entries_after: int = 0
    vacuumed: bool = False


class CacheVacuumResult(BaseModel):
    """Result of a SQLite VACUUM operation."""

    vacuumed: bool = True
    size_before_bytes: int = 0
    size_after_bytes: int = 0
