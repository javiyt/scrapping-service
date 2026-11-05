"""Error types for plugins.

This module re-exports :class:`PluginLoadError` from
:mod:`app.plugin_api.errors` for convenience.

"""

from app.plugin_api.errors import PluginLoadError

__all__ = ["PluginLoadError"]
