"""Tests for the impersonating fetcher (ticket #180)."""

from __future__ import annotations

import pytest

from app.core.errors import SecurityError
from app.scraper.impersonating_fetcher import (
    ImpersonatingFetcher,
    _resolve_profile,
)


class TestResolveProfile:
    """Tests for _resolve_profile function."""

    def test_resolve_basic_chrome(self) -> None:
        """Basic chrome profile maps to chrome124."""
        assert _resolve_profile("chrome") == "chrome124"

    def test_resolve_basic_firefox(self) -> None:
        """Basic firefox profile maps to firefox128."""
        assert _resolve_profile("firefox") == "firefox128"

    def test_resolve_basic_safari(self) -> None:
        """Basic safari profile maps to safari17_5."""
        assert _resolve_profile("safari") == "safari17_5"

    def test_resolve_specific_version(self) -> None:
        """Specific version profiles are preserved."""
        assert _resolve_profile("chrome124") == "chrome124"
        assert _resolve_profile("safari17_2") == "safari17_2"

    def test_resolve_case_insensitive(self) -> None:
        """Profile names are resolved case-insensitively."""
        assert _resolve_profile("CHROME") == "chrome124"
        assert _resolve_profile("ChRoMe124") == "chrome124"

    def test_resolve_none_returns_none(self) -> None:
        """None profile returns None."""
        assert _resolve_profile(None) is None

    def test_resolve_unknown_raises_security_error(self) -> None:
        """Unknown profile raises SecurityError."""
        with pytest.raises(SecurityError) as exc_info:
            _resolve_profile("unknown_browser")
        assert "unknown_browser" in str(exc_info.value)


class TestImpersonatingFetcher:
    """Tests for ImpersonatingFetcher class."""

    def test_init_sets_defaults(self) -> None:
        """Initializer sets default values."""
        fetcher = ImpersonatingFetcher()
        assert fetcher._timeout == 90
        assert fetcher._max_concurrency == 1
        assert fetcher._proxy_url is None

    def test_init_with_custom_timeout(self) -> None:
        """Initializer accepts custom timeout."""
        fetcher = ImpersonatingFetcher(timeout_seconds=120)
        assert fetcher._timeout == 120

    def test_init_with_custom_max_concurrency(self) -> None:
        """Initializer accepts custom max_concurrency."""
        fetcher = ImpersonatingFetcher(max_concurrency=5)
        assert fetcher._max_concurrency == 5

    def test_init_with_proxy(self) -> None:
        """Initializer accepts proxy URL."""
        fetcher = ImpersonatingFetcher(proxy_url="http://proxy:8080")
        assert fetcher._proxy_url == "http://proxy:8080"

    @pytest.mark.asyncio
    async def test_health_returns_curl_cffi_availability(self) -> None:
        """Health returns True when curl_cffi is available."""
        fetcher = ImpersonatingFetcher()
        assert await fetcher.health() is True

    def test_close_does_not_raise(self) -> None:
        """Close is safe to call multiple times."""
        fetcher = ImpersonatingFetcher()
        fetcher.close()
        fetcher.close()  # Should not raise
