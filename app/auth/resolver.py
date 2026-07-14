"""Profile-based API key resolution and effective settings computation."""

from __future__ import annotations

import copy
import hmac
import logging
import os
import re
from typing import Any

from app.auth.models import AuthContext, AuthProfile, AuthSettings
from app.core.config import Settings

logger = logging.getLogger("scraper-api.auth")

ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_env_var(value: str) -> str | None:
    """Resolve ``${VAR_NAME}`` inside a YAML string value.

    Returns the resolved string, or ``None`` if the variable is not set.
    Only supported for plain string values that contain a single env-var
    reference (with optional surrounding whitespace).
    """
    m = ENV_VAR_PATTERN.fullmatch(value.strip())
    if not m:
        return value
    var_name = m.group(1)
    resolved = os.environ.get(var_name)
    if resolved is None:
        return None
    return resolved


def constant_time_compare(a: str, b: str) -> bool:
    """Compare two strings in constant time to prevent timing attacks."""
    return hmac.compare_digest(a, b)


class ProfileResolver:
    """Resolves authentication tokens to named profiles and computes
    effective (profile-merged) settings for a request.

    This class is initialised once with global application settings and
    parses the ``auth`` config block from the YAML configuration file.

    API keys are stored internally as a list of (key, profile) pairs and
    compared in constant time to prevent timing-side-channel attacks.
    """

    def __init__(self, global_settings: Settings) -> None:
        self._global = global_settings
        self._profiles: dict[str, AuthProfile] = {}
        # List of (key, profile) pairs for constant-time comparison.
        self._key_entries: list[tuple[str, AuthProfile]] = []
        self._auth_settings = AuthSettings()
        self._expose_profile = False
        self._default_profile: str = "default"
        self._parse_auth_config(global_settings)

    def _parse_auth_config(self, settings: Settings) -> None:
        """Parse the ``auth`` block from the YAML config loaded into
        settings' ``_raw_yaml_auth`` attribute (if present)."""
        raw_auth: dict[str, Any] | None = getattr(settings, "raw_yaml_auth", None)
        if not raw_auth:
            # No auth block — single-key fallback.
            self._setup_single_key_fallback(settings)
            return

        self._default_profile = raw_auth.get("default_profile", "default")
        self._expose_profile = raw_auth.get("expose_profile_in_response", False)

        raw_keys: list[dict[str, Any]] = raw_auth.get("api_keys", [])
        if not raw_keys:
            self._setup_single_key_fallback(settings)
            return

        profiles: list[AuthProfile] = []
        warning_shown = False
        for entry in raw_keys:
            raw_key_value: str = entry.get("key", "")
            # Resolve environment variable references in the key value.
            resolved = _resolve_env_var(raw_key_value)
            if resolved is None:
                if not warning_shown:
                    logger.warning(
                        "Environment variable referenced in auth.api_keys "
                        "is not set — disabling profile %r",
                        entry.get("name", "<unnamed>"),
                    )
                    warning_shown = True
                # Skip profiles whose env var is missing.
                continue

            profile = AuthProfile(
                name=entry.get("name", "default"),
                key=resolved,
                description=entry.get("description"),
                enabled=entry.get("enabled", True),
                overrides=entry.get("overrides", {}),
            )
            profiles.append(profile)

        try:
            self._auth_settings = AuthSettings(
                default_profile=self._default_profile,
                expose_profile_in_response=self._expose_profile,
                api_keys=profiles,
            )
        except ValueError as exc:
            logger.error("Auth configuration error: %s", exc)
            raise

        # Build lookup maps from validated profiles — use constant-time entries.
        for p in self._auth_settings.api_keys:
            if p.enabled:
                self._key_entries.append((p.key, p))
                self._profiles[p.name] = p

        if not self._key_entries and settings.server_api_key_required:
            logger.error(
                "No valid API keys are configured but auth is required — "
                "the service will reject all requests"
            )

    def _setup_single_key_fallback(self, settings: Settings) -> None:
        """Set up a single 'default' profile from the legacy
        ``api_key`` setting."""
        key = settings.api_key
        if not key:
            logger.warning("No API key configured — auth is effectively disabled")
            return
        profile = AuthProfile(
            name="default",
            key=key,
            description="Default internal client (legacy single-key mode)",
            enabled=True,
            overrides={},
        )
        self._auth_settings = AuthSettings(
            default_profile="default",
            expose_profile_in_response=False,
            api_keys=[profile],
        )
        self._key_entries.append((profile.key, profile))
        self._profiles[profile.name] = profile

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def expose_profile_in_response(self) -> bool:
        """Whether the response should include the profile name."""
        return self._expose_profile

    @property
    def default_profile_name(self) -> str:
        """Default profile name used when no explicit profile is matched."""
        return self._default_profile

    def authenticate(self, token: str | None) -> AuthContext | None:
        """Authenticate a bearer token and return an :class:`AuthContext`.

        Uses constant-time comparison for API keys to prevent timing
        side-channel attacks.

        Returns ``None`` when the token is not recognised.
        """
        if token is None:
            return None
        for key, profile in self._key_entries:
            if constant_time_compare(token, key):
                return AuthContext(
                    profile_name=profile.name,
                    description=profile.description,
                )
        return None

    def get_profile(self, profile_name: str) -> AuthProfile | None:
        """Return the :class:`AuthProfile` for *profile_name*, or ``None``."""
        return self._profiles.get(profile_name)

    def effective_settings_for(self, profile_name: str | None = None) -> Settings:
        """Compute a new :class:`Settings` instance with profile overrides
        applied on top of the global defaults.

        The global :class:`Settings` is never mutated.

        Args:
            profile_name: The name of the profile whose overrides should
                be applied.  If ``None`` or unknown, global settings are
                returned unchanged.

        Returns:
            A new :class:`Settings` instance with profile overrides merged
            into the global settings.
        """
        if not profile_name:
            return self._global

        profile = self._profiles.get(profile_name)
        if profile is None or not profile.overrides:
            return self._global

        effective = self._deep_merge_settings(self._global, profile.overrides)
        return effective

    def _deep_merge_settings(self, base: Settings, overrides: dict[str, Any]) -> Settings:
        """Create a new Settings by deep-merging profile *overrides* on top
        of *base* settings.

        Only safe sections (cache, scraper, security, domains, debug,
        browser, jobs) are processed.  Global settings for unaffected
        fields are preserved verbatim.
        """
        merged = copy.deepcopy(base)

        for section, overrides_dict in overrides.items():
            if not isinstance(overrides_dict, dict):
                continue
            section_field_prefix = f"{section}_"
            for key, value in overrides_dict.items():
                field_name = f"{section_field_prefix}{key}"
                if hasattr(merged, field_name):
                    setattr(merged, field_name, value)

            # Special handling for domains: merge per-domain configs.
            if section == "domains":
                for domain, domain_cfg in overrides_dict.items():
                    if isinstance(domain_cfg, dict):
                        existing = dict(merged.domains.get(domain, {}))
                        existing.update(domain_cfg)
                        merged.domains[domain] = existing

        return merged


_RESOLVER: ProfileResolver | None = None


def get_profile_resolver() -> ProfileResolver:
    """Return the module-level :class:`ProfileResolver` singleton.

    On first call the resolver is auto-initialised from ``Settings.load()``
    so that tests and code paths that bypass the lifespan still work.
    """
    global _RESOLVER
    if _RESOLVER is None:
        from app.core.config import Settings

        _RESOLVER = ProfileResolver(Settings.load())
    return _RESOLVER


def init_profile_resolver(settings: Settings) -> ProfileResolver:
    """Initialise the global :class:`ProfileResolver` from *settings*."""
    global _RESOLVER
    _RESOLVER = ProfileResolver(settings)
    return _RESOLVER


def reset_profile_resolver() -> None:
    """Reset the global :class:`ProfileResolver` singleton.

    Used in tests to isolate resolver state between test cases.
    """
    global _RESOLVER
    _RESOLVER = None
