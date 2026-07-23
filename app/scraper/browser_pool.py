"""Shared browser fetcher pool.

The API creates ``ScraperService`` instances per request/profile, but browser
drivers are expensive OS resources.  This pool lets those short-lived services
reuse a small set of ``BrowserFetcher`` instances keyed by browser settings.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.scraper.browser_fetcher import BrowserFetcher

logger = logging.getLogger("scraper-api.fetcher.browser_pool")


def _freeze(value: Any) -> Any:
    """Convert nested config values into hashable equivalents."""
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


class BrowserFetcherPool:
    """Thread-safe cache of ``BrowserFetcher`` instances."""

    def __init__(self) -> None:
        self._fetchers: dict[Any, BrowserFetcher] = {}
        self._lock = threading.Lock()

    def get(self, config: dict[str, Any]) -> BrowserFetcher:
        """Return a shared fetcher for *config*, creating it lazily."""
        key = _freeze(config)
        with self._lock:
            fetcher = self._fetchers.get(key)
            if fetcher is None:
                fetcher = BrowserFetcher(**config)
                self._fetchers[key] = fetcher
                logger.info("Created shared browser fetcher (pool_size=%d)", len(self._fetchers))
            return fetcher

    def close_all(self) -> None:
        """Close all pooled browser fetchers."""
        with self._lock:
            fetchers = list(self._fetchers.values())
            self._fetchers.clear()

        for fetcher in fetchers:
            try:
                fetcher.close()
            except Exception:
                logger.exception("Error while closing pooled browser fetcher")
