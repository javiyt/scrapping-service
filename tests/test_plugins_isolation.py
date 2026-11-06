"""Tests for plugin runtime isolation.

These tests verify that wrapped plugin providers map exceptions to
fallback-eligible errors and that timeouts are properly enforced.

"""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import (
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
    TranscriptProviderTimeoutError,
    TranscriptProviderUnavailableError,
)
from app.plugins.isolation import (
    wrap_search_provider,
    wrap_transcript_provider,
)


@pytest.fixture
def mock_search_provider():
    """Create a mock search provider."""
    provider = MagicMock()
    provider.name = "test-search-provider"
    provider.capabilities = AsyncMock()
    provider.search = AsyncMock()
    provider.health = AsyncMock()
    return provider


@pytest.fixture
def mock_transcript_provider():
    """Create a mock transcript provider."""
    provider = MagicMock()
    provider.name = "test-transcript-provider"
    provider.health = AsyncMock()
    provider.fetch = AsyncMock()
    return provider


class TestSearchProviderIsolation:
    """Tests for SearchProvider wrapping."""

    def test_wrap_search_provider_returns_wrapped(self, mock_search_provider):
        """wrap_search_provider must return a wrapped instance."""
        wrapped = wrap_search_provider(mock_search_provider, "test-plugin", "test-provider")
        assert wrapped is not mock_search_provider
        assert wrapped.name == "test-search-provider"

    async def test_search_capabilities_success(self, mock_search_provider):
        """Success path for capabilities must return wrapped result."""
        mock_search_provider.capabilities = AsyncMock(return_value={"foo": "bar"})
        wrapped = wrap_search_provider(mock_search_provider, "test-plugin", "test-provider")

        result = await wrapped.capabilities()
        assert result == {"foo": "bar"}
        mock_search_provider.capabilities.assert_awaited_once()

    async def test_search_capabilities_timeout(self, mock_search_provider):
        """Timeout in capabilities must raise SearchProviderTimeoutError."""
        mock_search_provider.capabilities = AsyncMock(side_effect=TimeoutError())

        wrapped = wrap_search_provider(mock_search_provider, "test-plugin", "test-provider")

        with pytest.raises(SearchProviderTimeoutError):
            await wrapped.capabilities()

    async def test_search_capabilities_exception(self, mock_search_provider):
        """Exception in capabilities must raise SearchProviderUnavailableError."""
        mock_search_provider.capabilities = AsyncMock(side_effect=ValueError("boom"))

        wrapped = wrap_search_provider(mock_search_provider, "test-plugin", "test-provider")

        with pytest.raises(SearchProviderUnavailableError) as exc_info:
            await wrapped.capabilities()
        assert "test-plugin" in str(exc_info.value)
        assert "test-provider" in str(exc_info.value)

    async def test_search_success(self, mock_search_provider):
        """Success path for search must return wrapped result."""
        mock_search_provider.search = AsyncMock(return_value={"results": []})
        wrapped = wrap_search_provider(mock_search_provider, "test-plugin", "test-provider")

        result = await wrapped.search({"query": "test"})
        assert result == {"results": []}
        mock_search_provider.search.assert_awaited_once()

    async def test_search_timeout(self, mock_search_provider):
        """Timeout in search must raise SearchProviderTimeoutError."""
        mock_search_provider.search = AsyncMock(side_effect=TimeoutError())

        wrapped = wrap_search_provider(mock_search_provider, "test-plugin", "test-provider")

        with pytest.raises(SearchProviderTimeoutError):
            await wrapped.search({"query": "test"})

    async def test_search_exception(self, mock_search_provider):
        """Exception in search must raise SearchProviderUnavailableError."""
        mock_search_provider.search = AsyncMock(side_effect=ValueError("boom"))

        wrapped = wrap_search_provider(mock_search_provider, "test-plugin", "test-provider")

        with pytest.raises(SearchProviderUnavailableError) as exc_info:
            await wrapped.search({"query": "test"})
        assert "test-plugin" in str(exc_info.value)

    async def test_health_success(self, mock_search_provider):
        """Success path for health must return wrapped result."""
        mock_search_provider.health = AsyncMock(return_value={"status": "ok"})
        wrapped = wrap_search_provider(mock_search_provider, "test-plugin", "test-provider")

        result = await wrapped.health()
        assert result == {"status": "ok"}
        mock_search_provider.health.assert_awaited_once()

    async def test_health_timeout(self, mock_search_provider):
        """Timeout in health must raise SearchProviderTimeoutError."""
        mock_search_provider.health = AsyncMock(side_effect=TimeoutError())

        wrapped = wrap_search_provider(mock_search_provider, "test-plugin", "test-provider")

        with pytest.raises(SearchProviderTimeoutError):
            await wrapped.health()

    async def test_health_exception(self, mock_search_provider):
        """Exception in health must raise SearchProviderUnavailableError."""
        mock_search_provider.health = AsyncMock(side_effect=ValueError("boom"))

        wrapped = wrap_search_provider(mock_search_provider, "test-plugin", "test-provider")

        with pytest.raises(SearchProviderUnavailableError) as exc_info:
            await wrapped.health()
        assert "test-plugin" in str(exc_info.value)


class TestTranscriptProviderIsolation:
    """Tests for TranscriptProvider wrapping."""

    def test_wrap_transcript_provider_returns_wrapped(self, mock_transcript_provider):
        """wrap_transcript_provider must return a wrapped instance."""
        wrapped = wrap_transcript_provider(mock_transcript_provider, "test-plugin", "test-provider")
        assert wrapped is not mock_transcript_provider
        assert wrapped.name == "test-transcript-provider"

    async def test_health_success(self, mock_transcript_provider):
        """Success path for health must return wrapped result."""
        mock_transcript_provider.health = AsyncMock(return_value={"status": "ok"})
        wrapped = wrap_transcript_provider(mock_transcript_provider, "test-plugin", "test-provider")

        result = await wrapped.health()
        assert result == {"status": "ok"}
        mock_transcript_provider.health.assert_awaited_once()

    async def test_health_timeout(self, mock_transcript_provider):
        """Timeout in health must raise TranscriptProviderTimeoutError."""
        mock_transcript_provider.health = AsyncMock(side_effect=TimeoutError())

        wrapped = wrap_transcript_provider(mock_transcript_provider, "test-plugin", "test-provider")

        with pytest.raises(TranscriptProviderTimeoutError):
            await wrapped.health()

    async def test_health_exception(self, mock_transcript_provider):
        """Exception in health must raise TranscriptProviderUnavailableError."""
        mock_transcript_provider.health = AsyncMock(side_effect=ValueError("boom"))

        wrapped = wrap_transcript_provider(mock_transcript_provider, "test-plugin", "test-provider")

        with pytest.raises(TranscriptProviderUnavailableError) as exc_info:
            await wrapped.health()
        assert "test-plugin" in str(exc_info.value)

    async def test_fetch_success(self, mock_transcript_provider):
        """Success path for fetch must return wrapped result."""
        mock_transcript_provider.fetch = AsyncMock(
            return_value={"transcript": "test", "language": "en"}
        )
        wrapped = wrap_transcript_provider(mock_transcript_provider, "test-plugin", "test-provider")

        result = await wrapped.fetch("abc123", ["en"])
        assert result == {"transcript": "test", "language": "en"}
        mock_transcript_provider.fetch.assert_awaited_once()

    async def test_fetch_timeout(self, mock_transcript_provider):
        """Timeout in fetch must raise TranscriptProviderTimeoutError."""
        mock_transcript_provider.fetch = AsyncMock(side_effect=TimeoutError())

        wrapped = wrap_transcript_provider(mock_transcript_provider, "test-plugin", "test-provider")

        with pytest.raises(TranscriptProviderTimeoutError):
            await wrapped.fetch("abc123", ["en"])

    async def test_fetch_exception(self, mock_transcript_provider):
        """Exception in fetch must raise TranscriptProviderUnavailableError."""
        mock_transcript_provider.fetch = AsyncMock(side_effect=ValueError("boom"))

        wrapped = wrap_transcript_provider(mock_transcript_provider, "test-plugin", "test-provider")

        with pytest.raises(TranscriptProviderUnavailableError) as exc_info:
            await wrapped.fetch("abc123", ["en"])
        assert "test-plugin" in str(exc_info.value)


def test_guarded_extract_custom_extractor_timeout():
    """Custom extractors should be wrapped with timeout protection."""
    from app.plugins.isolation import guarded_extract

    # Test 1: Fast custom extractor returns result
    def fast_extractor(html, url):
        return {"title": "Fast Result"}

    result = guarded_extract(fast_extractor, "<html></html>", "https://example.com", timeout=5)
    assert result == {"title": "Fast Result"}


def test_guarded_extract_custom_extractor_timeout_hang():
    """Custom extractor timeout should be detected and handled gracefully."""
    from app.plugins.isolation import guarded_extract

    # Test 2: Slow custom extractor that hangs
    def slow_extractor(html, url):
        time.sleep(10)  # Sleep longer than timeout
        return {"title": "Should not return"}

    result = guarded_extract(slow_extractor, "<html></html>", "https://example.com", timeout=1)
    # On timeout, should return error string
    assert isinstance(result, str)
    assert "timed out" in result.lower() or "timeout" in result.lower()


def test_guarded_extract_custom_extractor_exception():
    """Custom extractor exceptions should be caught and returned as error."""
    from app.plugins.isolation import guarded_extract

    def failing_extractor(html, url):
        raise ValueError("Intentional failure")

    result = guarded_extract(failing_extractor, "<html></html>", "https://example.com", timeout=5)
    # Should return error string
    assert isinstance(result, str)
    assert "error" in result.lower() or "intentional" in result.lower()
