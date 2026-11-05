"""Plugins package.

This package provides the loader that discovers and registers plugins
via Python entry points.

To use the plugin API without importing app internals, import from
``app.plugin_api`` instead of ``app.plugins``.

"""

# Avoid importing app.plugin_api to prevent circular imports
# Instead, re-export leaf modules only

__all__ = [
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

# Import from leaf modules only (no circular deps)
from app.plugin_api import (
    PLUGIN_API_VERSION,
    Plugin,
    PluginContext,
    PluginLoadError,
    PluginRegistry,
    SearchProvider,
    SettingsView,
    TranscriptHealth,
    TranscriptProvider,
)
