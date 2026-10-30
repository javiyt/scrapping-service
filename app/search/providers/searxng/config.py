"""SearXNG provider configuration.

Configuration lives in the ``search`` section of the YAML config file, in the
provider block::

    search:
      providers:
        searxng:
          enabled: true
          base_url: http://searxng:8080
          timeout_seconds: 15
          default_language: en
          config_cache_ttl_seconds: 3600

Following the project's configuration conventions, every value can be
overridden at runtime with ``SCRAPER_``-prefixed environment variables
(e.g. ``SCRAPER_SEARCH_SEARXNG_BASE_URL``); environment variables take
priority over YAML values, which take priority over defaults.  YAML string
values additionally support ``${VAR}`` expansion (handled by
:class:`app.core.config.Settings` before this module sees them).

The raw ``search`` block is carried on :class:`app.core.config.Settings` as
``raw_yaml_search`` (same convention as the ``auth`` block) so that
provider-specific names never leak into the generic settings model.
"""

from __future__ import annotations

import os
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import Settings

NAME = "searxng"

# Environment-variable prefix for SearXNG configuration overrides.
ENV_PREFIX = "SCRAPER_SEARCH_SEARXNG_"

_BASE_URL_PATTERN = re.compile(r"^https?://\S+$")
_LANGUAGE_PATTERN = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$")

# Config keys in the order they appear in the YAML block (documented order).
_CONFIG_KEYS = (
    "enabled",
    "base_url",
    "timeout_seconds",
    "default_language",
    "config_cache_ttl_seconds",
)


class SearxngConfig(BaseModel):
    """Validated configuration for the SearXNG search provider."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Enable the SearXNG provider. Disabled by default.",
    )
    base_url: str | None = Field(
        default=None,
        description=(
            "Base URL of the SearXNG instance, e.g. ``http://searxng:8080``. "
            "Required when the provider is enabled. The instance must have the "
            "JSON output format enabled (``search.formats: [html, json]``)."
        ),
    )
    timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        le=300,
        description="HTTP timeout in seconds for /config and /search requests.",
    )
    default_language: str | None = Field(
        default=None,
        description=(
            "Optional BCP-47 style language tag used when a search request "
            "does not specify one (e.g. ``en``, ``en-US``)."
        ),
    )
    config_cache_ttl_seconds: int = Field(
        default=3600,
        ge=0,
        description=(
            "TTL for the cached /config discovery (enabled categories and "
            "engines). ``0`` disables caching (re-discover on every use). "
            "A stale cache is served when a refresh fails."
        ),
    )

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if not _BASE_URL_PATTERN.match(v):
            raise ValueError(f"base_url must be an absolute http(s) URL, got {v!r}")
        return v

    @field_validator("default_language")
    @classmethod
    def _validate_language(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if not _LANGUAGE_PATTERN.match(v):
            raise ValueError(
                f"default_language must be a BCP-47 style tag (e.g. en, en-US), got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _check_enabled_requires_url(self) -> SearxngConfig:
        if self.enabled and self.base_url is None:
            raise ValueError("SearXNG is enabled but base_url is not configured")
        return self

    @classmethod
    def from_settings(cls, settings: Settings) -> SearxngConfig:
        """Build a :class:`SearxngConfig` from application settings.

        Priority (highest to lowest):
          1. ``SCRAPER_SEARCH_SEARXNG_*`` environment variables
          2. The ``search.providers.searxng`` YAML block
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
