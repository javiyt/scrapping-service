"""Camofox REST client configuration.

The configuration lives in the top-level ``camofox`` section of the YAML
config file::

    camofox:
      enabled: true
      base_url: http://camofox:9377
      timeout_seconds: 60
      access_key: ${CAMOFOX_ACCESS_KEY}
      api_key: ${CAMOFOX_API_KEY}
      user_id_prefix: scrapping-service

Environment variables override YAML values and are loaded at construction
time (not import time), matching :mod:`app.auth` (where ``env_file="."`` is
applied): ``SCRAPER_CAMOFOX_ENABLED``, ``SCRAPER_CAMOFOX_BASE_URL``,
``SCRAPER_CAMOFOX_TIMEOUT_SECONDS``, ``SCRAPER_CAMOFOX_ACCESS_KEY``,
``SCRAPER_CAMOFOX_API_KEY``, ``SCRAPER_CAMOFOX_USER_ID_PREFIX``.

Secrets (``access_key`` / ``api_key``):

* Must not be literals in the repo — use ``${CAMOFOX_ACCESS_KEY}`` /
  ``${CAMOFOX_API_KEY}``` env-var references (or the direct env overrides).
* An unexpanded ``${VAR}`` placeholder (variable not in the environment) is
  treated as *unset* with a warning, never sent on the wire.
* They are excluded from every ``model_dump()`` and masked in ``repr()``.

Camofox is an **external** service (``camofox-browser``, default port 9377).
It is never embedded in or launched by this application — the client only
talks to it over HTTP.
"""

from __future__ import annotations

import os
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import ENV_VAR_PATTERN, Settings

__all__ = ["CamofoxConfig", "ENV_PREFIX", "NAME"]

NAME = "camofox"
ENV_PREFIX = "SCRAPER_CAMOFOX_"
_CONFIG_KEYS = ("enabled", "base_url", "timeout_seconds", "access_key", "api_key", "user_id_prefix")
_BASE_URL_PATTERN = re.compile(r"^https?://[^\s/][^\s]*$")


class CamofoxConfig(BaseModel):
    """Runtime configuration for the Camofox REST client."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Whether the Camofox client is constructed at startup.",
    )
    base_url: str | None = Field(
        default=None,
        description="Camofox REST base URL, e.g. http://camofox:9377.",
    )
    timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        le=3600,
        description="Per-request timeout for browser operations.",
    )
    access_key: str | None = Field(
        default=None,
        exclude=True,
        repr=False,
        description="General-purpose API key (used for most routes). Never serialized.",
    )
    api_key: str | None = Field(
        default=None,
        exclude=True,
        repr=False,
        description=(
            "Elevated API key (used for sensitive routes such as cookie import). Never serialized."
        ),
    )
    user_id_prefix: str = Field(
        default="scrapping-service",
        min_length=1,
        max_length=64,
        description="Prefix for generated Camofox userId values.",
    )

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().rstrip("/")
        if not v:
            return None
        if not _BASE_URL_PATTERN.match(v):
            raise ValueError("base_url must be an absolute http(s) URL")
        return v

    @field_validator("access_key", "api_key", mode="before")
    @classmethod
    def _validate_secret(cls, v: Any) -> str | None:
        if v is None:
            return None
        v = str(v).strip()
        if not v:
            return None
        if ENV_VAR_PATTERN.fullmatch(v):
            # Unexpanded ${VAR} placeholder — treat as unset, never send it.
            return None
        return v

    @field_validator("user_id_prefix")
    @classmethod
    def _validate_user_id_prefix(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("user_id_prefix must not be blank")
        return v

    @model_validator(mode="after")
    def _validate_enabled(self) -> CamofoxConfig:
        if self.enabled and not self.base_url:
            raise ValueError(f"{NAME} is enabled but base_url is not configured")
        return self

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"CamofoxConfig(enabled={self.enabled!r}, base_url={self.base_url!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"access_key={'*' * 8 if self.access_key else None!r}, "
            f"api_key={'*' * 8 if self.api_key else None!r}, "
            f"user_id_prefix={self.user_id_prefix!r})"
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> CamofoxConfig:
        """Build from a :class:`app.core.config.Settings` instance.

        YAML block wins by default, but env vars override YAML values when
        set (and non-empty).
        """
        raw = getattr(settings, "raw_yaml_camofox", None)
        block: dict[str, Any] = raw if isinstance(raw, dict) else {}

        values: dict[str, Any] = {}
        for key in _CONFIG_KEYS:
            env_value = os.environ.get(ENV_PREFIX + key.upper())
            yaml_value = block.get(key)
            if env_value is not None and str(env_value) != "":
                values[key] = env_value
            elif yaml_value is not None:
                values[key] = yaml_value

        return cls(**values)
