from unittest.mock import MagicMock, patch

from app.core.config import Settings
from app.scraper.browser_pool import BrowserFetcherPool
from app.scraper.service import ScraperService


def _browser_config() -> dict:
    return {
        "headless": True,
        "arguments": ["--no-sandbox"],
        "timeout_seconds": 30,
        "user_agent": "test-agent",
        "window_size": (1366, 768),
        "proxy_url": None,
    }


def test_browser_fetcher_pool_reuses_fetcher_for_same_config():
    pool = BrowserFetcherPool()

    with patch("app.scraper.browser_pool.BrowserFetcher") as fetcher_cls:
        first = pool.get(_browser_config())
        second = pool.get(_browser_config())

    assert first is second
    fetcher_cls.assert_called_once_with(**_browser_config())


def test_browser_fetcher_pool_closes_all_fetchers():
    pool = BrowserFetcherPool()

    with patch("app.scraper.browser_pool.BrowserFetcher") as fetcher_cls:
        fetcher = MagicMock()
        fetcher_cls.return_value = fetcher
        pool.get(_browser_config())

    pool.close_all()

    fetcher.close.assert_called_once_with()


def test_scraper_service_uses_shared_browser_pool():
    fetcher = MagicMock()
    pool = MagicMock()
    pool.get.return_value = fetcher

    service = ScraperService(
        settings=Settings(api_key="test-key"),
        cache=MagicMock(),
        browser_fetcher_pool=pool,
    )

    assert service.browser_fetcher is fetcher
    pool.get.assert_called_once()
