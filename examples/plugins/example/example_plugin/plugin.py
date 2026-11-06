"""Example plugin for scrapping-service demonstrating the plugin API v1.

This plugin demonstrates:

- A complete, minimal SearchProvider that satisfies the async contract
  (capabilities(), search() returning SearchResponse with SearchResult items).
- A per-domain extractor for example.com domains (CSS extractor).
- A custom extractor for *.example.com domains (callable extract function).
- Configuration via SCRAPER_PLUGIN_EXAMPLE_* environment variables.

Install as a derived Docker image:

    COPY example /tmp/example
    RUN pip install --no-cache-dir /tmp/example

Or install in a venv and run the service:

    pip install -e examples/plugins/example
    python -mscrapping-service

The plugin registers under the entry point group *scrapping_service.plugins*:

    [project.entry-points."scrapping_service.plugins"]
    example = "example_plugin.plugin:ExamplePlugin"
"""

from __future__ import annotations

from typing import Any

from app.plugin_api import Plugin, PluginContext, PluginRegistry
from app.scraper.extractor_registry import ExtractionResult


class ExamplePlugin(Plugin):
    """Example plugin that registers a real SearchProvider and extractors.

    This plugin satisfies the v1 plugin contract and demonstrates
    both built-in provider types (search, extractor) and configuration.
    """

    name = "example-plugin"
    version = "0.1.0"
    api_version = "1.0.0"

    def register(self, registry: PluginRegistry, ctx: PluginContext) -> None:
        """Register the example search provider and extractors."""
        # Parse plugin-specific config from SettingsView
        plugin_cfg = getattr(ctx.settings, "plugin", {}) or {}
        timeout_seconds = plugin_cfg.get("timeout-seconds", 30)
        debug_enabled = plugin_cfg.get("debug-enabled", False)

        # Register a SearchProvider that conforms to the async contract
        from app.search.contract import SearchProvider, SearchQuery, SearchResponse
        from app.search.models import SearchCapabilities, SearchMetadata, SearchResult, Vertical

        class _ExampleSearchProvider(SearchProvider):
            """Example search provider that returns canned results."""

            name = "example"

            def __init__(self) -> None:
                self._timeout = timeout_seconds
                self._debug = debug_enabled
                self.requests: list[SearchQuery] = []

            async def capabilities(self) -> SearchCapabilities:
                """Return capabilities for this provider."""
                return SearchCapabilities(
                    verticals=[Vertical.WEB, Vertical.NEWS, Vertical.IMAGES],
                    available=True,
                )

            async def search(self, request: SearchQuery) -> SearchResponse:
                """Return a canned search response."""
                self.requests.append(request)

                # Build canned results matching the request verticals
                results: list[SearchResult] = []
                for idx, vertical in enumerate(request.verticals, start=1):
                    results.append(
                        SearchResult(
                            title=f"Example result for '{request.query}' ({vertical})",
                            url=f"https://example.com/search?q={request.query}&v={vertical}",
                            rank=idx,
                            vertical=vertical,
                            provider=self.name,
                            description="This is an example result from the example plugin.",
                        )
                    )

                return SearchResponse(
                    query=request.query,
                    verticals=request.verticals,
                    results=results,
                    metadata=SearchMetadata(
                        provider=self.name,
                        providers_attempted=[self.name],
                        elapsed_ms=15,
                        warnings=[],
                    ),
                )

        registry.register_search_provider(_ExampleSearchProvider())

        # Register a per-domain extractor for example.com domains
        # (CSS extractor using the built-in extractor engine)
        registry.register_extractor(
            ExtractionResult(
                domain="example.com",
                name="example-main",
                extractor_type="css",
                fields={
                    "title": {"selector": "h1", "type": "text"},
                    "summary": {
                        "selector": "meta[name=description]",
                        "type": "attribute",
                        "attribute": "content",
                    },
                },
            )
        )

        # Register a custom extractor for any subdomain (*.example.com)
        from app.scraper.extractor import extract as css_extract

        def _custom_extractor(html: str, url: str) -> dict[str, Any]:
            """Custom extraction logic for example.com subdomains."""
            # Extract the subdomain from the URL
            from urllib.parse import urlparse

            parsed = urlparse(url)
            hostname = parsed.netloc
            # Extract subdomain (everything before .example.com)
            subdomain = None
            if hostname.endswith(".example.com"):
                parts = hostname.split(".example.com", 1)
                if parts and parts[0]:
                    subdomain = parts[0]

            # Build extracted data
            data: dict[str, Any] = {
                "url": url,
                "hostname": hostname,
                "subdomain": subdomain,
            }

            # If CSS extraction is needed too, delegate
            # (in a real plugin, you'd add CSS fields to ExtractionResult
            # and call app.scraper.extractor.extract separately)
            try:
                css_data = css_extract(
                    html=html,
                    extract={"body": {"selector": "body", "type": "text"}},
                )
                data["body_text"] = css_data.get("body", "")[:200]  # Truncate
            except Exception:
                data["body_text"] = ""

            return data

        registry.register_extractor(
            ExtractionResult(
                domain="*.example.com",
                name="example-custom",
                extractor_type="custom",
                extractor=_custom_extractor,
            )
        )
