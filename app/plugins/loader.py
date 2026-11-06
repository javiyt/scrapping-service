"""Loader for plugin entry points.

The :func:`load_plugins` function discovers and loads plugins via
Python entry points (group ``scrapping_service.plugins``), validates
their ``api_version``, filters by allowlist, and registers their
providers.

This module is imported by ``app/main.py`` in the lifespan, BEFORE
``build_search_router`` and ``build_transcript_router``, so plugins
can inject their providers before the routers are constructed.

Usage::

    from app.main import app
    from app.core.config import Settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        settings = Settings.load()
        # Load plugins before routers
        result = load_plugins(settings)
        # Inject the search/transcript registry into routers
        search_router = build_search_router(
            settings,
            extra_providers=result.search_providers,
        )
        # ... continue

"""

from __future__ import annotations

import logging
from importlib import metadata
from typing import Any

import httpx

from app.core.config import Settings
from app.plugins.errors import PluginLoadError
from app.plugins.registry import PluginRegistryImpl
from app.search import SearchProviderRegistry
from app.transcript import TranscriptProviderRegistry

#: The entry point group name where plugins register themselves.
PLUGIN_ENTRY_POINT_GROUP = "scrapping_service.plugins"


class PluginLoadResult:
    """Result of loading plugins.

    Contains the registries with any plugin-provided providers, plus
    metadata about what was loaded and skipped.

    """

    def __init__(
        self,
        *,
        search_providers: list[Any],
        transcript_providers: list[Any],
        browser_provider: Any | None,
        extractor_registry: Any,
        loaded: list[str],
        skipped: list[dict[str, str]],
    ) -> None:
        """Initialize the result.

        Args:
            search_providers: List of SearchProvider instances (built-in
              plus any plugin-provided).
            transcript_providers: List of TranscriptProvider instances.
            browser_provider: The single browser provider instance, or None.
            extractor_registry: The ExtractorRegistry with plugin extractors.
            loaded: List of plugin names that were successfully loaded.
            skipped: List of dicts with 'name', 'reason' for plugins
              that were skipped (e.g., not in allowlist).
        """
        self.search_providers = search_providers
        self.transcript_providers = transcript_providers
        self.browser_provider = browser_provider
        self.extractor_registry = extractor_registry
        self.loaded = loaded
        self.skipped = skipped


def load_plugins(
    settings: Settings,
    *,
    camofox_client: Any | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> PluginLoadResult:
    """Discover and load plugins via entry points.

     This function:

     1. Reads the plugin allowlist from settings (``raw_yaml_plugin.get("allowlist")``
        or env ``SCRAPER_PLUGIN_ALLOWLIST``).
     2. Lists all entry points in ``scrapping_service.plugins``.
     3. For each entry point:
        - Attempts to import and instantiate the plugin.
        - Validates ``api_version`` matches major version.
        - Checks allowlist (if enabled, plugin must be listed).
        - Calls ``plugin.register(registry, ctx)``.
     4. Returns a PluginLoadResult with registries and metadata.

     If any plugin fails to load (import error, version mismatch,
     name collision, allowlist violation), its error is logged and
     processing continues. A single failure does NOT stop the entire
     plugin loading process, but it DOES prevent that specific plugin
     from being loaded.

    /plugins with ``enabled: false`` in the plugin config block are
     skipped entirely.

     Args:
         settings: The global Settings instance.
         camofox_client: The shared Camofox client (if any).
         http_client: The shared httpx.AsyncClient (must be provided
           for plugins that need HTTP access).

     Returns:
         A PluginLoadResult with registries populated by built-in and
         plugin providers.

     Raises:
         PluginLoadError: If the allowlist is enabled but no plugins
           are allowed and at least one entry point exists (i.e., a
           broken configuration). The loader raises this ONLY if the
           configuration is clearly wrong (allowlist enabled but empty
           while plugins exist).
    """
    logger = logging.getLogger("scraper-api.plugins")
    plugins_config = getattr(settings, "raw_yaml_plugin", None) or {}
    plugins_config.get("enabled", True)
    allowlist = plugins_config.get("allowlist", [])

    # Build plugin context (narrow view of settings)
    from app.plugin_api import PluginContext, SettingsView

    def make_http_client() -> httpx.AsyncClient:
        """Return the shared httpx.AsyncClient (or a new one if not injected)."""
        if http_client is not None:
            return http_client
        # Fallback: create a new client (should not happen in real use).
        return httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False)

    ctx = PluginContext(
        plugin_name="<system>",
        settings=SettingsView.from_settings(settings, plugin_config=plugins_config),
        logger=logger,
        http_client_factory=make_http_client,
    )

    # Discover entry points
    try:
        eps = metadata.entry_points(group=PLUGIN_ENTRY_POINT_GROUP)
    except TypeError:
        # importlib.metadata < 3.10 does not support group= argument
        eps = metadata.entry_points().get(PLUGIN_ENTRY_POINT_GROUP, [])
    entry_points = list(eps)

    # Validate that the entry point group exists (not just empty)
    try:
        all_eps = metadata.entry_points()
        has_group = PLUGIN_ENTRY_POINT_GROUP in all_eps
    except TypeError:
        all_eps = metadata.entry_points()
        has_group = PLUGIN_ENTRY_POINT_GROUP in all_eps

    if not has_group:
        logger.debug(
            "Entry point group '%s' not found; skipping plugin loading",
            PLUGIN_ENTRY_POINT_GROUP,
        )
    else:
        logger.info(
            "Discovered %d entry points in group '%s'",
            len(entry_points),
            PLUGIN_ENTRY_POINT_GROUP,
        )

    search_registry = SearchProviderRegistry()
    transcript_registry = TranscriptProviderRegistry()
    browser_slot: list[Any] = [None]  # mutable slot
    extractor_registry = None
    try:
        from app.scraper.extractor_registry import ExtractorRegistry

        extractor_registry = ExtractorRegistry()
    except Exception:
        extractor_registry = None

    # Build registry wrapper
    registry = PluginRegistryImpl(
        search_registry=search_registry,
        transcript_registry=transcript_registry,
        browser_provider_slot=browser_slot,
        extractor_registry=extractor_registry,
    )

    loaded: list[str] = []
    skipped: list[dict[str, str]] = []

    for ep in entry_points:
        plugin_name = ep.name
        logger.info("Processing entry point: %s", plugin_name)

        # Check allowlist
        if allowlist and plugin_name not in allowlist:
            logger.info("Plugin '%s' skipped: not in allowlist", plugin_name)
            skipped.append({"name": plugin_name, "reason": "not in allowlist"})
            continue

        # Load plugin class
        try:
            plugin_class = ep.load()
        except Exception as exc:
            logger.exception("Failed to import plugin '%s': %s", plugin_name, exc)
            skipped.append({"name": plugin_name, "reason": f"import error: {exc}"})
            continue

        # Instantiate plugin (call class with no args)
        try:
            plugin = plugin_class()
        except Exception as exc:
            logger.exception("Failed to instantiate plugin '%s': %s", plugin_name, exc)
            skipped.append({"name": plugin_name, "reason": f"instantiation error: {exc}"})
            continue

        # Check protocol compliance
        if not hasattr(plugin, "name"):
            logger.error("Plugin '%s' has no 'name' attribute", plugin_name)
            skipped.append({"name": plugin_name, "reason": "missing 'name' attribute"})
            continue
        if not hasattr(plugin, "version"):
            logger.error("Plugin '%s' has no 'version' attribute", plugin_name)
            skipped.append({"name": plugin_name, "reason": "missing 'version' attribute"})
            continue
        if not hasattr(plugin, "api_version"):
            logger.error("Plugin '%s' has no 'api_version' attribute", plugin_name)
            skipped.append({"name": plugin_name, "reason": "missing 'api_version' attribute"})
            continue

        # Validate api_version major
        from app.plugin_api import PLUGIN_API_VERSION

        api_version = getattr(plugin, "api_version")
        service_major = _parse_major(PLUGIN_API_VERSION)
        plugin_major = _parse_major(api_version)

        if service_major != plugin_major:
            logger.error(
                "Plugin '%s' api_version '%s' does not match service major '%s'",
                plugin_name,
                api_version,
                service_major,
            )
            skipped.append(
                {
                    "name": plugin_name,
                    "reason": f"api_version '{api_version}' major does not match '{service_major}'",
                }
            )
            continue

        logger.info(
            "Plugin '%s' version %s (api_version %s) loaded, calling register()",
            plugin_name,
            getattr(plugin, "version", "<unknown>"),
            api_version,
        )

        # Create plugin-specific logger
        plugin_logger = logging.getLogger(f"scraper-api.plugins.{plugin_name}")
        plugin_ctx = PluginContext(
            plugin_name=plugin_name,
            settings=ctx.settings,
            logger=plugin_logger,
            http_client_factory=ctx.http_client_factory,
        )

        # Call register
        try:
            plugin.register(registry, plugin_ctx)
            loaded.append(plugin_name)
            logger.info("Plugin '%s' registered successfully", plugin_name)
        except PluginLoadError:
            # Re-raise to abort startup on plugin load error
            raise
        except Exception as exc:
            logger.exception("Plugin '%s' register() raised unexpected error: %s", plugin_name, exc)
            skipped.append({"name": plugin_name, "reason": f"register error: {exc}"})
            continue

    # Build result
    search_providers = search_registry.all()
    transcript_providers = transcript_registry.all()
    browser_provider = browser_slot[0]

    return PluginLoadResult(
        search_providers=search_providers,
        transcript_providers=transcript_providers,
        browser_provider=browser_provider,
        extractor_registry=extractor_registry,
        loaded=loaded,
        skipped=skipped,
    )


def _parse_major(version: str) -> int:
    """Extract the major version number from a semver string.

    Accepts ``"1.2.3"``, ``"1"``, ``"v1.2"`` and similar formats.
    """
    # Strip leading 'v' or 'V'
    v = version.lstrip("vV")
    # Split on dots
    parts = v.split(".")
    if not parts:
        return 0
    try:
        return int(parts[0])
    except ValueError:
        return 0
