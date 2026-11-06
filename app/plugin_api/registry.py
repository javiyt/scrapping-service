"""Registry protocol for plugins.

The :class:`PluginRegistry` protocol declares the typed registration methods
that plugins may call during :meth:`Plugin.register` to inject their
providers into the service's provider registries.

The protocol methods mirror the existing registry types:

- :meth:`register_search_provider` → :class:`app.search.SearchProviderRegistry`
- :meth:`register_transcript_provider` → :class:`app.transcript.TranscriptProviderRegistry`
- :meth:`register_browser_provider` → :class:`app.browser.providers.factory` (single provider)
- :meth:`register_extractor` → :class:`app.scraper.extractor_registry.ExtractorRegistry`

Plugins should NOT attempt to import or construct the real registries
themselves. Instead, they receive an implementation of this protocol
from the loader and call the appropriate method(s).

The loader enforces name uniqueness: attempting to register a provider
with an existing name raises :class:`PluginLoadError` (not just a plain
ValueError from the registry) so the plugin author gets a clear message.

"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PluginRegistry(Protocol):
    """Protocol for provider registration from plugins.

    Implementations wrap the real registries (:class:`app.search.SearchProviderRegistry`,
    :class:`app.transcript.TranscriptProviderRegistry`, etc.) and add
    collision detection and clear error messages.

    Plugins receive a PluginRegistry instance during :meth:`Plugin.register`.
    They call the appropriate method(s) to register their providers.

    All methods raise :class:`PluginLoadError` when the provider name
    collides with an existing registered provider (builtin or from another
    plugin). The error message names both the plugin and the conflicting
    provider name.

    """

    def register_search_provider(self, provider: Any) -> None:
        """Register a :class:`app.search.SearchProvider` instance.

        Args:
            provider: An object that satisfies the SearchProvider protocol
              (has ``name``, ``capabilities()``, ``search()``).

        Raises:
            PluginLoadError: if a provider with the same ``name`` is
              already registered (in the builtin set or another plugin).
        """
        ...

    def register_transcript_provider(self, provider: Any) -> None:
        """Register a :class:`app.transcript.TranscriptProvider` instance.

        Args:
            provider: An object that satisfies the TranscriptProvider
              protocol (has ``name``, ``health()``, ``fetch()``).

        Raises:
            PluginLoadError: if a provider with the same ``name`` is
              already registered.
        """
        ...

    def register_browser_provider(self, provider: Any) -> None:
        """Register an :class:`app.browser.providers.base.AgentBrowserProvider`.

        The agent browser provider is a singleton: only one may be
        registered (built-in or plugin). If another provider is already
        registered, this method raises :class:`PluginLoadError`.

        Args:
            provider: An object that satisfies the AgentBrowserProvider
              protocol.

        Raises:
            PluginLoadError: if a browser provider is already registered
              (either a built-in or from another plugin).
        """
        ...

    def register_extractor(self, extractor: Any) -> None:
        """Register an :class:`app.scraper.extractor_registry.ExtractorResult`.

        Extractors are keyed by domain and name. The ``extractor`` object
        must have:

        - ``domain`` — the domain pattern (exact or ``"*"`` for all).
        - ``name`` — a unique name within the domain.
        - ``extractor_type`` — ``"css"`` or ``"custom"``.
        - ``fields`` — CSS selector field definitions (for type ``"css"``).
        - ``priority`` — higher values are tried first.

        Args:
            extractor: An ExtractorResult instance or compatible object.

        Raises:
            PluginLoadError: if an extractor with the same domain and
              name is already registered.
        """
        ...
