"""4get provider configuration.

Configuration lives in the ``search`` section of the YAML config file, in
the provider block (the YAML key is the public provider name ``4get``)::

    search:
      providers:
        4get:
          enabled: true
          base_url: http://4get:8080          # self-hosted instance
          timeout_seconds: 15
          pass_token: ${SCRAPER_4GET_PASS}    # secret, never logged
          scraper: duckduckgo                 # optional engine selection
          default_language: en
          default_region: us
          default_nsfw: false
          health_cache_ttl_seconds: 300

Following the project's configuration conventions, every value can be
overridden at runtime with ``SCRAPER_``-prefixed environment variables
(e.g. ``SCRAPER_SEARCH_4GET_BASE_URL``); environment variables take
priority over YAML values, which take priority over defaults.  YAML string
values additionally support ``${VAR}`` expansion (handled by
:class:`app.core.config.Settings` before this module sees them).

The provider is **disabled by default**: 4get's API terms forbid intensive
scraping and non-human automated traffic, and public instances (4get.ca)
must not receive production traffic.  Self-host an instance
(https://git.lolcat.ca/lolcat/4get) and rely on the generic SearchCache.

``pass_token`` is a secret: it is excluded from every ``model_dump()``,
masked in ``repr()``, and never appears in logs, errors, or diagnostics.
"""

from __future__ import annotations

import os
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import ENV_VAR_PATTERN, Settings
from app.search.providers.fourget.mapping import NAME

# Environment-variable prefix for 4get configuration overrides.
ENV_PREFIX = "SCRAPER_SEARCH_4GET_"

_BASE_URL_PATTERN = re.compile(r"^https?://\S+$")
_LANGUAGE_PATTERN = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$")
_REGION_PATTERN = re.compile(r"^[a-zA-Z]{2,3}$")

# Config keys in the order they appear in the YAML block (documented order).
_CONFIG_KEYS = (
    "enabled",
    "base_url",
    "timeout_seconds",
    "pass_token",
    "scraper",
    "default_language",
    "default_region",
    "default_nsfw",
    "health_cache_ttl_seconds",
)


class FourgetConfig(BaseModel):
    """Validated configuration for the 4get search provider."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Enable the 4get provider. Disabled by default (4get's terms "
            "forbid intensive automated traffic; self-host an instance)."
        ),
    )
    base_url: str | None = Field(
        default=None,
        description=(
            "Base URL of the 4get instance, e.g. ``http://4get:8080``. "
            "Required when the provider is enabled. Point this at a "
            "self-hosted instance, not at 4get.ca."
        ),
    )
    timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        le=300,
        description="HTTP timeout in seconds for /ami4get and /api/v1/* requests.",
    )
    pass_token: str | None = Field(
        default=None,
        exclude=True,
        repr=False,
        description=(
            "Optional instance pass (captcha-derived cookie value) sent as "
            "the ``pass`` cookie on every request. Secret: never logged, "
            "serialized, or returned in errors/diagnostics."
        ),
    )
    scraper: str | None = Field(
        default=None,
        max_length=100,
        description=(
            "Optional default engine (scraper) selection sent as the "
            "``scraper`` request parameter. Values are instance-defined "
            "(e.g. ``duckduckgo``, ``brave``). A request ``sources`` entry "
            "overrides this."
        ),
    )
    default_language: str | None = Field(
        default=None,
        description=(
            "Optional BCP-47 style language tag sent as ``lang`` when a "
            "search request does not specify one (e.g. ``en``, ``en-US``)."
        ),
    )
    default_region: str | None = Field(
        default=None,
        description=("Optional region (country code) sent as ``country``, e.g. ``us``, ``br``."),
    )
    default_nsfw: bool = Field(
        default=False,
        description=(
            "Instance NSFW default applied at the *moderate* safe-search "
            "level: ``True`` sends ``nsfw=1`` for moderate requests, "
            "``False`` sends ``nsfw=0``. ``off`` always sends ``nsfw=1``, "
            "``strict`` always sends ``nsfw=0``."
        ),
    )
    health_cache_ttl_seconds: int = Field(
        default=300,
        ge=0,
        description=(
            "TTL for the cached /ami4get health probe. ``0`` re-probes on "
            "every capabilities() call."
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

    @field_validator("pass_token", mode="before")
    @classmethod
    def _validate_pass_token(cls, v: Any) -> str | None:
        if v is None:
            return None
        v = str(v).strip()
        if not v:
            return None
        if ENV_VAR_PATTERN.fullmatch(v):
            # Unexpanded ${VAR} placeholder — treat as unset, never send it.
            return None
        return v

    @field_validator("scraper")
    @classmethod
    def _validate_scraper(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return v.strip()

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

    @field_validator("default_region")
    @classmethod
    def _validate_region(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip().lower()
        if not _REGION_PATTERN.match(v):
            raise ValueError(f"default_region must be a short country code (e.g. us), got {v!r}")
        return v

    @model_validator(mode="after")
    def _check_enabled_requires_url(self) -> FourgetConfig:
        if self.enabled and self.base_url is None:
            raise ValueError("4get is enabled but base_url is not configured")
        return self

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"FourgetConfig(enabled={self.enabled!r}, base_url={self.base_url!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"pass_token={'*' * 8 if self.pass_token else None!r}, "
            f"scraper={self.scraper!r}, default_language={self.default_language!r}, "
            f"default_region={self.default_region!r}, default_nsfw={self.default_nsfw!r}, "
            f"health_cache_ttl_seconds={self.health_cache_ttl_seconds!r})"
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> FourgetConfig:
        """Build a :class:`FourgetConfig` from application settings.

        Priority (highest to lowest):
          1. ``SCRAPER_SEARCH_4GET_*`` environment variables
          2. The ``search.providers.4get`` YAML block
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
