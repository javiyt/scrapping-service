"""Tests for the example plugin using the reusable contract suite.

These tests verify that the example plugin installed via pip
passes the service's search contract suite.
"""

from app.plugin_testing.search_contract import run_search_contract_suite


def test_example_plugin_in_registry() -> None:
    """The example plugin's entry point must be discoverable."""
    from importlib import metadata

    eps = metadata.entry_points(group="scrapping_service.plugins")
    names = [e.name for e in eps]
    assert "example" in names, f"Entry points: {names}"


def test_example_plugin_importable() -> None:
    """The example plugin package must be importable."""
    import example_plugin
    assert hasattr(example_plugin, "ExamplePlugin")


def test_example_plugin_has_required_attributes() -> None:
    """The example plugin must have name, version, and api_version."""
    from example_plugin import ExamplePlugin

    plugin = ExamplePlugin()
    assert plugin.name == "example-plugin"
    assert plugin.version == "0.1.0"
    assert plugin.api_version == "1.0.0"


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
