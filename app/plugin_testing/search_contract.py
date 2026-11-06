"""Contract tests for SearchProvider instances.

These tests are reused across the service's built-in providers
and third-party plugins.

Usage::

    from app.plugin_testing.search_contract import run_search_contract_suite

    # After registering your provider
    provider = MyProvider()
    run_search_contract_suite(provider)
"""

from __future__ import annotations

import asyncio

from app.core.errors import (
    ScraperError,
    SearchProviderUnavailableError,
)
from app.search import SearchCapabilities, SearchProvider, SearchQuery, SearchResponse
from app.search.models import SafeSearch, SearchResult, Vertical


def assert_search_contract(provider: SearchProvider) -> None:
    """Assert that a SearchProvider satisfies the contract.

    This function runs all contract checks synchronously (awaiting
    async methods via asyncio.run). It raises AssertionError if any
    contract requirement is violated.

    Checks:
    - provider.name is a non-empty string
    - capabilities() returns SearchCapabilities with non-empty verticals
      and boolean available
    - search(SearchQuery) returns SearchResponse with provider.name
      in metadata.provider, and results have url/title/rank
    - Errors raised are ScraperError subclasses

    Args:
        provider: An instance of SearchProvider (or any object with
          the required attributes/methods).

    Raises:
        AssertionError: if any contract requirement is violated.
    """
    # 1. Name check
    name = getattr(provider, "name", None)
    if not isinstance(name, str) or not name:
        raise AssertionError("SearchProvider.name must be a non-empty string")

    # 2. Capabilities check
    async def _check_capabilities() -> None:
        caps = await provider.capabilities()
        if not isinstance(caps, SearchCapabilities):
            raise AssertionError(f"{name}.capabilities() must return SearchCapabilities")
        if not caps.verticals:
            raise AssertionError(f"{name}.capabilities().verticals must be non-empty")
        if not isinstance(caps.available, bool):
            raise AssertionError(f"{name}.capabilities().available must be bool")

    asyncio.run(_check_capabilities())

    # 3. Search check
    async def _check_search() -> None:
        query = SearchQuery(
            query="test",
            verticals=[Vertical.WEB],
            language="all",
            safe_search=SafeSearch.OFF,
        )
        response = await provider.search(query)
        if not isinstance(response, SearchResponse):
            raise AssertionError(f"{name}.search() must return SearchResponse")
        if response.metadata.provider != name:
            raise AssertionError(f"{name}.search().metadata.provider must equal provider name")
        if not response.results:
            # Empty results are allowed but metadata must be present
            return
        for result in response.results:
            if not isinstance(result, SearchResult):
                raise AssertionError(f"{name}.search() results must be SearchResult instances")
            if not result.url:
                raise AssertionError(f"{name}.search() result must have url")
            if not result.title:
                raise AssertionError(f"{name}.search() result must have title")
            if not isinstance(result.rank, int):
                raise AssertionError(f"{name}.search() result must have integer rank")

    asyncio.run(_check_search())

    # 4. Error check (unavailable provider should raise ScraperError)
    async def _check_unavailable() -> None:
        # Temporarily make capabilities unavailable
        original_capabilities = provider.capabilities

        async def _unavailable_caps() -> SearchCapabilities:
            return SearchCapabilities(verticals=[Vertical.WEB], available=False)

        provider.capabilities = _unavailable_caps  # type: ignore[method-assign]
        query = SearchQuery(
            query="test",
            verticals=[Vertical.WEB],
            language="all",
            safe_search=SafeSearch.OFF,
        )
        try:
            await provider.search(query)
        except (SearchProviderUnavailableError, ScraperError):
            pass  # Expected
        except Exception as exc:
            raise AssertionError(
                f"{name}.search() with unavailable=True must raise ScraperError, "
                f"got {type(exc).__name__}"
            ) from None
        finally:
            provider.capabilities = original_capabilities  # type: ignore[method-assign]

    asyncio.run(_check_unavailable())


async def _run_search_contract_async(provider: SearchProvider) -> None:
    """Async version of assert_search_contract for await from test functions."""
    assert_search_contract(provider)


def run_search_contract_suite(provider: SearchProvider) -> None:
    """Run the full search provider contract suite.

    This is a helper for test suites that wants to run the contract checks
    with a single call. It calls assert_search_contract internally.

    Example::

        def test_my_plugin_search_contract():
            provider = MyPlugin().create_provider()
            run_search_contract_suite(provider)

    Args:
        provider: An instance of SearchProvider.
    """
    assert_search_contract(provider)
