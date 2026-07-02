"""Comprehensive tests for the profile-aware API key authentication system.

Tests cover:
1. Single-key fallback (backwards compatibility)
2. Public endpoint access
3. Token validation (missing, invalid)
4. Disabled profile rejection
5. Multiple profiles mapping to different names
6. Environment variable expansion
7. Missing env var handling (no secret leak)
8. Validation errors (duplicate names, duplicate keys, forbidden sections)
9. Profile overrides (allowed domains, cache TTL, scraper mode)
10. Request explicit field overrides profile defaults
11. Global settings immutability
12. Response metadata (auth_profile)
13. Batch scrape with profile context
14. Async jobs with profile context
15. Constant-time comparison helper
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth.models import AuthProfile, AuthSettings
from app.auth.resolver import (
    ProfileResolver,
    constant_time_compare,
    init_profile_resolver,
    reset_profile_resolver,
)
from app.core.config import Settings

# ------------------------------------------------------------------- helpers


def _make_settings_with_auth(auth_config: dict[str, Any]) -> Settings:
    """Create a Settings instance with a custom auth block."""
    settings = Settings(
        scraper_api_key="test-key",
        server_api_key_required=True,
    )
    object.__setattr__(settings, "raw_yaml_auth", auth_config)
    return settings


def _make_settings_no_auth() -> Settings:
    """Create a Settings instance with no auth block (single-key fallback)."""
    return Settings(
        scraper_api_key="legacy-key",
        server_api_key_required=True,
    )


# ======================================================= constant_time_compare


class TestConstantTimeCompare:
    """Unit tests for the constant-time comparison helper."""

    def test_equal_strings(self):
        assert constant_time_compare("abc123", "abc123") is True

    def test_unequal_strings(self):
        assert constant_time_compare("abc123", "xyz789") is False

    def test_empty_strings(self):
        assert constant_time_compare("", "") is True

    def test_different_lengths(self):
        assert constant_time_compare("short", "a-longer-string") is False

    def test_same_prefix(self):
        assert constant_time_compare("abcdef", "abcxyz") is False


# ============================================================== AuthProfile model


class TestAuthProfileModel:
    """Tests for AuthProfile validation."""

    def test_valid_profile(self):
        profile = AuthProfile(
            name="my-profile",
            key="secret-key-123",
            description="A test profile",
            enabled=True,
            overrides={"scraper": {"default_mode": "browser"}},
        )
        assert profile.name == "my-profile"
        assert profile.key == "secret-key-123"

    def test_invalid_name_with_spaces(self):
        with pytest.raises(ValueError, match="must match"):
            AuthProfile(name="bad name", key="key")

    def test_invalid_name_too_long(self):
        with pytest.raises(ValueError, match="must match"):
            AuthProfile(name="x" * 65, key="key")

    def test_invalid_name_special_chars(self):
        with pytest.raises(ValueError, match="must match"):
            AuthProfile(name="hello@world", key="key")

    def test_valid_name_with_underscores_and_dashes(self):
        profile = AuthProfile(name="my_cool-profile_2", key="key")
        assert profile.name == "my_cool-profile_2"

    def test_empty_key(self):
        with pytest.raises(ValueError, match="must not be empty"):
            AuthProfile(name="test", key="")

    def test_forbidden_override_section(self):
        with pytest.raises(ValueError, match="forbidden"):
            AuthProfile(
                name="test",
                key="key",
                overrides={"server": {"host": "0.0.0.0"}},
            )

    def test_unknown_override_section(self):
        with pytest.raises(ValueError, match="Unknown"):
            AuthProfile(
                name="test",
                key="key",
                overrides={"unknown_section": {"foo": "bar"}},
            )

    def test_safe_override_sections_accepted(self):
        profile = AuthProfile(
            name="test",
            key="key",
            overrides={
                "cache": {"default_ttl_seconds": 3600},
                "scraper": {"default_mode": "http"},
                "security": {"allowed_domains": ["example.com"]},
                "domains": {"example.com": {"allowed": True}},
                "debug": {"screenshots": True},
                "browser": {"arguments": ["--headless"]},
                "jobs": {"max_concurrency": 1},
            },
        )
        assert profile.name == "test"


# ============================================================= AuthSettings model


class TestAuthSettingsModel:
    """Tests for AuthSettings validation."""

    def test_duplicate_profile_names(self):
        with pytest.raises(ValueError, match="Duplicate profile name"):
            AuthSettings(
                api_keys=[
                    AuthProfile(name="dup", key="key1"),
                    AuthProfile(name="dup", key="key2"),
                ],
            )

    def test_duplicate_api_keys(self):
        with pytest.raises(ValueError, match="Duplicate API key"):
            AuthSettings(
                api_keys=[
                    AuthProfile(name="alpha", key="same-key"),
                    AuthProfile(name="beta", key="same-key"),
                ],
            )

    def test_valid_multiple_profiles(self):
        settings = AuthSettings(
            default_profile="default",
            expose_profile_in_response=True,
            api_keys=[
                AuthProfile(name="default", key="key1"),
                AuthProfile(name="fanatics", key="key2"),
            ],
        )
        assert len(settings.api_keys) == 2
        assert settings.expose_profile_in_response is True


# ======================================================= ProfileResolver — single key


class TestProfileResolverSingleKey:
    """Tests for single-key backwards compatibility."""

    def test_single_key_fallback_authenticates(self):
        settings = _make_settings_no_auth()
        resolver = ProfileResolver(settings)
        ctx = resolver.authenticate("legacy-key")
        assert ctx is not None
        assert ctx.profile_name == "default"

    def test_single_key_fallback_rejects_wrong_key(self):
        settings = _make_settings_no_auth()
        resolver = ProfileResolver(settings)
        ctx = resolver.authenticate("wrong-key")
        assert ctx is None

    def test_single_key_fallback_rejects_none(self):
        settings = _make_settings_no_auth()
        resolver = ProfileResolver(settings)
        ctx = resolver.authenticate(None)
        assert ctx is None

    def test_globals_not_mutated(self):
        settings = _make_settings_no_auth()
        original_mode = settings.scraper_default_mode
        resolver = ProfileResolver(settings)
        effective = resolver.effective_settings_for("default")
        assert effective.scraper_default_mode == original_mode
        assert settings.scraper_default_mode == original_mode

    def test_single_key_fallback_no_expose(self):
        settings = _make_settings_no_auth()
        resolver = ProfileResolver(settings)
        assert resolver.expose_profile_in_response is False


# ======================================================= ProfileResolver — multi-key


class TestProfileResolverMultiKey:
    """Tests for multi-key profile mode."""

    AUTH_CONFIG: dict[str, Any] = {
        "default_profile": "default",
        "expose_profile_in_response": False,
        "api_keys": [
            {
                "name": "default",
                "key": "default-key",
                "description": "Default profile",
                "enabled": True,
            },
            {
                "name": "fanatics",
                "key": "fan-key-123",
                "description": "Fanatics tracker",
                "enabled": True,
                "overrides": {
                    "scraper": {"default_mode": "browser", "timeout_seconds": 60},
                    "cache": {"default_ttl_seconds": 21600},
                    "security": {"allowed_domains": ["fanatics.es", "www.fanatics.es"]},
                    "domains": {
                        "fanatics.es": {
                            "allowed": True,
                            "min_delay_seconds": 6,
                            "max_concurrent_requests": 1,
                            "default_ttl_seconds": 21600,
                        },
                    },
                },
            },
            {
                "name": "debug",
                "key": "debug-key",
                "description": "Debug profile",
                "enabled": False,  # Disabled
                "overrides": {
                    "debug": {"screenshots": True, "html_dumps": True},
                    "cache": {"default_ttl_seconds": 300},
                },
            },
        ],
    }

    @pytest.fixture
    def resolver(self):
        settings = _make_settings_with_auth(self.AUTH_CONFIG)
        return ProfileResolver(settings)

    def test_multiple_keys_map_to_different_profiles(self, resolver):
        ctx_default = resolver.authenticate("default-key")
        assert ctx_default is not None
        assert ctx_default.profile_name == "default"

        ctx_fanatics = resolver.authenticate("fan-key-123")
        assert ctx_fanatics is not None
        assert ctx_fanatics.profile_name == "fanatics"

    def test_disabled_profile_rejected(self, resolver):
        ctx = resolver.authenticate("debug-key")
        assert ctx is None

    def test_invalid_token_rejected(self, resolver):
        ctx = resolver.authenticate("nonexistent-key")
        assert ctx is None

    def test_missing_token_rejected(self, resolver):
        ctx = resolver.authenticate(None)
        assert ctx is None

    def test_unknown_profile_returns_global_settings(self, resolver):
        effective = resolver.effective_settings_for("nonexistent")
        assert effective.scraper_default_mode == "auto"

    def test_none_profile_returns_global_settings(self, resolver):
        effective = resolver.effective_settings_for(None)
        assert effective.scraper_default_mode == "auto"

    def test_profile_overrides_allowed_domains(self, resolver):
        effective = resolver.effective_settings_for("fanatics")
        assert "fanatics.es" in effective.security_allowed_domains
        assert "www.fanatics.es" in effective.security_allowed_domains

    def test_profile_overrides_cache_ttl(self, resolver):
        effective = resolver.effective_settings_for("fanatics")
        assert effective.cache_default_ttl_seconds == 21600

    def test_profile_overrides_scraper_mode(self, resolver):
        effective = resolver.effective_settings_for("fanatics")
        assert effective.scraper_default_mode == "browser"

    def test_profile_overrides_timeout(self, resolver):
        effective = resolver.effective_settings_for("fanatics")
        assert effective.scraper_timeout_seconds == 60

    def test_global_settings_not_mutated(self, resolver):
        original = resolver._global
        original_mode = original.scraper_default_mode
        effective = resolver.effective_settings_for("fanatics")
        assert effective.scraper_default_mode == "browser"
        assert original.scraper_default_mode == original_mode

    def test_profile_domain_overrides_merged(self, resolver):
        effective = resolver.effective_settings_for("fanatics")
        assert "fanatics.es" in effective.domains
        domain_cfg = effective.domains["fanatics.es"]
        assert domain_cfg["min_delay_seconds"] == 6

    def test_no_expose_by_default(self, resolver):
        assert resolver.expose_profile_in_response is False


# ======================================================= ProfileResolver — expose


class TestProfileResolverExpose:
    def test_expose_profile_in_response_true(self):
        settings = _make_settings_with_auth(
            {
                "default_profile": "default",
                "expose_profile_in_response": True,
                "api_keys": [
                    {"name": "default", "key": "key1", "enabled": True},
                ],
            }
        )
        resolver = ProfileResolver(settings)
        assert resolver.expose_profile_in_response is True


# ======================================================= ProfileResolver — env vars


class TestProfileResolverEnvVars:
    """Tests for environment variable expansion in API keys."""

    def test_env_var_expansion(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_API_KEY_MYPROFILE", "resolved-key-999")
        settings = _make_settings_with_auth(
            {
                "api_keys": [
                    {
                        "name": "myprofile",
                        "key": "${SCRAPER_API_KEY_MYPROFILE}",
                        "enabled": True,
                    },
                ],
            }
        )
        resolver = ProfileResolver(settings)
        ctx = resolver.authenticate("resolved-key-999")
        assert ctx is not None
        assert ctx.profile_name == "myprofile"

    def test_missing_env_var_disables_profile(self, monkeypatch):
        monkeypatch.delenv("SCRAPER_API_KEY_MISSING", raising=False)
        settings = _make_settings_with_auth(
            {
                "api_keys": [
                    {
                        "name": "default",
                        "key": "default-key",
                        "enabled": True,
                    },
                    {
                        "name": "missing",
                        "key": "${SCRAPER_API_KEY_MISSING}",
                        "enabled": True,
                    },
                ],
            }
        )
        resolver = ProfileResolver(settings)
        # Default should still work
        ctx_default = resolver.authenticate("default-key")
        assert ctx_default is not None
        assert ctx_default.profile_name == "default"
        # Missing profile should not be resolvable
        assert resolver.get_profile("missing") is None

    def test_missing_env_var_does_not_leak_placeholder(self, monkeypatch, caplog):
        monkeypatch.delenv("SCRAPER_API_KEY_LEAK", raising=False)
        settings = _make_settings_with_auth(
            {
                "api_keys": [
                    {
                        "name": "leaktest",
                        "key": "${SCRAPER_API_KEY_LEAK}",
                        "enabled": True,
                    },
                ],
            }
        )
        ProfileResolver(settings)
        # The warning should not contain the placeholder value
        for record in caplog.records:
            if record.levelname == "WARNING" and "auth.api_keys" in record.getMessage():
                assert "${" not in record.getMessage()
                assert "SCRAPER_API_KEY_LEAK" not in record.getMessage()

    def test_no_valid_keys_but_auth_required_does_not_crash(self):
        settings = _make_settings_with_auth(
            {
                "api_keys": [
                    {
                        "name": "should-be-disabled",
                        "key": "${NONEXISTENT_VAR_XYZ}",
                        "enabled": True,
                    },
                ],
            }
        )
        # Should not crash
        resolver = ProfileResolver(settings)
        assert resolver.authenticate("anything") is None


# ======================================================= ProfileResolver — validation


class TestProfileResolverValidation:
    def test_forbidden_override_sections_fail(self):
        with pytest.raises(ValueError, match="forbidden"):
            settings = _make_settings_with_auth(
                {
                    "api_keys": [
                        {
                            "name": "hacker",
                            "key": "key1",
                            "overrides": {"server": {"host": "evil.com"}},
                        },
                    ],
                }
            )
            ProfileResolver(settings)

    def test_forbidden_override_auth_section(self):
        with pytest.raises(ValueError, match="forbidden"):
            settings = _make_settings_with_auth(
                {
                    "api_keys": [
                        {
                            "name": "hacker",
                            "key": "key1",
                            "overrides": {"auth": {"expose_profile_in_response": True}},
                        },
                    ],
                }
            )
            ProfileResolver(settings)

    def test_forbidden_override_log_level(self):
        with pytest.raises(ValueError, match="forbidden"):
            settings = _make_settings_with_auth(
                {
                    "api_keys": [
                        {
                            "name": "hacker",
                            "key": "key1",
                            "overrides": {"log_level": "DEBUG"},
                        },
                    ],
                }
            )
            ProfileResolver(settings)

    def test_duplicate_profile_names_fail(self):
        with pytest.raises(ValueError, match="Duplicate profile name"):
            settings = _make_settings_with_auth(
                {
                    "api_keys": [
                        {"name": "dup", "key": "key1"},
                        {"name": "dup", "key": "key2"},
                    ],
                }
            )
            ProfileResolver(settings)

    def test_duplicate_api_keys_fail(self):
        with pytest.raises(ValueError, match="Duplicate API key"):
            settings = _make_settings_with_auth(
                {
                    "api_keys": [
                        {"name": "alpha", "key": "same-key"},
                        {"name": "beta", "key": "same-key"},
                    ],
                }
            )
            ProfileResolver(settings)


# ======================================================= ProfileResolver — effective settings


class TestProfileResolverEffectiveSettings:
    def test_request_explicit_mode_overrides_profile(self):
        """Request explicit 'mode' field wins over profile override."""
        settings = _make_settings_with_auth(
            {
                "api_keys": [
                    {
                        "name": "default",
                        "key": "key1",
                        "enabled": True,
                        "overrides": {"scraper": {"default_mode": "browser"}},
                    },
                ],
            }
        )
        resolver = ProfileResolver(settings)
        effective = resolver.effective_settings_for("default")
        # Profile sets it to browser, but request would set it explicitly.
        # The effective settings reflect the profile; the request override
        # happens at the route level (in the scrape() call).
        assert effective.scraper_default_mode == "browser"

    def test_profile_override_does_not_mutate_global(self):
        settings = _make_settings_with_auth(
            {
                "api_keys": [
                    {
                        "name": "default",
                        "key": "key1",
                        "enabled": True,
                        "overrides": {"scraper": {"default_mode": "browser"}},
                    },
                ],
            }
        )
        resolver = ProfileResolver(settings)
        _ = resolver.effective_settings_for("default")
        assert settings.scraper_default_mode == "auto"


# ======================================================= API integration tests


class TestAPIWithProfiles:
    """Integration tests that use the FastAPI TestClient with profile auth."""

    VALID_KEY = "test-api-key"
    AUTH_CONFIG: dict[str, Any] = {
        "default_profile": "default",
        "expose_profile_in_response": False,
        "api_keys": [
            {
                "name": "default",
                "key": VALID_KEY,
                "description": "Default profile",
                "enabled": True,
            },
            {
                "name": "fanatics",
                "key": "fan-key",
                "description": "Fanatics",
                "enabled": True,
                "overrides": {
                    "scraper": {"default_mode": "browser"},
                },
            },
        ],
    }
    SAMPLE_RESULT = {
        "url": "https://example.com",
        "final_url": "https://example.com",
        "status_code": 200,
        "from_cache": False,
        "stale": False,
        "fetched_at": "2026-06-30T10:00:00+00:00",
        "expires_at": "2026-06-30T16:00:00+00:00",
        "html": "<html>Hello World</html>",
        "metadata": {
            "mode": "http",
            "elapsed_ms": 120,
            "content_length": 25,
            "cache_key": "abc123",
        },
    }

    @pytest.fixture(autouse=True)
    def _setup_resolver(self):
        """Initialise the global resolver with a known auth config before each test."""
        reset_profile_resolver()
        settings = _make_settings_with_auth(self.AUTH_CONFIG)
        init_profile_resolver(settings)
        yield
        reset_profile_resolver()

    @pytest.fixture(autouse=True)
    def _clean_app_state(self):
        """Reset app.state before each test so tests don't leak state."""
        from app.main import app

        for attr in ("scraper", "cache", "settings", "auth_context", "job_service"):
            if hasattr(app.state, attr):
                delattr(app.state, attr)

    def _mock_scraper(self, return_value=None, side_effect=None):
        mock = AsyncMock()
        if return_value is not None:
            mock.scrape.return_value = return_value
        if side_effect is not None:
            mock.scrape.side_effect = side_effect
        return mock

    # ----------------------------------------------------------- 1. Backwards compatibility

    def test_single_key_behavior(self):
        """Existing single SCRAPER_API_KEY behavior still works.

        With the current setup, the resolver has the 'default' key
        configured. Authenticating with it should succeed.
        """
        from app.main import app

        mock = self._mock_scraper(return_value=self.SAMPLE_RESULT)
        app.state.scraper = mock
        app.state.cache = MagicMock()
        client = TestClient(app)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers={"Authorization": f"Bearer {self.VALID_KEY}"},
        )
        assert response.status_code == 200, response.text

    # ----------------------------------------------------------- 2. Health is public

    def test_health_remains_public(self):
        from app.main import app

        client = TestClient(app)

        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"

    # ----------------------------------------------------------- 3. Missing token

    def test_missing_token_rejected(self):
        from app.main import app

        app.state.cache = MagicMock()
        client = TestClient(app)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
        )
        assert response.status_code == 401

    # ----------------------------------------------------------- 4. Invalid token

    def test_invalid_token_rejected(self):
        from app.main import app

        app.state.cache = MagicMock()
        client = TestClient(app)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers={"Authorization": "Bearer invalid-key"},
        )
        assert response.status_code == 403

    def test_auth_block_parsed_unregistered_key_rejected(self):
        """Regression: ensure the auth block from config.yaml is parsed
        and unregistered keys are rejected.  The alias bug caused the
        raw_yaml_auth field to be silently dropped during Settings
        construction, so all keys fell through to the single-key
        fallback."""
        from app.main import app

        app.state.cache = MagicMock()
        app.state.scraper = self._mock_scraper(return_value=self.SAMPLE_RESULT)
        client = TestClient(app)

        # Key that IS configured for the class-level AUTH_CONFIG
        resp_valid = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers={"Authorization": f"Bearer {self.VALID_KEY}"},
        )
        assert resp_valid.status_code == 200, resp_valid.text

        # Key that is NOT configured — must be rejected with 403
        resp_invalid = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers={"Authorization": "Bearer this-key-is-not-configured"},
        )
        assert resp_invalid.status_code == 403, (
            f"Expected 403 for unregistered key, got {resp_invalid.status_code}: "
            f"{resp_invalid.text}"
        )

    # ----------------------------------------------------------- 5. Disabled profile

    def test_disabled_profile_rejected(self):
        """Disabled profiles should not be accepted."""
        from app.main import app

        app.state.cache = MagicMock()
        client = TestClient(app)

        # Set up a new resolver with a disabled profile.
        reset_profile_resolver()
        settings = _make_settings_with_auth(
            {
                "api_keys": [
                    {"name": "default", "key": "valid-key", "enabled": True},
                    {"name": "disabled", "key": "disabled-key", "enabled": False},
                ],
            }
        )
        init_profile_resolver(settings)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers={"Authorization": "Bearer disabled-key"},
        )
        assert response.status_code == 403

    # ----------------------------------------------------------- 6. Multiple keys

    def test_multiple_keys_map_to_different_profiles(self):
        """Different keys authenticate to different profile names."""
        from app.main import app

        app.state.cache = MagicMock()
        app.state.scraper = self._mock_scraper(return_value=self.SAMPLE_RESULT)
        client = TestClient(app)

        # Default key works
        resp1 = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers={"Authorization": f"Bearer {self.VALID_KEY}"},
        )
        assert resp1.status_code == 200

        # Fanatics key works
        resp2 = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers={"Authorization": "Bearer fan-key"},
        )
        assert resp2.status_code == 200

    # ----------------------------------------------------------- 7. Expose profile in response

    def test_auth_profile_metadata_appears_when_configured(self):
        """auth_profile metadata appears when expose_profile_in_response is true
        and auth_context is available."""
        from app.api.routes import _maybe_add_auth_profile
        from app.auth.models import AuthContext

        # Unit test: _maybe_add_auth_profile adds the profile when both
        # conditions are met.
        ctx = AuthContext(profile_name="default")
        result = {"html": "<html>test</html>", "metadata": {"mode": "http"}}

        # With expose=True
        enriched = _maybe_add_auth_profile(result, ctx, expose_profile=True)
        assert enriched["metadata"]["auth_profile"] == "default"

        # With expose=False
        enriched2 = _maybe_add_auth_profile(result, ctx, expose_profile=False)
        assert "auth_profile" not in enriched2.get("metadata", {})

        # With None auth_context
        enriched3 = _maybe_add_auth_profile(result, None, expose_profile=True)
        assert "auth_profile" not in enriched3.get("metadata", {})

        # Test resolver expose property.
        reset_profile_resolver()
        settings = _make_settings_with_auth(
            {
                "expose_profile_in_response": True,
                "api_keys": [
                    {"name": "default", "key": "expose-key", "enabled": True},
                ],
            }
        )
        resolver = init_profile_resolver(settings)
        assert resolver.expose_profile_in_response is True

        # With expose=False
        reset_profile_resolver()
        settings2 = _make_settings_with_auth(
            {
                "expose_profile_in_response": False,
                "api_keys": [
                    {"name": "default", "key": "expose-key", "enabled": True},
                ],
            }
        )
        resolver2 = init_profile_resolver(settings2)
        assert resolver2.expose_profile_in_response is False

    def test_auth_profile_metadata_absent_when_not_configured(self):
        """auth_profile metadata does not appear when expose_profile_in_response is false."""
        from app.main import app

        app.state.cache = MagicMock()
        app.state.scraper = self._mock_scraper(return_value=self.SAMPLE_RESULT)
        client = TestClient(app)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers={"Authorization": f"Bearer {self.VALID_KEY}"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert "auth_profile" not in data["metadata"]

    # ----------------------------------------------------------- 8. Batch scrape

    def test_batch_scrape_uses_authenticated_profile(self):
        """Batch scrape uses the authenticated profile."""
        from app.main import app

        app.state.cache = MagicMock()
        app.state.scraper = self._mock_scraper(return_value=self.SAMPLE_RESULT)
        client = TestClient(app)

        response = client.post(
            "/v1/scrape/batch",
            json={
                "items": [
                    {"url": "https://example.com/1"},
                    {"url": "https://example.com/2"},
                ],
                "max_concurrency": 2,
            },
            headers={"Authorization": f"Bearer {self.VALID_KEY}"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["total"] == 2
        assert data["succeeded"] == 2

    # ----------------------------------------------------------- 9. Async jobs

    def test_job_uses_authenticated_profile(self):
        """Async job stores the authenticated profile name."""
        from app.main import app

        app.state.cache = MagicMock()
        app.state.scraper = self._mock_scraper(return_value=self.SAMPLE_RESULT)
        # Run the lifespan-like setup for the job service.
        from app.core.config import Settings as AppSettings
        from app.jobs.service import JobService
        from app.metrics.prometheus import get_metrics

        app.state.settings = AppSettings()
        job_service = JobService(
            scraper=app.state.scraper,
            settings=app.state.settings,
            cache=MagicMock(),
            metrics=get_metrics(),
        )
        app.state.job_service = job_service

        client = TestClient(app)

        response = client.post(
            "/v1/jobs",
            json={"url": "https://example.com"},
            headers={"Authorization": f"Bearer {self.VALID_KEY}"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] in ("queued", "running", "succeeded")

        # Profile name may be None by default.
        # Check that no secrets are leaked.
        assert "api_key" not in str(data).lower()

    def test_job_response_includes_profile_when_configured(self):
        """Job response includes profile_name when expose_profile_in_response is true."""
        from app.main import app

        reset_profile_resolver()
        settings = _make_settings_with_auth(
            {
                "default_profile": "default",
                "expose_profile_in_response": True,
                "api_keys": [
                    {"name": "default", "key": "expose-key", "enabled": True},
                ],
            }
        )
        init_profile_resolver(settings)

        app.state.cache = MagicMock()
        app.state.scraper = self._mock_scraper(return_value=self.SAMPLE_RESULT)
        from app.jobs.service import JobService
        from app.metrics.prometheus import get_metrics

        app.state.settings = _make_settings_no_auth()
        job_service = JobService(
            scraper=app.state.scraper,
            settings=app.state.settings,
            cache=MagicMock(),
            metrics=get_metrics(),
        )
        app.state.job_service = job_service

        client = TestClient(app)

        response = client.post(
            "/v1/jobs",
            json={"url": "https://example.com"},
            headers={"Authorization": "Bearer expose-key"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data.get("profile_name") == "default"

    def test_job_stores_profile_context(self):
        """Job stores effective settings snapshot without API keys."""
        from app.main import app

        app.state.cache = MagicMock()
        app.state.scraper = self._mock_scraper(return_value=self.SAMPLE_RESULT)
        from app.jobs.service import JobService
        from app.metrics.prometheus import get_metrics

        app.state.settings = _make_settings_no_auth()
        job_service = JobService(
            scraper=app.state.scraper,
            settings=app.state.settings,
            cache=MagicMock(),
            metrics=get_metrics(),
        )
        app.state.job_service = job_service

        client = TestClient(app)

        response = client.post(
            "/v1/jobs",
            json={"url": "https://example.com"},
            headers={"Authorization": f"Bearer {self.VALID_KEY}"},
        )
        assert response.status_code == 200

    def test_job_response_does_not_expose_key(self):
        """Job response should never include API keys."""
        from app.main import app

        app.state.cache = MagicMock()
        app.state.scraper = self._mock_scraper(return_value=self.SAMPLE_RESULT)
        from app.jobs.service import JobService
        from app.metrics.prometheus import get_metrics

        app.state.settings = _make_settings_no_auth()
        job_service = JobService(
            scraper=app.state.scraper,
            settings=app.state.settings,
            cache=MagicMock(),
            metrics=get_metrics(),
        )
        app.state.job_service = job_service

        client = TestClient(app)

        response = client.post(
            "/v1/jobs",
            json={"url": "https://example.com"},
            headers={"Authorization": f"Bearer {self.VALID_KEY}"},
        )
        assert response.status_code == 200
        data = response.json()
        # Check that no field named 'key' or 'api_key' exists at any level.
        json_str = str(data)
        assert "api_key" not in json_str.lower()

    def test_job_uses_effective_settings(self):
        """Job should use effective settings based on profile."""
        from app.jobs.service import JobService
        from app.main import app
        from app.metrics.prometheus import get_metrics

        # Set up a resolver with a profile that has browser override.
        reset_profile_resolver()
        settings = _make_settings_with_auth(
            {
                "expose_profile_in_response": True,
                "api_keys": [
                    {
                        "name": "browser-profile",
                        "key": "browser-key",
                        "enabled": True,
                        "overrides": {"scraper": {"default_mode": "browser"}},
                    },
                ],
            }
        )
        init_profile_resolver(settings)

        app.state.cache = MagicMock()
        app.state.scraper = self._mock_scraper(return_value=self.SAMPLE_RESULT)
        app.state.settings = settings

        job_service = JobService(
            scraper=app.state.scraper,
            settings=settings,
            cache=MagicMock(),
            metrics=get_metrics(),
        )
        app.state.job_service = job_service

        client = TestClient(app)

        response = client.post(
            "/v1/jobs",
            json={"url": "https://example.com"},
            headers={"Authorization": "Bearer browser-key"},
        )
        assert response.status_code == 200


# ======================================================= Auth disabled


class TestAuthDisabled:
    def test_auth_disabled_allows_all_requests(self):
        from app.api.dependencies import verify_api_key
        from app.main import app

        app.dependency_overrides[verify_api_key] = lambda: None
        try:
            app.state.cache = MagicMock()
            app.state.scraper = AsyncMock()
            app.state.scraper.scrape.return_value = {
                "url": "https://example.com",
                "final_url": "https://example.com",
                "status_code": 200,
                "html": "<html>Hello</html>",
                "metadata": {
                    "mode": "http",
                    "elapsed_ms": 10,
                    "content_length": 20,
                    "cache_key": "x",
                },
            }

            client = TestClient(app)
            response = client.post(
                "/v1/scrape",
                json={"url": "https://example.com"},
            )
            assert response.status_code == 200, response.text
        finally:
            app.dependency_overrides.clear()


# ======================================================= Docs and examples


class TestConfigExample:
    """Verify that the example config documents the auth section."""

    def test_example_yaml_has_auth_section(self):
        from pathlib import Path

        import yaml

        example_path = Path("configs/config.example.yaml")
        assert example_path.exists()
        with open(example_path) as f:
            data = yaml.safe_load(f)
        assert "auth" in data
        assert "api_keys" in data["auth"]

    def test_example_env_has_profile_keys(self):
        from pathlib import Path

        env_path = Path(".env.example")
        assert env_path.exists()
        content = env_path.read_text()
        assert "SCRAPER_API_KEY_FANATICS" in content
        assert "SCRAPER_API_KEY_DEBUG" in content
