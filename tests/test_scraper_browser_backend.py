"""Tests for the browser-backend wiring inside :class:`ScraperService`.

Covers the property-level behaviour of the new ``browser_backend`` /
``browser_fetcher`` surface and the ``fetch_backend`` metadata that flows into
the scrape response — without any real browser engine.  The scraper is
constructed via ``object.__new__`` (bypassing the heavy async client setup)
and only the attributes under test are injected.
"""

import json
from unittest.mock import MagicMock

import pytest

from app.core.config import Settings
from app.core.errors import BrowserError, SecurityError
from app.scraper.http_fetcher import FetchResult
from app.scraper.service import ScraperService


def _make_scraper() -> ScraperService:
    """A ScraperService with enough state to exercise the backend properties.

    The ``browser_backend`` property builds an ``asyncio.Lock``, which requires
    a running event loop, so all backend tests are async.
    """
    scraper = object.__new__(ScraperService)
    scraper.settings = Settings(api_key="test-key")
    scraper._browser_fetcher_pool = None
    scraper._browser_fetcher = None
    scraper._browser_fetcher_args = {}
    scraper._camofox_client = None
    scraper._browser_backend = None
    scraper._browser_backend_lock = None
    return scraper


# ------------------------------------------------------- browser_backend property


async def test_browser_backend_none_pool_no_fallback():
    scraper = _make_scraper()
    # Default settings: botasaurus primary, no fallbacks, no pool.
    backend = scraper.browser_backend
    assert backend.name == "botasaurus"


async def test_browser_backend_wraps_pool_without_fallback():
    scraper = _make_scraper()
    # The pool path builds a BotasaurusBackend from the pool (not a fetcher).
    scraper._browser_fetcher_pool = MagicMock()

    backend = scraper.browser_backend
    assert backend.name == "botasaurus"
    # A single backend is returned directly (no orchestrator wrapper).
    assert type(backend).__name__ == "BotasaurusBackend"
    backend.close()


async def test_browser_backend_wraps_pool_with_camofox_fallback():
    scraper = _make_scraper()
    # Enable camofox in settings and list it as a fallback.
    scraper.settings = Settings(
        api_key="test-key",
        browser_fallback_backends=["camofox"],
        _raw_yaml_camofox={"enabled": True, "base_url": "http://raspberry5:9377"},
    )
    scraper._browser_fetcher_pool = MagicMock()
    # A shared camofox client (duck-typed; build_camofox_backend only reads .name).
    scraper._camofox_client = MagicMock(name="camofox-client")

    backend = scraper.browser_backend
    # The orchestrator's name is the primary's name.
    assert backend.name == "botasaurus"
    # Two backends registered: botasaurus (primary) + camofox (fallback).
    assert len(backend._backends) == 2


async def test_browser_backend_camofox_enabled_but_not_listed():
    """Camofox enabled in config but *not* in the fallback list → no fallback."""
    scraper = _make_scraper()
    scraper.settings = Settings(
        api_key="test-key",
        _raw_yaml_camofox={"enabled": True, "base_url": "http://raspberry5:9377"},
    )
    # No fallback_backends → even though a client exists, no fallback.
    scraper._camofox_client = MagicMock(name="camofox-client")

    backend = scraper.browser_backend
    # Primary only: a single backend is returned directly (no orchestrator).
    assert backend.name == "botasaurus"
    assert type(backend).__name__ == "BotasaurusBackend"
    backend.close()


# ------------------------------------------------------- browser_fetcher property


def test_browser_fetcher_returns_pool_fetcher():
    scraper = _make_scraper()
    fetcher = MagicMock(name="botasaurus-fetcher")
    pool = MagicMock()
    pool.get = lambda args: fetcher
    scraper._browser_fetcher_pool = pool

    assert scraper.browser_fetcher is fetcher


def test_browser_fetcher_none_pool_returns_cached_fetcher():
    scraper = _make_scraper()
    fetcher = MagicMock(name="botasaurus-fetcher")
    scraper._browser_fetcher = fetcher

    assert scraper.browser_fetcher is fetcher


async def test_browser_backend_caches_across_calls():
    scraper = _make_scraper()
    first = scraper.browser_backend
    second = scraper.browser_backend
    assert first is second


# ------------------------------------------------------- fetch_backend in the response


def _cache_entry(fetch_backend: str | None):
    e = MagicMock()
    e.html = "<html>ok</html>"
    e.status_code = 200
    e.final_url = "https://example.com"
    e.error_metadata = json.dumps({"fetch_backend": fetch_backend}) if fetch_backend else None
    return e


def test_entry_to_response_records_fetch_backend():
    scraper = _make_scraper()
    entry = _cache_entry("camofox")
    resp = scraper._entry_to_response(entry, "https://example.com", False)
    # _entry_to_response returns a dict; the response model is built on top.
    assert resp["metadata"]["fetch_backend"] == "camofox"


def test_entry_to_response_none_backend_for_http():
    scraper = _make_scraper()
    entry = _cache_entry(None)
    resp = scraper._entry_to_response(entry, "https://example.com", False)
    assert resp["metadata"]["fetch_backend"] is None


# ----------------------------------------------------------- _fetch_browser end-to-end


class _FailingBackend:
    """A backend that always raises a BrowserError (simulates a crashed driver)."""

    name = "botasaurus"

    async def fetch(self, url, **kwargs):
        raise BrowserError("primary driver failed")

    async def health(self):
        return False

    def close(self):
        pass


class _WorkingBackend:
    """A backend that returns a valid FetchResult."""

    name = "camofox"

    def __init__(self, html: str = "<html>" + "x" * 600 + "</html>") -> None:
        self._html = html

    async def fetch(self, url, **kwargs):
        return FetchResult(
            html=self._html,
            status_code=200,
            final_url=url,
            backend_name=self.name,
            backend_metadata={"via": "camofox"},
        )

    async def health(self):
        return True

    def close(self):
        pass


async def test_fetch_browser_uses_backend_and_falls_back():
    """_fetch_browser drives the orchestrator; failing primary → working fallback."""
    scraper = _make_scraper()
    from app.scraper.browser_backends.orchestrator import BrowserBackendOrchestrator

    # Inject an orchestrator (failing botasaurus → working camofox).
    scraper._browser_backend = BrowserBackendOrchestrator(
        [_FailingBackend(), _WorkingBackend("<html>camofox " + "content " * 80 + "</html>")]
    )

    result = await scraper._fetch_browser(
        url="https://example.com",
        timeout=10,
        wait_until="networkidle",
        wait_selector=None,
        scroll_config=None,
        debug_config={},
    )

    # The fallback backend produced the result.
    assert result.backend_name == "camofox"
    assert "camofox" in result.html
    assert result.status_code == 200
    scraper._browser_backend.close()


async def test_fetch_browser_propagates_security_error():
    """A security error from the backend is not swallowed by the fallback."""

    class _SecurityBackend:
        name = "botasaurus"

        async def fetch(self, url, **kwargs):
            raise SecurityError("blocked localhost")

        async def health(self):
            return False

        def close(self):
            pass

    from app.scraper.browser_backends.orchestrator import BrowserBackendOrchestrator

    scraper = _make_scraper()
    scraper._browser_backend = BrowserBackendOrchestrator(
        [_SecurityBackend(), _WorkingBackend("<html>camofox</html>")]
    )
    with pytest.raises(SecurityError):
        await scraper._fetch_browser(
            url="http://localhost",
            timeout=10,
            wait_until="domcontentloaded",
            wait_selector=None,
            scroll_config=None,
            debug_config={},
        )
    scraper._browser_backend.close()
