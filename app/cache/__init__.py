"""Cache backend package.

Provides a persistent SQLite-backed HTML cache with configurable TTL,
eviction, and maintenance (cleanup / vacuum).
"""

from app.cache.maintenance import CacheMaintenanceService
from app.cache.models import CacheCleanupResult, CacheEntry, CacheVacuumResult
from app.cache.sqlite_cache import SqliteCache

__all__ = [
    "CacheCleanupResult",
    "CacheEntry",
    "CacheMaintenanceService",
    "CacheVacuumResult",
    "SqliteCache",
]
