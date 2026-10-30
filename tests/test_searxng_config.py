"""Tests for the SearXNG provider configuration parsing."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings
from app.search.providers.searxng import SearxngConfig


def _settings_with_search_block(search_block: dict | None) -> Settings:
    """Build a Settings instance with a raw search block injected.

    Mirrors the ``raw_yaml_auth`` test convention: the aliased raw block
    field cannot be set via constructor kwargs, so it is set directly.
    """
    settings = Settings(api_key="test-key", server_api_key_required=False)
    object.__setattr__(settings, "raw_yaml_search", search_block)
    return settings


class TestSearxngConfigDefaults:
    def test_defaults(self):
        cfg = SearxngConfig()
        assert cfg.enabled is False
        assert cfg.base_url is None
        assert cfg.timeout_seconds == 15.0
        assert cfg.default_language is None
        assert cfg.config_cache_ttl_seconds == 3600

    def test_from_settings_without_block_uses_defaults(self):
        settings = _settings_with_search_block(None)
        cfg = SearxngConfig.from_settings(settings)
        assert cfg.enabled is False
        assert cfg.base_url is None


class TestSearxngConfigFromBlock:
    def test_full_block(self):
        settings = _settings_with_search_block(
            {
                "providers": {
                    "searxng": {
                        "enabled": True,
                        "base_url": "http://searxng:8080",
                        "timeout_seconds": 30,
                        "default_language": "en-US",
                        "config_cache_ttl_seconds": 600,
                    }
                }
            }
        )
        cfg = SearxngConfig.from_settings(settings)
        assert cfg.enabled is True
        assert cfg.base_url == "http://searxng:8080"
        assert cfg.timeout_seconds == 30.0
        assert cfg.default_language == "en-US"
        assert cfg.config_cache_ttl_seconds == 600

    def test_other_providers_ignored(self):
        """Unknown provider blocks are ignored, not applied to searxng."""
        settings = _settings_with_search_block(
            {"providers": {"other": {"enabled": True, "base_url": "http://x"}}}
        )
        cfg = SearxngConfig.from_settings(settings)
        assert cfg.enabled is False

    def test_non_mapping_block_rejected(self):
        settings = _settings_with_search_block({"providers": {"searxng": "yes"}})
        with pytest.raises(ValueError, match="must be a mapping"):
            SearxngConfig.from_settings(settings)

    def test_env_var_overrides_yaml(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_SEARCH_SEARXNG_ENABLED", "true")
        monkeypatch.setenv("SCRAPER_SEARCH_SEARXNG_BASE_URL", "http://env:1234")
        monkeypatch.setenv("SCRAPER_SEARCH_SEARXNG_TIMEOUT_SECONDS", "7")
        settings = _settings_with_search_block(
            {
                "providers": {
                    "searxng": {
                        "enabled": False,
                        "base_url": "http://yaml:8080",
                        "timeout_seconds": 15,
                    }
                }
            }
        )
        cfg = SearxngConfig.from_settings(settings)
        assert cfg.enabled is True
        assert cfg.base_url == "http://env:1234"
        assert cfg.timeout_seconds == 7.0

    def test_env_var_unknown_key_ignored(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_SEARCH_SEARXNG_BOGUS", "1")
        settings = _settings_with_search_block({"providers": {"searxng": {}}})
        cfg = SearxngConfig.from_settings(settings)
        assert cfg.enabled is False

    def test_from_settings_load_yaml(self):
        """End-to-end: a YAML file with a search block lands in the raw block."""
        yaml_content = {
            "server": {"api_key_required": False},
            "search": {
                "providers": {
                    "searxng": {
                        "enabled": True,
                        "base_url": "${SEARXNG_TEST_URL}",
                    }
                }
            },
        }
        tmp = Path(tempfile.mktemp(suffix=".yaml"))
        monkeypatch_env = os.environ.get("SEARXNG_TEST_URL")
        os.environ["SEARXNG_TEST_URL"] = "http://searxng.test:8080"
        try:
            with open(tmp, "w") as f:
                yaml.dump(yaml_content, f)
            settings = Settings.load(config_path=str(tmp))
            cfg = SearxngConfig.from_settings(settings)
            assert cfg.enabled is True
            assert cfg.base_url == "http://searxng.test:8080"
        finally:
            if monkeypatch_env is None:
                os.environ.pop("SEARXNG_TEST_URL", None)
            else:
                os.environ["SEARXNG_TEST_URL"] = monkeypatch_env
            tmp.unlink(missing_ok=True)


class TestSearxngConfigValidation:
    def test_enabled_requires_base_url(self):
        with pytest.raises(ValueError, match="base_url"):
            SearxngConfig(enabled=True)

    def test_invalid_base_url_scheme(self):
        with pytest.raises(ValueError, match="http"):
            SearxngConfig(enabled=True, base_url="ftp://searxng:8080")

    def test_empty_base_url_treated_as_unset(self):
        cfg = SearxngConfig(enabled=False, base_url="")
        assert cfg.base_url is None

    def test_invalid_default_language(self):
        with pytest.raises(ValueError, match="BCP-47"):
            SearxngConfig(default_language="english")

    def test_valid_language_tag(self):
        cfg = SearxngConfig(default_language="en-US")
        assert cfg.default_language == "en-US"

    def test_whitespace_only_language_treated_as_unset(self):
        assert SearxngConfig(default_language="  ").default_language is None

    def test_timeout_bounds(self):
        with pytest.raises(ValueError):
            SearxngConfig(timeout_seconds=0)
        with pytest.raises(ValueError):
            SearxngConfig(timeout_seconds=1000)
        assert SearxngConfig(timeout_seconds=1).timeout_seconds == 1

    def test_negative_cache_ttl_rejected(self):
        with pytest.raises(ValueError):
            SearxngConfig(config_cache_ttl_seconds=-1)

    def test_zero_cache_ttl_allowed(self):
        assert SearxngConfig(config_cache_ttl_seconds=0).config_cache_ttl_seconds == 0

    def test_unknown_field_rejected(self):
        with pytest.raises(ValueError):
            SearxngConfig(bogus_field="x")
