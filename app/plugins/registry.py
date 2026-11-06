"""Plugin registry implementation for the service.

The :class:`PluginRegistryImpl` class implements the :class:`app.plugin_api.PluginRegistry`
protocol and wraps the service's real registries (search, transcript, browser,
extractor). It adds collision detection and clear error messages.

Plugins receive an instance of this class during :meth:`Plugin.register`.

"""

from __future__ import annotations

from typing import Any

from app.plugins.errors import PluginLoadError
from app.scraper.extractor_registry import ExtractionResult, ExtractorRegistry


class PluginRegistryImpl:
    """Internal registry that implements PluginRegistry with collision checks.

    This class wraps the actual registries from ``app.search``,
    ``app.transcript``, ``app.browser.providers.factory``, and
    ``app.scraper.extractor_registry`` and adds name-collision detection
    before delegating to the real registries.

    Plugins should NOT instantiate this class directly. The loader creates
    and passes one to each plugin.

    """

    def __init__(
        self,
        *,
        search_registry: Any,
        transcript_registry: Any,
        browser_provider_slot: Any,
        extractor_registry: ExtractorRegistry,
    ) -> None:
        """Initialize the registry wrapper.

        Args:
            search_registry: The real SearchProviderRegistry from app.search.
            transcript_registry: The real TranscriptProviderRegistry from app.transcript.
            browser_provider_slot: A mutable slot (single-element list or similar)
              that holds the active browser provider, or None.
            extractor_registry: The ExtractorRegistry instance for this session.
        """
        self._search = search_registry
        self._transcript = transcript_registry
        self._browser_slot = browser_provider_slot
        self._extractor = extractor_registry

    def register_search_provider(self, provider: Any) -> None:
        """Register a SearchProvider with collision detection."""
        name = getattr(provider, "name", None)
        if not name:
            raise PluginLoadError(
                plugin_name="<unknown>",
                reason="Provider has no 'name' attribute",
            )
        if self._search.get(name) is not None:
            raise PluginLoadError(
                plugin_name=getattr(provider, "name", "<unknown>"),
                reason=f"Search provider '{name}' is already registered (builtin or plugin)",
            )
        self._search.register(provider)

    def register_transcript_provider(self, provider: Any) -> None:
        """Register a TranscriptProvider with collision detection."""
        name = getattr(provider, "name", None)
        if not name:
            raise PluginLoadError(
                plugin_name="<unknown>",
                reason="Provider has no 'name' attribute",
            )
        if self._transcript.get(name) is not None:
            raise PluginLoadError(
                plugin_name=getattr(provider, "name", "<unknown>"),
                reason=f"Transcript provider '{name}' is already registered (builtin or plugin)",
            )
        self._transcript.register(provider)

    def register_browser_provider(self, provider: Any) -> None:
        """Register an AgentBrowserProvider with collision detection."""
        if self._browser_slot[0] is not None:
            existing = getattr(self._browser_slot[0], "name", "<unknown>")
            raise PluginLoadError(
                plugin_name=getattr(provider, "name", "<unknown>"),
                reason=f"Browser provider '{existing}' is already registered (builtin or plugin)",
            )
        self._browser_slot[0] = provider

    def register_extractor(self, extractor: ExtractionResult) -> None:
        """Register an ExtractorResult with collision detection and validation."""
        # Validate required fields before registration
        if not hasattr(extractor, "domain") or not extractor.domain:
            raise PluginLoadError(
                plugin_name="<unknown>",
                reason="Extractor missing required field 'domain'",
            )
        if not hasattr(extractor, "name") or not extractor.name:
            raise PluginLoadError(
                plugin_name="<unknown>",
                reason="Extractor missing required field 'name'",
            )
        extractor_type_value = getattr(extractor, "extractor_type", None)
        if not hasattr(extractor, "extractor_type") or extractor_type_value not in (
            "css",
            "custom",
        ):
            raise PluginLoadError(
                plugin_name="<unknown>",
                reason=(f"Extractor has invalid 'extractor_type': {extractor_type_value}"),
            )

        key = (extractor.domain, extractor.name)
        if key in self._extractor:
            raise PluginLoadError(
                plugin_name=getattr(extractor, "name", "<unknown>"),
                reason="Extractor already registered",
            )
        self._extractor.register(extractor)
