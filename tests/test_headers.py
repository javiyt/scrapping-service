from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.scraper.headers import parse_header_config, validate_header_name, validate_header_value
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


# --------------------------------------------------------------- allowlist


def test_allowed_standard_header_names():
    for name in ("Authorization", "authorization", "Referer", "Accept-Language", "Origin"):
        assert validate_header_name(name) is None


def test_x_prefixed_headers_are_allowed():
    assert validate_header_name("X-Custom-Trace") is None
    assert validate_header_name("x-request-id") is None


def test_disallowed_header_name_rejected():
    for name in ("Host", "Content-Length", "Transfer-Encoding", "Cookie", "Connection"):
        assert validate_header_name(name) is not None


def test_invalid_header_name_characters_rejected():
    assert validate_header_name("X-Bad Name") is not None
    assert validate_header_name("X-Inj:ect") is not None


def test_header_value_crlf_injection_rejected():
    assert validate_header_value("X-Test", "value\r\nSet-Cookie: evil=1") is not None
    assert validate_header_value("X-Test", "value\nEvil") is not None
    assert validate_header_value("X-Test", "clean value") is None


def test_header_value_too_long_rejected():
    assert validate_header_value("X-Test", "a" * 4001) is not None
    assert validate_header_value("X-Test", "a" * 4000) is None


# --------------------------------------------------------------- parse_header_config


def test_parse_header_config_disabled_returns_empty():
    assert parse_header_config({"enabled": False, "values": {"X-Test": "1"}}) == {}


def test_parse_header_config_filters_disallowed_entries():
    result = parse_header_config(
        {
            "enabled": True,
            "values": {"X-Test": "1", "Host": "evil.com", "authorization": "Bearer abc"},
        }
    )
    assert result == {"X-Test": "1", "authorization": "Bearer abc"}


# --------------------------------------------------------------- schema validation (API)


def test_api_passes_header_config_to_scraper():
    mock_scraper = AsyncMock()
    mock_scraper.scrape.return_value = SAMPLE_RESULT
    app.state.scraper = mock_scraper
    app.state.cache = MagicMock()

    response = TestClient(app).post(
        "/v1/scrape",
        json={
            "url": "https://x.com",
            "headers": {"enabled": True, "values": {"X-Trace-Id": "abc123"}},
        },
        headers=AUTH_HEADER,
    )

    assert response.status_code == 200, response.text
    call_kwargs = mock_scraper.scrape.call_args.kwargs
    assert call_kwargs["header_config"] == {
        "enabled": True,
        "values": {"X-Trace-Id": "abc123"},
    }


def test_api_rejects_enabled_header_config_without_values():
    app.state.scraper = AsyncMock()
    app.state.cache = MagicMock()

    response = TestClient(app).post(
        "/v1/scrape",
        json={"url": "https://x.com", "headers": {"enabled": True}},
        headers=AUTH_HEADER,
    )

    assert response.status_code == 422


def test_api_rejects_disallowed_header_name():
    app.state.scraper = AsyncMock()
    app.state.cache = MagicMock()

    response = TestClient(app).post(
        "/v1/scrape",
        json={
            "url": "https://x.com",
            "headers": {"enabled": True, "values": {"Host": "evil.com"}},
        },
        headers=AUTH_HEADER,
    )

    assert response.status_code == 422


def test_api_rejects_crlf_injection_in_header_value():
    app.state.scraper = AsyncMock()
    app.state.cache = MagicMock()

    response = TestClient(app).post(
        "/v1/scrape",
        json={
            "url": "https://x.com",
            "headers": {"enabled": True, "values": {"X-Test": "a\r\nSet-Cookie: evil=1"}},
        },
        headers=AUTH_HEADER,
    )

    assert response.status_code == 422


# --------------------------------------------------------------- HttpFetcher


@pytest.mark.asyncio
async def test_http_fetcher_merges_extra_headers():
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
            extra_headers={"Authorization": "Bearer abc", "Accept-Language": "es-ES"},
        )

        sent_headers = mock_instance.get.call_args.kwargs["headers"]
        assert sent_headers["Authorization"] == "Bearer abc"
        assert sent_headers["Accept-Language"] == "es-ES"


# --------------------------------------------------------------- BrowserFetcher


def test_browser_fetcher_applies_extra_headers_via_cdp():
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
        [],
        {"X-Trace-Id": "abc123"},
    )

    assert result.html == "<html>ok</html>"
    assert driver.run_cdp_command.call_count >= 2


# --------------------------------------------------------------- ScraperService


@pytest.mark.asyncio
async def test_scraper_with_custom_headers_uses_credential_scoped_cache_key():
    """Header-carrying requests are still cached, but under a key scoped to
    the headers used, so different (or absent) headers never collide."""
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
            header_config={"enabled": True, "values": {"X-Trace-Id": "abc123"}},
        )

    assert result["html"] == "<html>authenticated</html>"
    cache.get.assert_called_once()
    cache.set.assert_called_once()

    used_key = cache.get.call_args.args[0]
    set_entry = cache.set.call_args.args[0]
    assert set_entry.cache_key == used_key
    assert used_key != make_cache_key("https://x.com")


@pytest.mark.asyncio
async def test_scraper_reuses_cache_for_identical_headers():
    """A second request with the exact same headers hits the cache instead
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
    header_config = {"enabled": True, "values": {"X-Trace-Id": "abc123"}}

    with (
        patch("app.scraper.service.validate_url", return_value=(True, "")),
        patch.object(
            service.http_fetcher, "fetch", AsyncMock(return_value=fetcher_result)
        ) as mock_fetch,
    ):
        await service.scrape("https://x.com", mode="http", header_config=header_config)
        await service.scrape("https://x.com", mode="http", header_config=header_config)

    assert mock_fetch.await_count == 1
