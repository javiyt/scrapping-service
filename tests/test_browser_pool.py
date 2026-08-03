from unittest.mock import MagicMock, patch

import pytest

from app.core.config import Settings
from app.scraper.browser_fetcher import BrowserFetcher
from app.scraper.browser_pool import BrowserFetcherPool
from app.scraper.http_fetcher import FetchResult
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


def test_browser_fetcher_pool_evicts_least_recently_used_fetcher():
    pool = BrowserFetcherPool(max_size=1)
    first_fetcher = MagicMock()
    second_fetcher = MagicMock()
    second_config = {**_browser_config(), "user_agent": "other-agent"}

    with patch("app.scraper.browser_pool.BrowserFetcher") as fetcher_cls:
        fetcher_cls.side_effect = [first_fetcher, second_fetcher]
        first = pool.get(_browser_config())
        second = pool.get(second_config)

    assert first is first_fetcher
    assert second is second_fetcher
    first_fetcher.close.assert_called_once_with()
    second_fetcher.close.assert_not_called()


def test_browser_fetcher_pool_rejects_invalid_max_size():
    with pytest.raises(ValueError, match="max_size"):
        BrowserFetcherPool(max_size=0)


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


def test_scraper_service_passes_browser_lifecycle_settings_to_pool():
    fetcher = MagicMock()
    pool = MagicMock()
    pool.get.return_value = fetcher

    service = ScraperService(
        settings=Settings(
            api_key="test-key",
            browser_idle_timeout_seconds=60,
            browser_max_uses=25,
        ),
        cache=MagicMock(),
        browser_fetcher_pool=pool,
    )

    assert service.browser_fetcher is fetcher
    config = pool.get.call_args.args[0]
    assert config["idle_timeout_seconds"] == 60
    assert config["max_uses"] == 25


def test_browser_fetcher_close_prefers_process_shutdown():
    fetcher = BrowserFetcher()
    driver = MagicMock()
    fetcher._driver = driver

    fetcher.close()

    driver.quit.assert_called_once_with()
    driver.stop.assert_not_called()
    driver.close.assert_not_called()


def test_browser_fetcher_idle_timer_closes_driver():
    fetcher = BrowserFetcher(idle_timeout_seconds=60)
    driver = MagicMock()
    fetcher._driver = driver

    with fetcher._lock:
        fetcher._schedule_idle_close_locked()
        timer = fetcher._idle_timer

    assert timer is not None

    fetcher._close_if_idle(timer)

    driver.quit.assert_called_once_with()
    assert fetcher._driver is None


def test_browser_fetcher_max_uses_recycles_driver_after_fetch():
    fetcher = BrowserFetcher(max_uses=1)
    driver = MagicMock()
    fetcher._driver = driver
    result = FetchResult(
        html="<html></html>",
        status_code=200,
        final_url="https://example.com",
        headers={},
        elapsed_ms=1,
    )

    with patch.object(fetcher, "_sync_fetch_locked", return_value=result):
        returned = fetcher._sync_fetch(
            "https://example.com",
            30,
            "networkidle",
            None,
            {},
            None,
        )

    assert returned is result
    driver.quit.assert_called_once_with()
    assert fetcher._driver is None
