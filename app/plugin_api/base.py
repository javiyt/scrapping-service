"""Plugin base protocol.

The :class:`Plugin` protocol defines the interface every plugin must satisfy.
It is a runtime-checkable protocol backed by an abstract base class for
convenience.

Plugins are instantiated at load time, and their :meth:`register` method is
called with a :class:`PluginRegistry` and :class:`PluginContext`.

Example::

    from app.plugin_api import Plugin, PluginContext, PluginRegistry

    class MySearchPlugin(Plugin):
        name = "my-search"
        version = "1.2.3"
        api_version = "1.0.0"

        def register(self, registry: PluginRegistry, ctx: PluginContext) -> None:
            # Register a search provider
            from my_search.provider import MySearchProvider

            provider = MySearchProvider()
            registry.register_search_provider(provider)

"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from .context import PluginContext
from .registry import PluginRegistry


@runtime_checkable
class Plugin(Protocol):
    """Base protocol every plugin must satisfy.

    Plugins must expose at least:

    - ``name: str`` — a unique, stable identifier for the plugin.
    - ``version: str`` — the plugin's version string (arbitrary format).
    - ``api_version: str`` — must match ``app.plugin_api.PLUGIN_API_VERSION``
      up to the major version (see :class:`PluginLoadError` on version mismatch).

    The :meth:`register` method is called once per plugin during application
    startup to register all of its providers into the service's registries.

    The protocol is ``@runtime_checkable``, so any class with the required
    attributes and method will pass ``isinstance(plugin, Plugin)`` even if
    it does not explicitly subclass this class.
    """

    name: str
    version: str
    api_version: str

    @abstractmethod
    def register(self, registry: PluginRegistry, ctx: PluginContext) -> None:
        """Register providers with the service.

        This method is called exactly once per plugin during startup.
        Implementations should:

        - Construct any provider instances.
        - Call the appropriate ``registry.register_*`` methods for each
          provider type (search, transcript, browser, extractor).
        - Raise :class:`PluginLoadError` if any provider name collides
          with an existing registered provider (builtin or from another
          plugin) or if the provider cannot be instantiated.

        Errors raised from this method prevent the plugin from loading
        and cause the entire service startup to abort (fail-fast per ADR).
        """
        ...


class _PluginBase(ABC):
    """Base class for plugins that want ``isinstance`` checks without ABC ``register``.

    Subclass this if you want ``isinstance(plugin, Plugin)`` to work but
    don't want to declare ``register`` as abstract (e.g., you implement it
    via dynamic attribute setting or don't care about isinstance).
    """

    name: str
    version: str
    api_version: str

    def register(self, registry: PluginRegistry, ctx: PluginContext) -> None:
        # Default no-op implementation — subclasses override.
        raise NotImplementedError
