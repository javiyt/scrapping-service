"""Tests for the example plugin contract suite.

These tests verify that a SearchProvider matching the example plugin
passes the service's search contract suite. The example plugin files
are in examples/plugins/example/ for local installation and Docker builds.
"""

from app.plugin_testing.search_contract import run_search_contract_suite


def test_example_plugin_search_contract() -> None:
    """The example plugin's search provider must pass the contract suite."""
    from app.search import SearchQuery, SearchResponse
    from app.search.models import SearchCapabilities, SearchMetadata, SearchResult, Vertical

    class ExampleSearchProvider:
        """SearchProvider matching the example plugin."""

        name = "example"

        async def capabilities(self) -> SearchCapabilities:
            return SearchCapabilities(
                verticals=[Vertical.WEB, Vertical.NEWS, Vertical.IMAGES], available=True
            )

        async def search(self, request: SearchQuery) -> SearchResponse:
            results = [
                SearchResult(
                    title=f"Example result for '{request.query}' ({v})",
                    url=f"https://example.com/search?q={request.query}&v={v}",
                    rank=i + 1,
                    vertical=v,
                    provider="example",
                )
                for i, v in enumerate(request.verticals)
            ]
            return SearchResponse(
                query=request.query,
                verticals=request.verticals,
                results=results,
                metadata=SearchMetadata(
                    provider="example",
                    providers_attempted=["example"],
                    elapsed_ms=15,
                ),
            )

    search_provider = ExampleSearchProvider()
    run_search_contract_suite(search_provider)
