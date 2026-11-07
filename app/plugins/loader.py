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
import os
import re
from importlib import metadata
from typing import Any

import httpx

from app.core.config import Settings
from app.plugins.errors import PluginLoadError
from app.plugins.isolation import wrap_search_provider, wrap_transcript_provider
from app.plugins.registry import PluginRegistryImpl
from app.search import SearchProviderRegistry
from app.transcript import TranscriptProviderRegistry

#: The entry point group name where plugins register themselves.
PLUGIN_ENTRY_POINT_GROUP = "scrapping_service.plugins"

# Patterns to sanitize plugin failure reasons (strip paths, redact credentials)
# Reuse patterns from app.transcript.router.redact_secrets to avoid cycles
# If that import causes a cycle, move these patterns to app/core/security.py
_URL_USERINFO_PATTERN = re.compile(r"(?i)\b(https?://)([^/@\s]+)@")
_AUTHORIZATION_PATTERN = re.compile(r"(?i)\b(authorization)\s*[:=]\s*\S+(?:\s+\S+)?")
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer)\s+\S+")
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[-_ ]?key|access[-_ ]?key)\b"
    r"(\s*[:=]\s*)([^\s,;&'\"\\)]*)"
)


def sanitize_plugin_reason(reason: str) -> str:
    """Sanitize a plugin failure reason for safe exposure.

    Removes absolute file paths (replaces with <path>), redacts credential-like
    material (URLs, Authorization headers, bearer tokens, common secret keys),
    and truncates to a reasonable length to prevent information leakage.

    This helper is idempotent — applying it twice yields the same result.
    """
    # Truncate extremely long strings to avoid DoS
    if len(reason) > 1024:
        reason = reason[:1000] + "..."

    # Replace absolute paths (e.g., /Users/... or C:\...)
    reason = re.sub(r"(?i)(?:^|[\/\\])(?:Users|home|var|tmp|app|root|c:)\S+", "<path>", reason)
    # Catch relative paths starting with ./ or ../
    reason = re.sub(r"(?i)[\./]+\w+[\./\w]*\.py[\w:\s]*", "<path>", reason)

    # Credential redaction patterns
    reason = _URL_USERINFO_PATTERN.sub(r"\1", reason)
    reason = _AUTHORIZATION_PATTERN.sub(lambda m: f"{m.group(1).lower()} [REDACTED]", reason)
    reason = _BEARER_PATTERN.sub(lambda m: f"{m.group(1).lower()} [REDACTED]", reason)
    reason = _CREDENTIAL_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", reason)

    return reason.strip()


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
        failed: list[dict[str, str]] | None = None,
        enabled: list[str] | None = None,
        on_error: str = "fail",
        metadata: list[dict[str, Any]] | None = None,
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
            failed: List of dicts with 'name', 'reason' for plugins
              that failed to load and triggered on_error=fail.
            enabled: List of plugin names that were enabled (allowlist/denylist).
            on_error: The on_error policy ("fail" or "warn").
            metadata: List of dicts describing each discovered entry point
              with keys: name, version, api_version, service_api_version,
              status, reason (optional), extensions (optional dict of lists).
        """
        self.search_providers = search_providers
        self.transcript_providers = transcript_providers
        self.browser_provider = browser_provider
        self.extractor_registry = extractor_registry
        self.loaded = loaded
        self.skipped = skipped
        self.failed = failed or []
        self.enabled = enabled or []
        self.on_error = on_error
        self.metadata = metadata or []


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
    2. Reads ``SCRAPER_PLUGINS_ENABLED`` for explicit enable list.
    3. Parses ``SCRAPER_PLUGINS`` for implicitly enabled plugins.
    4. Reads ``SCRAPER_PLUGINS_DENY`` for denylist.
    5. Lists all entry points in ``scrapping_service.plugins``.
    6. For each entry point:
       - Checks enabled list (opt-in: none by default; allowlist or enable env adds).
       - Checks denylist (overrides all enables).
       - Attempts to import and instantiate the plugin.
       - Validates ``api_version`` matches major version.
       - Calls ``plugin.register(registry, ctx)``.
    7. Wraps plugin-provided providers with timeout and error mapping.
    8. Returns a PluginLoadResult with registries and metadata.

    For plugin failure:
    - In "warn" mode (default if ``SCRAPER_PLUGINS_ON_ERROR`` is not "fail"),
      failed plugins are logged and skipped; the service continues.
    - In "fail" mode, any plugin failure raises PluginLoadError and aborts
      startup.

    If any plugin fails to load (import error, version mismatch,
    name collision, allowlist violation), its error is logged and
    processing continues (unless on_error="fail"). A single failure does
    NOT stop the entire plugin loading process, but it DOES prevent that
    specific plugin from being loaded.

    Args:
        settings: The global Settings instance.
        camofox_client: The shared Camofox client (if any).
        http_client: The shared httpx.AsyncClient (must be provided
          for plugins that need HTTP access).

    Returns:
        A PluginLoadResult with registries populated by built-in and
        plugin providers.

    Raises:
        PluginLoadError: If on_error="fail" and at least one plugin
          failed to load.
    """
    logger = logging.getLogger("scraper-api.plugins")
    plugins_config = getattr(settings, "raw_yaml_plugin", None) or {}
    plugins_config.get("enabled", True)
    allowlist = plugins_config.get("allowlist", [])

    # Determine on_error policy
    # Environment variable takes precedence over raw_yaml_plugin config
    on_error_env = os.environ.get("SCRAPER_PLUGINS_ON_ERROR", "").strip().lower()
    if on_error_env in ("fail", "warn"):
        on_error = on_error_env
    else:
        # Fallback to raw_yaml_plugin config or default to "fail"
        on_error_config = plugins_config.get("on_error", "fail")
        on_error = on_error_config if on_error_config in ("fail", "warn") else "fail"

    # Build enabled list
    enabled_list: list[str] = []

    # If allowlist is defined and non-empty, only allowlisted plugins are enabled
    if allowlist:
        enabled_list = list(allowlist)
    else:
        # Empty or absent allowlist means opt-in: no plugins enabled by default
        enabled_list = []

    # Explicitly enabled via environment
    enabled_env = os.environ.get("SCRAPER_PLUGINS_ENABLED", "").strip()
    if enabled_env:
        import re as _re

        enabled_parts = _re.split(r"[,\s]+", enabled_env)
        enabled_parts = [p.strip() for p in enabled_parts if p.strip()]
        enabled_list = list(set(enabled_list) | set(enabled_parts))

    # Plugins from SCRAPER_PLUGINS spec are implicitly enabled
    from app.plugins.install import parse_requirements

    scraper_plugins_env = os.environ.get("SCRAPER_PLUGINS", "").strip()
    if scraper_plugins_env:
        parsed_specs = parse_requirements(scraper_plugins_env)
        for spec in parsed_specs:
            # Extract package name from spec
            import re as _re

            spec_name = _re.split(r"[=<>!~]", spec)[0].strip()
            # Normalize to hyphen-separated for comparison
            spec_name = spec_name.replace("_", "-")
            if spec_name and spec_name not in enabled_list:
                enabled_list.append(spec_name)

    # Denylist
    denylist = plugins_config.get("denylist", [])
    denylist_env = os.environ.get("SCRAPER_PLUGINS_DENY", "").strip()
    if denylist_env:
        import re as _re

        deny_parts = _re.split(r"[,\s]+", denylist_env)
        deny_parts = [p.strip() for p in deny_parts if p.strip()]
        denylist = list(set(denylist) | set(deny_parts))

    # Apply denylist: remove from enabled
    enabled_list = [name for name in enabled_list if name not in denylist]

    # Check for non-pinned specs warning
    if scraper_plugins_env:
        parsed_specs = parse_requirements(scraper_plugins_env)
        for spec in parsed_specs:
            if not any(c in spec for c in ["==", "!="]):
                logger.warning(
                    "SCRAPER_PLUGINS spec '%s' has no version pin; "
                    "recommend pinning to a specific hash for production",
                    spec,
                )

    # Build plugin-specific environment variables
    import re as _re

    def _get_per_plugin_env(plugin_name: str) -> dict[str, str]:
        """Extract env vars matching SCRAPER_PLUGIN_<NAME>_*."""
        per_plugin = {}
        prefix = f"SCRAPER_PLUGIN_{plugin_name.upper().replace('-', '_')}_"
        for key, value in os.environ.items():
            if key.startswith(prefix):
                # Extract suffix after prefix and normalize to lowercase
                suffix = key[len(prefix) :].lower()
                # Normalize to PEP503 (replace underscores with hyphens)
                normalized_suffix = suffix.replace("_", "-")
                per_plugin[normalized_suffix] = value
        return per_plugin

    # Build plugin context (narrow view of settings)
    from app.plugin_api import PluginContext, SettingsView

    def make_http_client() -> httpx.AsyncClient:
        """Return the shared httpx.AsyncClient (or a new one if not injected)."""
        if http_client is not None:
            return http_client
        # Fallback: create a new client (should not happen in real use).
        return httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False)

    # Build the system context with per-plugin env support
    # (per-plugin env vars are accessed via os.environ in the plugin loop)
    # system_context = PluginContext(...)

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

    # Collect metadata for each entry point (before processing)
    from app.plugin_api import PLUGIN_API_VERSION

    plugin_metadata: list[dict[str, Any]] = []
    for ep in entry_points:
        plugin_name = ep.name
        entry_point_metadata = {
            "name": plugin_name,
            "version": "<unknown>",
            "api_version": "<unknown>",
            "service_api_version": PLUGIN_API_VERSION,
            "status": "loaded",
            "reason": None,
            "extensions": {
                "search_providers": [],
                "transcript_providers": [],
                "browser_provider": None,
                "extractors": [],
            },
        }
        plugin_metadata.append(entry_point_metadata)

    loaded: list[str] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []

    for ep in entry_points:
        plugin_name = ep.name
        logger.info("Processing entry point: %s", plugin_name)

        # Check denylist FIRST (overrides allowlist)
        if plugin_name in denylist:
            logger.warning("Plugin '%s' skipped: explicitly denied via denylist", plugin_name)
            reason = sanitize_plugin_reason("explicitly denied via denylist")
            skipped.append({"name": plugin_name, "reason": reason})
            continue

        # Check enabled list
        if plugin_name not in enabled_list:
            logger.info("Plugin '%s' skipped: not enabled (opt-in allowlist)", plugin_name)
            reason = sanitize_plugin_reason("not enabled (opt-in allowlist)")
            skipped.append({"name": plugin_name, "reason": reason})
            continue

        # Load plugin class
        try:
            plugin_class = ep.load()
        except Exception as exc:
            logger.exception("Failed to import plugin '%s': %s", plugin_name, exc)
            failure_reason = sanitize_plugin_reason(f"ImportError: {exc}")
            skipped.append({"name": plugin_name, "reason": failure_reason})
            # Always record failures regardless of on_error mode
            failed.append({"name": plugin_name, "reason": failure_reason})
            if on_error == "fail":
                logger.error("Plugin load failure with on_error=fail, queuing PluginLoadError")
            continue

        # Instantiate plugin (call class with no args)
        try:
            plugin = plugin_class()
        except Exception as exc:
            logger.exception("Failed to instantiate plugin '%s': %s", plugin_name, exc)
            failure_reason = sanitize_plugin_reason(f"instantiation error: {exc}")
            skipped.append({"name": plugin_name, "reason": failure_reason})
            # Always record failures regardless of on_error mode
            failed.append({"name": plugin_name, "reason": failure_reason})
            if on_error == "fail":
                logger.error("Plugin load failure with on_error=fail, queuing PluginLoadError")
            continue

        # Check protocol compliance
        if not hasattr(plugin, "name"):
            logger.error("Plugin '%s' has no 'name' attribute", plugin_name)
            failure_reason = sanitize_plugin_reason("missing 'name' attribute")
            skipped.append({"name": plugin_name, "reason": failure_reason})
            # Always record failures regardless of on_error mode
            failed.append({"name": plugin_name, "reason": failure_reason})
            if on_error == "fail":
                logger.error("Plugin load failure with on_error=fail, queuing PluginLoadError")
            continue
        if not hasattr(plugin, "version"):
            logger.error("Plugin '%s' has no 'version' attribute", plugin_name)
            failure_reason = sanitize_plugin_reason("missing 'version' attribute")
            skipped.append({"name": plugin_name, "reason": failure_reason})
            # Always record failures regardless of on_error mode
            failed.append({"name": plugin_name, "reason": failure_reason})
            if on_error == "fail":
                logger.error("Plugin load failure with on_error=fail, queuing PluginLoadError")
            continue
        if not hasattr(plugin, "api_version"):
            logger.error("Plugin '%s' has no 'api_version' attribute", plugin_name)
            failure_reason = sanitize_plugin_reason("missing 'api_version' attribute")
            skipped.append({"name": plugin_name, "reason": failure_reason})
            # Always record failures regardless of on_error mode
            failed.append({"name": plugin_name, "reason": failure_reason})
            if on_error == "fail":
                logger.error("Plugin load failure with on_error=fail, queuing PluginLoadError")
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
            failure_reason = sanitize_plugin_reason(
                f"api_version '{api_version}' major does not match '{service_major}'"
            )
            skipped.append({"name": plugin_name, "reason": failure_reason})
            # Always record failures regardless of on_error mode
            failed.append({"name": plugin_name, "reason": failure_reason})
            if on_error == "fail":
                logger.error("Plugin load failure with on_error=fail, queuing PluginLoadError")
            continue

        logger.info(
            "Plugin '%s' version %s (api_version %s) loaded, calling register()",
            plugin_name,
            getattr(plugin, "version", "<unknown>"),
            api_version,
        )

        # Create plugin-specific logger
        plugin_logger = logging.getLogger(f"scraper-api.plugins.{plugin_name}")
        # Get settings with plugin-specific env
        plugin_settings = SettingsView.from_settings(
            settings,
            plugin_config=plugins_config,
        )
        # Build per-plugin environment for this specific plugin
        per_plugin_env = _get_per_plugin_env(plugin_name)
        plugin_ctx = PluginContext(
            plugin_name=plugin_name,
            settings=plugin_settings,
            logger=plugin_logger,
            http_client_factory=make_http_client,
            per_plugin_env=per_plugin_env,
        )

        # Call register
        try:
            plugin.register(registry, plugin_ctx)
            loaded.append(plugin_name)
            logger.info("Plugin '%s' registered successfully", plugin_name)
            # Update metadata with actual plugin attributes
            for entry in plugin_metadata:
                if entry["name"] == plugin_name:
                    entry["version"] = getattr(plugin, "version", "<unknown>")
                    entry["api_version"] = getattr(plugin, "api_version", "<unknown>")
                    break
        except PluginLoadError:
            # Re-raise to abort startup on plugin load error
            raise
        except Exception as exc:
            logger.exception("Plugin '%s' register() raised unexpected error: %s", plugin_name, exc)
            failure_reason = f"register error: {exc}"
            skipped.append({"name": plugin_name, "reason": failure_reason})
            # Always record failures regardless of on_error mode
            failed.append({"name": plugin_name, "reason": failure_reason})
            if on_error == "fail":
                logger.error("Plugin load failure with on_error=fail, queuing PluginLoadError")
            continue

    # Build result with wrap only plugin-provided providers
    search_providers = []
    for p in search_registry.all():
        if hasattr(p, "_plugin_name") and p._plugin_name:
            wrapped = wrap_search_provider(p, p._plugin_name, p.name)
            search_providers.append(wrapped)
            # Update metadata for this plugin's search provider
            for entry in plugin_metadata:
                if entry["name"] == p._plugin_name:
                    entry["extensions"]["search_providers"].append(p.name)
                    break
        else:
            search_providers.append(p)

    transcript_providers = []
    for p in transcript_registry.all():
        if hasattr(p, "_plugin_name") and p._plugin_name:
            wrapped = wrap_transcript_provider(p, p._plugin_name, p.name)
            transcript_providers.append(wrapped)
            # Update metadata for this plugin's transcript provider
            for entry in plugin_metadata:
                if entry["name"] == p._plugin_name:
                    entry["extensions"]["transcript_providers"].append(p.name)
                    break
        else:
            transcript_providers.append(p)

    browser_provider = browser_slot[0]
    # Update metadata for browser provider
    if browser_provider and hasattr(browser_provider, "_plugin_name"):
        for entry in plugin_metadata:
            if entry["name"] == browser_provider._plugin_name:
                entry["extensions"]["browser_provider"] = browser_provider.name
                break
    # Also check for extractor providers if any
    if extractor_registry:
        for ext in extractor_registry.get_all():
            plugin_name = getattr(ext, "_plugin_name", None)
            if plugin_name:
                for entry in plugin_metadata:
                    if entry["name"] == plugin_name:
                        entry["extensions"].setdefault("extractors", []).append(
                            ext.name if hasattr(ext, "name") else "unknown"
                        )
                        break

    # Update skipped/failed metadata with final status
    for entry in plugin_metadata:
        name = entry["name"]
        if name in loaded:
            entry["status"] = "loaded"
            entry["reason"] = None
        elif any(f.get("name") == name for f in failed):
            entry["status"] = "failed"
            reason = next(f["reason"] for f in failed if f.get("name") == name)
            entry["reason"] = reason
        elif any(s.get("name") == name for s in skipped):
            # Map skipped plugins to 'disabled' status for API consistency
            entry["status"] = "disabled"
            reason = next(s["reason"] for s in skipped if s.get("name") == name)
            entry["reason"] = reason
        else:
            # Not loaded but also not skipped/failed: e.g., import failed early
            entry["status"] = "failed"
            entry["reason"] = sanitize_plugin_reason("plugin not loaded")

    # Raise if any failures and on_error=fail
    if failed and on_error == "fail":
        reasons = ", ".join(f"{f['name']}: {f['reason']}" for f in failed)
        raise PluginLoadError(
            plugin_name="multiple",
            cause=f"one or more plugins failed to load ({reasons})",
        )

    return PluginLoadResult(
        search_providers=search_providers,
        transcript_providers=transcript_providers,
        browser_provider=browser_provider,
        extractor_registry=extractor_registry,
        loaded=loaded,
        skipped=skipped,
        failed=failed,
        enabled=enabled_list,
        on_error=on_error,
        metadata=plugin_metadata,
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
