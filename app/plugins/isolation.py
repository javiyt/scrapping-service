"""Runtime isolation for plugin-provided providers.

This module wraps plugin providers (SearchProvider, TranscriptProvider)
with timeout and exception mapping so that a buggy or malicious plugin
cannot crash a request handler.

The wrappers only apply to providers registered by plugins (not builtins),
recorded by the loader in PluginLoadResult.

"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core.errors import (
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
    TranscriptProviderTimeoutError,
    TranscriptProviderUnavailableError,
)
from app.search.contract import SearchProvider
from app.transcript.contract import TranscriptProvider

#: Per-call timeout for plugin providers (configurable via SCRAPER_PLUGIN_TIMEOUT_SECONDS)
PLUGIN_CALL_TIMEOUT_SECONDS = 60


def wrap_search_provider(
    provider: SearchProvider, plugin_name: str, provider_name: str
) -> SearchProvider:
    """Wrap a plugin-provided search provider with timeout and error mapping."""
    return _WrappedSearchProvider(provider, plugin_name, provider_name)


def wrap_transcript_provider(
    provider: TranscriptProvider, plugin_name: str, provider_name: str
) -> TranscriptProvider:
    """Wrap a plugin-provided transcript provider with timeout and error mapping."""
    return _WrappedTranscriptProvider(provider, plugin_name, provider_name)


class _WrappedSearchProvider(SearchProvider):
    """SearchProvider that wraps plugin providers with timeout and error mapping."""

    def __init__(self, inner: SearchProvider, plugin_name: str, provider_name: str) -> None:
        self._inner = inner
        self._plugin_name = plugin_name
        self._provider_name = provider_name

    @property
    def name(self) -> str:
        return self._inner.name

    async def capabilities(self) -> Any:
        try:
            return await asyncio.wait_for(
                self._inner.capabilities(),
                timeout=PLUGIN_CALL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # Map timeout to broadcast-friendly error
            raise SearchProviderTimeoutError(
                f"Plugin '{self._plugin_name}' provider '{self._provider_name}' "
                f"timed out during capabilities"
            )
        except Exception as exc:
            # Map any exception to unavailable (router can fall back)
            raise SearchProviderUnavailableError(
                f"Plugin '{self._plugin_name}' provider '{self._provider_name}' "
                f"capabilities failed: {exc}"
            ) from exc

    async def search(self, request: Any) -> Any:
        try:
            return await asyncio.wait_for(
                self._inner.search(request),
                timeout=PLUGIN_CALL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            raise SearchProviderTimeoutError(
                f"Plugin '{self._plugin_name}' provider '{self._provider_name}' "
                f"timed out during search"
            )
        except Exception as exc:
            raise SearchProviderUnavailableError(
                f"Plugin '{self._plugin_name}' provider '{self._provider_name}' "
                f"search failed: {exc}"
            ) from exc

    async def health(self) -> Any:
        try:
            return await asyncio.wait_for(
                self._inner.health(),
                timeout=PLUGIN_CALL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            raise SearchProviderTimeoutError(
                f"Plugin '{self._plugin_name}' provider '{self._provider_name}' "
                f"timed out during health"
            )
        except Exception as exc:
            raise SearchProviderUnavailableError(
                f"Plugin '{self._plugin_name}' provider '{self._provider_name}' "
                f"health failed: {exc}"
            ) from exc


class _WrappedTranscriptProvider(TranscriptProvider):
    """TranscriptProvider that wraps plugin providers with timeout and error mapping."""

    def __init__(self, inner: TranscriptProvider, plugin_name: str, provider_name: str) -> None:
        self._inner = inner
        self._plugin_name = plugin_name
        self._provider_name = provider_name

    @property
    def name(self) -> str:
        return self._inner.name

    async def health(self) -> Any:
        try:
            return await asyncio.wait_for(
                self._inner.health(),
                timeout=PLUGIN_CALL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            raise TranscriptProviderTimeoutError(
                f"Plugin '{self._plugin_name}' provider '{self._provider_name}' "
                f"timed out during health"
            )
        except Exception as exc:
            raise TranscriptProviderUnavailableError(
                f"Plugin '{self._plugin_name}' provider '{self._provider_name}' "
                f"health failed: {exc}"
            ) from exc

    async def fetch(self, video_id: str, languages: list[str]) -> Any:
        try:
            return await asyncio.wait_for(
                self._inner.fetch(video_id, languages),
                timeout=PLUGIN_CALL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            raise TranscriptProviderTimeoutError(
                f"Plugin '{self._plugin_name}' provider '{self._provider_name}' "
                f"timed out during fetch"
            )
        except Exception as exc:
            raise TranscriptProviderUnavailableError(
                f"Plugin '{self._plugin_name}' provider '{self._provider_name}' fetch failed: {exc}"
            ) from exc


# Browser provider and extractor wrappers are deferred to app/browser/providers/base.py
# and app/scraper/response_processing.py respectively, since their protocol imports
# are heavier and we want to keep this module lightweight.


def guarded_extract(
    extractor_callable: Any, html: str, url: str, timeout: int | None = None
) -> Any:
    """Wrap a custom extractor function with timeout and error protection.

    Custom extractors (plugin-provided callables) are wrapped with a timeout
    so a buggy extractor cannot hang scrape requests. The callable is invoked
    as ``extractor(html, url)`` and any exception is caught and returned as
    a string in the metadata.

    Args:
        extractor_callable: The custom extractor function to wrap.
        html: The HTML string to extract from.
        url: The URL being extracted.
        timeout: Timeout in seconds; defaults to PLUGIN_CALL_TIMEOUT_SECONDS.

    Returns:
        The extracted data dict on success, or None on error (with error details
        in the metadata).
    """
    import threading

    if timeout is None:
        timeout = PLUGIN_CALL_TIMEOUT_SECONDS

    result_container: list[Any] = [None]
    error_container: list[str | None] = [None]

    def _run_extractor() -> None:
        try:
            result_container[0] = extractor_callable(html, url)
        except Exception as exc:
            error_container[0] = f"Custom extractor error: {exc}"

    # Run with timeout in a separate thread
    thread = threading.Thread(target=_run_extractor, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    if thread.is_alive():
        # Timeout - return None as error indicator
        error_container[0] = "Custom extractor timed out"

    if error_container[0] is not None:
        return error_container[0]
    return result_container[0]
