"""Shared browser fetcher pool.

The API creates ``ScraperService`` instances per request/profile, but browser
drivers are expensive OS resources.  This pool lets those short-lived services
reuse a small set of ``BrowserFetcher`` instances keyed by browser settings.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
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

    def __init__(self, max_size: int = 1) -> None:
        if max_size < 1:
            raise ValueError("BrowserFetcherPool max_size must be >= 1")
        self._max_size = max_size
        self._fetchers: OrderedDict[Any, BrowserFetcher] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, config: dict[str, Any]) -> BrowserFetcher:
        """Return a shared fetcher for *config*, creating it lazily."""
        key = _freeze(config)
        evicted: BrowserFetcher | None = None
        with self._lock:
            fetcher = self._fetchers.get(key)
            if fetcher is None:
                fetcher = BrowserFetcher(**config)
                self._fetchers[key] = fetcher
                if len(self._fetchers) > self._max_size:
                    _, evicted = self._fetchers.popitem(last=False)
                logger.info(
                    "Created shared browser fetcher (pool_size=%d, max_size=%d)",
                    len(self._fetchers),
                    self._max_size,
                )
            else:
                self._fetchers.move_to_end(key)

        if evicted is not None:
            try:
                evicted.close()
            except Exception:
                logger.exception("Error while closing evicted browser fetcher")
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
