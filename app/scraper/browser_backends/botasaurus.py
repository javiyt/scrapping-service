"""Primary browser backend: an adapter over the existing Botasaurus fetcher.

Botasaurus remains the default/primary browser engine.  This adapter is a
thin wrapper that:

* exposes the :class:`app.scraper.browser_backend.BrowserBackend` contract,
* forwards every fetch argument to the underlying
  :class:`app.scraper.browser_fetcher.BrowserFetcher` (which owns the driver
  lifecycle, idle timers, and challenge handling — none of that is changed),
* stamps the returned :class:`FetchResult` with ``backend_name`` /
  ``backend_metadata`` so the service can record *which* backend fetched the
  page, and
* normalises any driver/execution failure into a
  :class:`app.core.errors.BrowserError` so the orchestrator can apply a single
  fallback policy.

The heavy lifting (selenium driver, cookies, CDP headers, scrolling,
challenge resolution) all stays in :class:`BrowserFetcher`; this file adds no
scraping logic.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.errors import BrowserError, ScraperError
from app.scraper.browser_fetcher import BrowserFetcher
from app.scraper.http_fetcher import FetchResult

logger = logging.getLogger("scraper-api.fetcher.botasaurus")

NAME = "botasaurus"


class BotasaurusBackend:
    """A :class:`BrowserBackend` backed by the (primary) Botasaurus Driver."""

    def __init__(self, fetcher_args: dict[str, Any]) -> None:
        self._args = dict(fetcher_args)
        self._fetcher: BrowserFetcher | None = None

    @property
    def name(self) -> str:
        return NAME

    @property
    def fetcher(self) -> BrowserFetcher:
        """Lazily construct the shared :class:`BrowserFetcher`."""
        if self._fetcher is None:
            self._fetcher = BrowserFetcher(**self._args)
        return self._fetcher

    async def fetch(
        self,
        url: str,
        timeout_seconds: int | None = None,
        wait_until: str = "networkidle",
        wait_selector: str | None = None,
        scroll_config: dict[str, Any] | None = None,
        screenshot_path: str | None = None,
        proxy_url: str | None = None,
        cookies: list[dict] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> FetchResult:
        """Fetch *url* through the Botasaurus driver and stamp the result."""
        try:
            result = await self.fetcher.fetch(
                url=url,
                timeout_seconds=timeout_seconds,
                wait_until=wait_until,
                wait_selector=wait_selector,
                scroll_config=scroll_config,
                screenshot_path=screenshot_path,
                proxy_url=proxy_url,
                cookies=cookies,
                extra_headers=extra_headers,
            )
        except ScraperError as exc:
            # The underlying fetcher may raise domain errors; normalise to a
            # single browser-failure type for the orchestrator's fallback.
            raise BrowserError(
                "Botasaurus browser fetch failed",
                details={
                    "backend": self.name,
                    "url": url,
                    "error": getattr(exc, "message", str(exc)),
                },
            ) from exc
        except (ImportError, Exception) as exc:  # noqa: BLE001 - driver-level failures
            if isinstance(exc, BrowserError):
                raise
            raise BrowserError(
                "Botasaurus browser fetch failed",
                details={
                    "backend": self.name,
                    "url": url,
                    "error": getattr(exc, "message", str(exc)),
                },
            ) from exc

        result.backend_name = self.name
        if result.backend_metadata is None:
            result.backend_metadata = {}
        result.backend_metadata.setdefault("via", "botasaurus")
        return result

    async def health(self) -> bool:
        """Cheap capability check: is the Botasaurus driver importable?"""
        try:
            import botasaurus_driver  # noqa: F401

            return True
        except ImportError:
            return False

    @classmethod
    def fetcher_only(cls, fetcher: Any) -> BotasaurusBackend:
        """Wrap an existing :class:`BrowserFetcher` without constructing a new one.

        Used by the service when it already holds a shared fetcher (from the
        pool); the wrapper is a pure :class:`BrowserBackend` view over it.
        """
        instance = cls.__new__(cls)
        instance._args = {}
        instance._fetcher = fetcher
        return instance

    def close(self) -> None:
        """Release the driver (idle timer, process).  Safe to call twice."""
        if self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None


__all__ = ["BotasaurusBackend", "NAME"]
