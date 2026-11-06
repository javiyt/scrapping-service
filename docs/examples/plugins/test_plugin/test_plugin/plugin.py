"""Test plugin for scrapping-service derived Docker image example.

This plugin demonstrates a minimal, self-contained plugin that registers
with the scrapping-service plugin loader via entry points.

Install in a derived image:

    COPY test_plugin /tmp/test_plugin
    RUN pip install --no-cache-dir /tmp/test_plugin

And enable at runtime (optional — empty allowlist loads all discovered):

    ENV SCRAPER_PLUGIN_ALLOWLIST=test-plugin

Plugin contract v1:

- name: unique stable identifier
- version: arbitrary format, typically semver
- api_version: must match major of app.plugin_api.PLUGIN_API_VERSION
- register(registry, ctx): called once at startup to register providers
"""

from __future__ import annotations

from typing import Any

from app.plugin_api import Plugin, PluginContext, PluginRegistry


class TestPlugin(Plugin):
    """Minimal test plugin that registers a no-op search provider.

    This plugin satisfies the v1 plugin contract: it has name, version,
    api_version attributes and a register method that registers a provider.

    The provider itself is intentionally simple: it doesn't implement
    search logic but records that it was called for smoke testing.
    """

    name = "test-plugin"
    version = "0.1.0"
    api_version = "1.0.0"

    def register(self, registry: PluginRegistry, ctx: PluginContext) -> None:
        """Register a trivial search provider for smoke testing.

        The provider doesn't implement real search — it exists only to
        prove the plugin loader discovered and loaded this entry point.
        """
        from app.search.contract import SearchProvider

        class _TestSearchProvider(SearchProvider):
            """Trivial search provider for plugin smoke tests."""

            name = "test"

            def search(
                self,
                query: str,
                *,
                limit: int = 10,
                lang: str = "all",
                safe: bool = False,
                **kwargs: Any,
            ) -> dict[str, Any]:
                """Return a minimal successful search result."""
                return {
                    "results": [],
                    "query": query,
                    "limit": limit,
                    "provider": self.name,
                }

            def generate_search_url(
                self,
                query: str,
                *,
                lang: str = "all",
                safe: bool = False,
            ) -> str:
                """Return a placeholder URL."""
                return f"https://example.com/search?q={query}&provider=test"

        registry.register_search_provider(_TestSearchProvider())