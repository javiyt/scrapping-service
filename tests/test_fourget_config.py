"""Tests for the 4get provider configuration (``FourgetConfig``).

Configuration sources are exercised directly (no application startup):
YAML blocks via ``raw_yaml_search`` and environment overrides via
``SCRAPER_SEARCH_4GET_*``.
"""

import pytest

from app.core.config import Settings
from app.search.providers.fourget.config import FourgetConfig

BASE_URL = "http://4get.test:8080"


def _settings_with_search_block(search_block: dict | None) -> Settings:
    settings = Settings(api_key="test-key", server_api_key_required=False)
    object.__setattr__(settings, "raw_yaml_search", search_block)
    return settings


class TestDefaults:
    def test_disabled_by_default(self):
        config = FourgetConfig()
        assert config.enabled is False

    def test_base_url_defaults_none(self):
        assert FourgetConfig().base_url is None

    def test_pass_token_defaults_none(self):
        assert FourgetConfig().pass_token is None

    def test_defaults(self):
        config = FourgetConfig()
        assert config.timeout_seconds == 15.0
        assert config.scraper is None
        assert config.default_language is None
        assert config.default_region is None
        assert config.default_nsfw is False
        assert config.health_cache_ttl_seconds == 300


class TestConstruction:
    def test_full_configuration(self):
        config = FourgetConfig(
            enabled=True,
            base_url=BASE_URL,
            timeout_seconds=30,
            pass_token="my-pass",
            scraper="duckduckgo",
            default_language="en-US",
            default_region="BR",
            default_nsfw=True,
            health_cache_ttl_seconds=0,
        )
        assert config.enabled is True
        assert config.base_url == BASE_URL
        assert config.timeout_seconds == 30
        assert config.pass_token == "my-pass"
        assert config.scraper == "duckduckgo"
        assert config.default_language == "en-US"
        assert config.default_region == "br"  # lower-cased
        assert config.default_nsfw is True
        assert config.health_cache_ttl_seconds == 0

    def test_enabled_requires_base_url(self):
        with pytest.raises(ValueError, match="base_url"):
            FourgetConfig(enabled=True)

    def test_invalid_base_url_rejected(self):
        with pytest.raises(ValueError, match="base_url"):
            FourgetConfig(enabled=True, base_url="not-a-url")

    def test_invalid_language_rejected(self):
        with pytest.raises(ValueError, match="default_language"):
            FourgetConfig(default_language="english")

    def test_invalid_region_rejected(self):
        with pytest.raises(ValueError, match="default_region"):
            FourgetConfig(default_region="united-states")

    def test_timeout_bounds(self):
        with pytest.raises(ValueError):
            FourgetConfig(timeout_seconds=0)
        with pytest.raises(ValueError):
            FourgetConfig(timeout_seconds=1000)

    def test_empty_pass_token_becomes_none(self):
        assert FourgetConfig(pass_token="   ").pass_token is None

    def test_unexpanded_env_placeholder_pass_becomes_none(self):
        assert FourgetConfig(pass_token="${SCRAPER_4GET_PASS}").pass_token is None

    def test_extra_keys_rejected(self):
        with pytest.raises(ValueError):
            FourgetConfig(**{"unknown_key": 1})


class TestSecretHandling:
    def test_pass_token_excluded_from_dump(self):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, pass_token="super-secret")
        assert "pass_token" not in config.model_dump()

    def test_pass_token_masked_in_repr(self):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, pass_token="super-secret")
        assert "super-secret" not in repr(config)
        assert "********" in repr(config)

    def test_pass_token_absent_from_repr_when_unset(self):
        config = FourgetConfig()
        assert "pass_token=None" in repr(config)


class TestFromSettings:
    def test_no_search_block_gives_defaults(self):
        config = FourgetConfig.from_settings(_settings_with_search_block(None))
        assert config.enabled is False
        assert config.base_url is None

    def test_yaml_block(self):
        settings = _settings_with_search_block(
            {
                "providers": {
                    "4get": {
                        "enabled": True,
                        "base_url": BASE_URL,
                        "timeout_seconds": 20,
                        "scraper": "brave",
                        "default_language": "en",
                        "default_region": "us",
                        "default_nsfw": True,
                        "health_cache_ttl_seconds": 600,
                    }
                }
            }
        )
        config = FourgetConfig.from_settings(settings)
        assert config.enabled is True
        assert config.base_url == BASE_URL
        assert config.timeout_seconds == 20
        assert config.scraper == "brave"
        assert config.default_language == "en"
        assert config.default_region == "us"
        assert config.default_nsfw is True
        assert config.health_cache_ttl_seconds == 600

    def test_env_overrides_yaml(self, monkeypatch):
        settings = _settings_with_search_block(
            {
                "providers": {
                    "4get": {
                        "enabled": False,
                        "base_url": "http://yaml.test",
                        "scraper": "ddg",
                    }
                }
            }
        )
        monkeypatch.setenv("SCRAPER_SEARCH_4GET_ENABLED", "true")
        monkeypatch.setenv("SCRAPER_SEARCH_4GET_BASE_URL", BASE_URL)
        monkeypatch.setenv("SCRAPER_SEARCH_4GET_SCRAPER", "brave")
        config = FourgetConfig.from_settings(settings)
        assert config.enabled is True
        assert config.base_url == BASE_URL
        assert config.scraper == "brave"

    def test_env_pass_token(self, monkeypatch):
        monkeypatch.setenv("SCRAPER_SEARCH_4GET_ENABLED", "true")
        monkeypatch.setenv("SCRAPER_SEARCH_4GET_BASE_URL", BASE_URL)
        monkeypatch.setenv("SCRAPER_SEARCH_4GET_PASS_TOKEN", "env-pass")
        config = FourgetConfig.from_settings(_settings_with_search_block(None))
        assert config.pass_token == "env-pass"

    def test_non_mapping_block_rejected(self):
        settings = _settings_with_search_block({"providers": {"4get": "nope"}})
        with pytest.raises(ValueError, match="must be a mapping"):
            FourgetConfig.from_settings(settings)
