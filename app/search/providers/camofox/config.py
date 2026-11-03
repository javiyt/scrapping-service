"""Camofox search provider configuration.

Provider-level options live in the ``search`` section of the YAML config
file, in the provider block (following the SearXNG convention)::

    search:
      providers:
        camofox:
          enabled: true
          max_results: 20

Connection settings (``base_url``, keys, timeout, session prefix) live in
the top-level ``camofox`` section — they belong to the shared
:class:`app.camofox.CamofoxClient`, which is built at startup and injected
into this provider.  This block only carries options that are *search
provider* policy, not connection settings.

The shared Camofox section is parsed through
:meth:`CamofoxConfig.from_settings`; the client is *required* for this
provider, so enabling it without a usable client configuration is a startup
error (a misconfiguration, never a per-request surprise).

Following the project's configuration conventions, every value can be
overridden at runtime with ``SCRAPER_``-prefixed environment variables
(e.g. ``SCRAPER_SEARCH_CAMOFOX_MAX_RESULTS``); environment variables take
priority over YAML values, which take priority over defaults.  YAML string
values additionally support ``${VAR}`` expansion (handled by
:class:`app.core.config.Settings` before this module sees them).
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.camofox import CamofoxClient
from app.camofox.config import CamofoxConfig
from app.core.config import Settings

NAME = "camofox"

# Environment-variable prefix for the provider-level search options.
ENV_PREFIX = "SCRAPER_SEARCH_CAMOFOX_"

# Config keys in the order they appear in the YAML block (documented order).
_CONFIG_KEYS = (
    "enabled",
    "max_results",
)


class CamofoxSearchConfig(BaseModel):
    """Validated provider-level configuration for the Camofox search provider."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Enable the Camofox search provider. Disabled by default. The "
            "shared ``camofox`` section (client) must also be enabled."
        ),
    )
    max_results: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description=(
            "Optional documented cap on results per request. When set, a "
            "request asking for more results is degraded (``limit``) and "
            "truncated to this cap. ``None`` = no documented cap (the "
            "browser page simply yields whatever it yields)."
        ),
    )

    @model_validator(mode="after")
    def _check_max_results(self) -> CamofoxSearchConfig:
        if self.max_results is not None and not 1 <= self.max_results <= 100:
            raise ValueError("max_results must be between 1 and 100")
        return self

    @classmethod
    def from_settings(cls, settings: Settings) -> CamofoxSearchConfig:
        """Build a :class:`CamofoxSearchConfig` from application settings.

        Priority (highest to lowest):
          1. ``SCRAPER_SEARCH_CAMOFOX_*`` environment variables
          2. The ``search.providers.camofox`` YAML block
          3. Hard-coded defaults
        """
        raw_search: dict[str, Any] | None = getattr(settings, "raw_yaml_search", None)
        providers = raw_search.get("providers") if isinstance(raw_search, dict) else None
        block = providers.get(NAME) if isinstance(providers, dict) else None
        if block is None:
            block = {}
        if not isinstance(block, dict):
            raise ValueError(f"search.providers.{NAME} must be a mapping of configuration values")

        values: dict[str, Any] = {}
        for key in _CONFIG_KEYS:
            env_value = os.environ.get(ENV_PREFIX + key.upper())
            yaml_value = block.get(key)
            if env_value is not None:
                values[key] = env_value
            elif yaml_value is not None:
                values[key] = yaml_value
        return cls(**values)


def resolve_camofox_client(
    settings: Settings,
) -> tuple[CamofoxConfig, CamofoxClient | None]:
    """Resolve the shared Camofox client for the search provider.

    Returns the parsed :class:`CamofoxConfig` and a :class:`CamofoxClient`
    when the ``camofox`` section is enabled, else ``(config, None)``.

    Raises:
        ValueError: when the provider is enabled but the client cannot be
            constructed (the ``camofox`` section is disabled, or
            ``base_url`` is missing).
    """
    client_config = CamofoxConfig.from_settings(settings)
    if not client_config.enabled:
        if client_config.base_url:
            return client_config, CamofoxClient(client_config)
        return client_config, None
    if not client_config.base_url:
        raise ValueError(f"search provider '{NAME}' requires the camofox section with a base_url")
    return client_config, CamofoxClient(client_config)
