"""Settings view passed to plugins during registration.

To satisfy the "narrow context" requirement of the plugin API, this module
defines a Pydantic model (:class:`SettingsView`) that is a read-only,
non-secret subset of the full :class:`app.core.config.Settings` and a
:class:`PluginContext` dataclass that bundles it with a logger and HTTP
client factory.

The view is constructed from the :class:`Settings` at load time and includes
only configuration keys that are safe for plugins to see (no API keys,
no auth block, no per-profile overrides — those are per-request and plugins
are loaded at startup, not per-request).

"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict


class SettingsView(BaseModel):
    """Read-only view of Settings for plugins.

    This model is constructed from :class:`app.core.config.Settings` and
    exposes only the non-sensitive configuration fields that a plugin might
    need to decide how to behave.

    Secrets are explicitly excluded:

    - ``api_key``, ``api_keys`` — never exposed.
    - ``raw_yaml_auth``, ``raw_yaml_search``, ``raw_yaml_camofox``,
      ``raw_yaml_transcript``, ``raw_yaml_browser``, ``raw_yaml_agent_browser``
      — raw YAML blocks are never exposed because they contain unvalidated
      user data that may include secrets.

    Plugins should treat the view as immutable and never attempt to modify it.

    The ``plugin`` field carries the plugin-specific YAML sub-block
    from the ``plugin:`` section of the config, allowing plugins to define
    their own options without polluting the global Settings.

    Example::

        from app.core.config import Settings
        from app.plugin_api import SettingsView

        global_settings = Settings.load()
        view = SettingsView.from_settings(global_settings, plugin_config={"foo": "bar"})

        # Safe to access — public fields
        mode = view.scraper_default_mode
        allowed = view.security_allowed_domains

        # Plugin-specific config
        custom = view.plugin.get("custom_setting")

    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Non-secret fields exposed by Settings. Mirrored from core/config.py.
    cache_sqlite_path: str
    log_level: str
    scraper_default_mode: str
    browser_primary_backend: str
    browser_fallback_backends: list[str]
    browser_max_pool_size: int
    security_allowed_domains: list[str]
    security_block_private_ips: bool
    security_block_localhost: bool
    agent_browser_enabled: bool
    agent_browser_allow_evaluate: bool
    agent_browser_max_sessions: int
    agent_browser_max_tabs_per_session: int
    agent_browser_session_ttl_seconds: int
    agent_browser_cleanup_interval_seconds: int
    agent_browser_operation_timeout_seconds: int
    proxy_enabled: bool
    proxy_url: str | None
    proxy_country: str | None
    proxy_allow_request_override: bool
    proxy_block_private_proxy_hosts: bool
    debug_screenshots: bool
    debug_html_dumps: bool
    debug_dir: str
    jobs_enabled: bool
    jobs_max_retained: int
    jobs_max_concurrency: int
    jobs_result_ttl_seconds: int
    health_max_consecutive_scrape_errors: int
    server_port: int
    server_api_key_required: bool
    server_cors_origins: list[str]

    # Plugin-specific YAML sub-block from the top-level "plugin" section.
    plugin: dict[str, Any]

    @classmethod
    def from_settings(
        cls, settings: Any, *, plugin_config: dict[str, Any] | None = None
    ) -> SettingsView:
        """Build a SettingsView from a full Settings instance.

        Args:
            settings: The full Settings instance (usually obtained via
              ``get_settings()`` in the lifespan).
            plugin_config: The raw plugin sub-block from the YAML
              ``plugin:`` section, or ``None`` if no plugin section exists.

        Returns:
            A frozen SettingsView instance containing only safe fields.

        Raises:
            ValueError: if ``settings`` is not a Settings instance or
              any field cannot be mapped.
        """
        # Map Settings fields to SettingsView fields.
        view_data: dict[str, Any] = {
            # Core
            "cache_sqlite_path": settings.cache_sqlite_path,
            "log_level": settings.log_level,
            # Scraper
            "scraper_default_mode": settings.scraper_default_mode,
            # Browser
            "browser_primary_backend": settings.browser_primary_backend,
            "browser_fallback_backends": list(settings.browser_fallback_backends),
            "browser_max_pool_size": settings.browser_max_pool_size,
            # Security
            "security_allowed_domains": list(settings.security_allowed_domains),
            "security_block_private_ips": settings.security_block_private_ips,
            "security_block_localhost": settings.security_block_localhost,
            # Agent browser
            "agent_browser_enabled": settings.agent_browser_enabled,
            "agent_browser_allow_evaluate": settings.agent_browser_allow_evaluate,
            "agent_browser_max_sessions": settings.agent_browser_max_sessions,
            "agent_browser_max_tabs_per_session": settings.agent_browser_max_tabs_per_session,
            "agent_browser_session_ttl_seconds": settings.agent_browser_session_ttl_seconds,
            "agent_browser_cleanup_interval_seconds": settings.agent_browser_cleanup_interval_seconds,  # noqa: E501
            "agent_browser_operation_timeout_seconds": settings.agent_browser_operation_timeout_seconds,  # noqa: E501
            # Proxy
            "proxy_enabled": settings.proxy_enabled,
            "proxy_url": settings.proxy_url,
            "proxy_country": settings.proxy_country,
            "proxy_allow_request_override": settings.proxy_allow_request_override,
            "proxy_block_private_proxy_hosts": settings.proxy_block_private_proxy_hosts,
            # Debug
            "debug_screenshots": settings.debug_screenshots,
            "debug_html_dumps": settings.debug_html_dumps,
            "debug_dir": settings.debug_dir,
            # Jobs
            "jobs_enabled": settings.jobs_enabled,
            "jobs_max_retained": settings.jobs_max_retained,
            "jobs_max_concurrency": settings.jobs_max_concurrency,
            "jobs_result_ttl_seconds": settings.jobs_result_ttl_seconds,
            "health_max_consecutive_scrape_errors": settings.health_max_consecutive_scrape_errors,
            # Server
            "server_port": settings.server_port,
            "server_api_key_required": settings.server_api_key_required,
            "server_cors_origins": list(settings.server_cors_origins),
            # Plugin-specific config
            "plugin": plugin_config or {},
        }
        return cls(**view_data)


@dataclass(frozen=True)
class PluginContext:
    """Read-only context passed to plugins during registration.

    This is the narrow context that plugins receive when their
    :meth:`Plugin.register` method is called. It provides:

    - ``plugin_name`` — the plugin's declared name (for logging).
    - ``settings`` — the :class:`SettingsView` subset (no secrets).
    - ``logger`` — a child logger ``scraper-api.plugins.<name>``.
    - ``http_client_factory`` — a callable that returns the shared
      :class:`httpx.AsyncClient` instance (closed at app shutdown).

    Plugins must not retain references to ``ctx`` after ``register`` returns.
    They should use ``logger`` for all logging instead of print statements.

    Example::

        from app.plugin_api import PluginContext, PluginRegistry

        def register(self, registry: PluginRegistry, ctx: PluginContext) -> None:
            ctx.logger.info("Registering plugin '%s' (version %s)", self.name, self.version)
            ctx.logger.debug("HTTP client available: %s", ctx.http_client_factory is not None)

    """

    plugin_name: str
    settings: SettingsView
    logger: Any  # logging.Logger
    http_client_factory: Callable[[], httpx.AsyncClient]
    per_plugin_env: dict[str, str] | None = None
    """Per-plugin environment variables (SCRAPER_PLUGIN_<NAME>_*)"""
