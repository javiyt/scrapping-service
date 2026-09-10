from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.scraper.cookies import (
    browser_cookie_dicts,
    cookie_header_dict,
    cookies_for_url,
    parse_cookie_config,
    parse_cookie_header,
    parse_netscape_cookie_file,
)
from app.scraper.http_fetcher import FetchResult, HttpFetcher
from app.scraper.normalizer import make_cache_key
from app.scraper.service import ScraperService

VALID_API_KEY = "test-key-for-tests"
AUTH_HEADER = {"Authorization": f"Bearer {VALID_API_KEY}"}

SAMPLE_RESULT = {
    "url": "https://x.com",
    "final_url": "https://x.com",
    "status_code": 200,
    "from_cache": False,
    "stale": False,
    "fetched_at": "2026-06-30T10:00:00+00:00",
    "expires_at": None,
    "html": "<html>Hello World</html>",
    "metadata": {
        "mode": "http",
        "elapsed_ms": 120,
        "content_length": 25,
        "cache_key": "abc123",
    },
}


@pytest.fixture(autouse=True)
def _clean_app_state():
    for attr in ("scraper", "cache", "settings"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def test_parse_cookie_header():
    cookies = parse_cookie_header("auth_token=abc; ct0=def")

    assert cookie_header_dict(cookies) == {"auth_token": "abc", "ct0": "def"}


def test_parse_netscape_cookie_file_and_filter_by_domain():
    contents = "\n".join(
        [
            "# Netscape HTTP Cookie File",
            ".x.com\tTRUE\t/\tTRUE\t1820581233\tauth_token\tabc",
            "other.com\tFALSE\t/\tFALSE\t0\tignored\tvalue",
        ]
    )

    cookies = cookies_for_url(parse_netscape_cookie_file(contents), "https://x.com/home")

    assert cookie_header_dict(cookies) == {"auth_token": "abc"}
    assert browser_cookie_dicts(cookies) == [
        {
            "name": "auth_token",
            "value": "abc",
            "domain": ".x.com",
            "path": "/",
            "secure": True,
            "expires": 1820581233,
        }
    ]


def test_parse_cookie_config_disabled_returns_empty():
    assert parse_cookie_config({"enabled": False, "header": "a=1"}) == []


def test_api_passes_cookie_config_to_scraper():
    mock_scraper = AsyncMock()
    mock_scraper.scrape.return_value = SAMPLE_RESULT
    app.state.scraper = mock_scraper
    app.state.cache = MagicMock()

    response = TestClient(app).post(
        "/v1/scrape",
        json={
            "url": "https://x.com",
            "cookies": {"enabled": True, "header": "auth_token=abc; ct0=def"},
        },
        headers=AUTH_HEADER,
    )

    assert response.status_code == 200, response.text
    call_kwargs = mock_scraper.scrape.call_args.kwargs
    assert call_kwargs["cookie_config"] == {
        "enabled": True,
        "header": "auth_token=abc; ct0=def",
        "netscape": None,
    }


def test_api_rejects_enabled_cookie_config_without_source():
    app.state.scraper = AsyncMock()
    app.state.cache = MagicMock()

    response = TestClient(app).post(
        "/v1/scrape",
        json={"url": "https://x.com", "cookies": {"enabled": True}},
        headers=AUTH_HEADER,
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_http_fetcher_sends_cookies_to_httpx():
    fetcher = HttpFetcher(timeout_seconds=10, max_concurrency=1)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock()
        mock_instance.__aenter__.return_value = mock_instance
        mock_client_cls.return_value = mock_instance

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.text = "<html>ok</html>"
        mock_response.status_code = 200
        mock_response.url = httpx.URL("https://x.com")
        mock_response.headers = {"content-type": "text/html"}
        mock_instance.get.return_value = mock_response

        await fetcher.fetch(
            "https://x.com",
            cookies=[{"name": "auth_token", "value": "abc"}],
        )

        assert mock_instance.get.call_args.kwargs["cookies"] == {"auth_token": "abc"}


def test_browser_fetcher_resets_and_applies_cookies():
    from app.scraper.browser_fetcher import BrowserFetcher

    fetcher = BrowserFetcher()
    driver = MagicMock()
    driver.run_js.return_value = "complete"
    driver.page_html = "<html>ok</html>"
    driver.current_url = "https://x.com"
    fetcher._driver = driver
    fetcher._botasaurus_available = True

    result = fetcher._sync_fetch(
        "https://x.com",
        30,
        "networkidle",
        None,
        {},
        None,
        [{"name": "auth_token", "value": "abc", "domain": ".x.com", "path": "/"}],
    )

    assert result.html == "<html>ok</html>"
    driver.delete_cookies.assert_called_once_with()
    driver.add_cookies.assert_called_once_with(
        [{"name": "auth_token", "value": "abc", "domain": ".x.com", "path": "/"}]
    )


@pytest.mark.asyncio
async def test_scraper_with_cookies_uses_credential_scoped_cache_key():
    """Cookie-carrying requests are still cached, but under a key scoped to
    the cookies used — so a subsequent request with *different* (or no)
    cookies never reads the authenticated entry, while a request with the
    *same* cookies can still hit the cache instead of re-fetching."""
    from app.core.config import Settings

    cache = MagicMock()
    cache.get.return_value = None
    fetcher_result = FetchResult(
        html="<html>authenticated</html>",
        status_code=200,
        final_url="https://x.com",
        headers={},
        elapsed_ms=1,
    )
    service = ScraperService(settings=Settings(api_key=VALID_API_KEY), cache=cache)

    with (
        patch("app.scraper.service.validate_url", return_value=(True, "")),
        patch.object(service.http_fetcher, "fetch", AsyncMock(return_value=fetcher_result)),
    ):
        result = await service.scrape(
            "https://x.com",
            mode="http",
            cookie_config={"enabled": True, "header": "auth_token=abc"},
        )

    assert result["html"] == "<html>authenticated</html>"
    cache.get.assert_called_once()
    cache.set.assert_called_once()

    used_key = cache.get.call_args.args[0]
    set_entry = cache.set.call_args.args[0]
    assert set_entry.cache_key == used_key
    # Must differ from the plain (no-cookie) cache key for the same URL.
    assert used_key != make_cache_key("https://x.com")


@pytest.mark.asyncio
async def test_scraper_reuses_cache_for_identical_cookies():
    """A second request with the exact same cookies hits the cache instead
    of re-fetching."""
    from app.core.config import Settings

    store = {}
    cache = MagicMock()
    cache.get.side_effect = lambda key: store.get(key)
    cache.set.side_effect = lambda entry: store.__setitem__(entry.cache_key, entry)

    fetcher_result = FetchResult(
        html="<html>authenticated</html>",
        status_code=200,
        final_url="https://x.com",
        headers={},
        elapsed_ms=1,
    )
    service = ScraperService(settings=Settings(api_key=VALID_API_KEY), cache=cache)
    cookie_config = {"enabled": True, "header": "auth_token=abc"}

    with (
        patch("app.scraper.service.validate_url", return_value=(True, "")),
        patch.object(
            service.http_fetcher, "fetch", AsyncMock(return_value=fetcher_result)
        ) as mock_fetch,
    ):
        await service.scrape("https://x.com", mode="http", cookie_config=cookie_config)
        await service.scrape("https://x.com", mode="http", cookie_config=cookie_config)

    assert mock_fetch.await_count == 1
