"""Data models for profile-aware API key authentication."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

PROFILE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Sections that profiles are allowed to override.
SAFE_OVERRIDE_SECTIONS = frozenset(
    {
        "cache",
        "scraper",
        "security",
        "domains",
        "debug",
        "browser",
        "jobs",
    }
)

# Sections that must never be overridden by profiles.
FORBIDDEN_OVERRIDE_SECTIONS = frozenset(
    {
        "server",
        "auth",
        "config_path",
        "log_level",
    }
)


class AuthProfile(BaseModel):
    """A named API key with its own profile of configuration overrides."""

    name: str
    key: str
    description: str | None = None
    enabled: bool = True
    overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not PROFILE_NAME_PATTERN.match(v):
            raise ValueError(f"Profile name {v!r} must match ^[a-zA-Z0-9_-]{{1,64}}$")
        return v

    @field_validator("key")
    @classmethod
    def _validate_key(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("API key must not be empty")
        return v

    @field_validator("overrides")
    @classmethod
    def _validate_overrides(cls, v: dict[str, Any]) -> dict[str, Any]:
        forbidden = set(FORBIDDEN_OVERRIDE_SECTIONS)
        unknown = set(v.keys()) - SAFE_OVERRIDE_SECTIONS - forbidden
        if unknown:
            raise ValueError(
                f"Unknown or forbidden override section(s): {', '.join(sorted(unknown))}. "
                f"Allowed sections: {', '.join(sorted(SAFE_OVERRIDE_SECTIONS))}"
            )
        overlap = set(v.keys()) & forbidden
        if overlap:
            raise ValueError(
                f"Profile must not override forbidden section(s): {', '.join(sorted(overlap))}"
            )
        return v


class AuthSettings(BaseModel):
    """Top-level auth configuration block."""

    default_profile: str = "default"
    expose_profile_in_response: bool = False
    api_keys: list[AuthProfile] = Field(default_factory=list)

    @field_validator("api_keys")
    @classmethod
    def _validate_api_keys(cls, v: list[AuthProfile]) -> list[AuthProfile]:
        names: set[str] = set()
        keys: set[str] = set()
        for profile in v:
            if profile.name in names:
                raise ValueError(f"Duplicate profile name: {profile.name!r}")
            names.add(profile.name)
            if profile.key in keys:
                raise ValueError(f"Duplicate API key for profile {profile.name!r}")
            keys.add(profile.key)
        return v


class AuthContext(BaseModel):
    """Resolved authentication context available per request.

    This is stored on ``request.state.auth_context`` and can be retrieved
    by route handlers.  It never includes the raw API key.
    """

    profile_name: str
    description: str | None = None
