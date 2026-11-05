"""Plugin API contracts and models.

This module defines the stable, versioned contract that third-party
plugins must satisfy to integrate with scrapping-service.

The public surface includes:

- :const:`PLUGIN_API_VERSION` — declared major.minor.patch for
  compatibility checking; plugins must match the major version.
- :class:`Plugin` — base protocol every plugin must implement.
- :class:`PluginContext` — read-only context passed to plugins during
  registration (narrow view of settings, logger, and HTTP client).
- :class:`PluginRegistry` — typed registration methods for each provider
  category.

Contracts for each provider type are re-exported to avoid importing app
internals:

- :class:`SearchProvider` — from :mod:`app.search.contract`.
- :class:`TranscriptProvider` — from :mod:`app.transcript.contract`.
- :class:`TranscriptHealth` — from :mod:`app.transcript.contract`.
- :class:`AgentBrowserProvider` — from :mod:`app.browser.providers.base`.
- :class:`ExtractionResult` — from :mod:`app.scraper.extractor_registry`.

Errors:

- :class:`PluginLoadError` — raised by the loader when a plugin cannot be
  instantiated or registered.

All imports are shallow re-exports from leaf modules; importing
:mod:`app.plugin_api` does NOT pull :mod:`app.search` or other internals,
fulfilling the "plugin API is importable without importing app internals"
acceptance criterion.

"""

from __future__ import annotations

# Re-export contracts (leaf modules only)
# from app.core.errors import ScraperError  # unused in v1
from app.scraper.extractor import ExtractionError
from app.scraper.extractor_registry import ExtractionResult, ExtractorRegistry
from app.search.contract import SearchProvider
from app.transcript.contract import TranscriptHealth, TranscriptProvider

from .base import Plugin
from .context import PluginContext, SettingsView
from .errors import PluginLoadError
from .registry import PluginRegistry

__all__ = [
    "ExtractionError",
    "ExtractionResult",
    "ExtractorRegistry",
    "Plugin",
    "PluginContext",
    "PluginLoadError",
    "PluginRegistry",
    "PLUGIN_API_VERSION",
    "SearchProvider",
    "SettingsView",
    "TranscriptHealth",
    "TranscriptProvider",
]


#: The declared plugin API version. Plugins must match the major version.
PLUGIN_API_VERSION = "1.0.0"
