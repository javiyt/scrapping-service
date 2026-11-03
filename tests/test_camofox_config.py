"""Tests for the Camofox client configuration (CamofoxConfig)."""

import os

import pytest

from app.camofox import build_camofox_client
from app.camofox.config import CamofoxConfig
from app.core.config import Settings


def _settings_with_camofox_block(block: dict | None) -> Settings:
    """Build a Settings instance with a raw camofox block injected."""
    settings = Settings(api_key="test-key", server_api_key_required=False)
    object.__setattr__(settings, "raw_yaml_camofox", block)
    return settings


def test_defaults():
    cfg = CamofoxConfig()
    assert cfg.enabled is False
    assert cfg.base_url is None
    assert cfg.timeout_seconds == 60.0
    assert cfg.access_key is None
    assert cfg.api_key is None
    assert cfg.user_id_prefix == "scrapping-service"


def test_enabled_requires_base_url():
    with pytest.raises(ValueError, match="base_url"):
        CamofoxConfig(enabled=True)


def test_disabled_allows_missing_base_url():
    cfg = CamofoxConfig(enabled=False)
    assert cfg.base_url is None


def test_base_url_validation():
    assert (
        CamofoxConfig(enabled=True, base_url="http://camofox:9377").base_url
        == "http://camofox:9377"
    )
    assert CamofoxConfig(enabled=True, base_url="https://camofox.example.com/").base_url == (
        "https://camofox.example.com"
    )
    with pytest.raises(ValueError):
        CamofoxConfig(enabled=True, base_url="camofox:9377")
    with pytest.raises(ValueError):
        CamofoxConfig(enabled=True, base_url="ftp://camofox:9377")


def test_timeout_bounds():
    assert CamofoxConfig(timeout_seconds=1).timeout_seconds == 1
    with pytest.raises(ValueError):
        CamofoxConfig(timeout_seconds=0)
    with pytest.raises(ValueError):
        CamofoxConfig(timeout_seconds=-5)


def test_secrets_are_cleaned_and_never_serialized():
    cfg = CamofoxConfig(access_key="  aksv1-secret  ", api_key="${CAMOFOX_API_KEY}")
    assert cfg.access_key == "aksv1-secret"
    assert cfg.api_key is None  # unexpanded placeholder -> unset
    dumped = cfg.model_dump()
    assert "access_key" not in dumped
    assert "api_key" not in dumped
    assert "aksv1-secret" not in repr(cfg)
    assert "aksv1-secret" not in str(cfg)


def test_blank_secrets_become_none():
    cfg = CamofoxConfig(access_key="   ", api_key="")
    assert cfg.access_key is None
    assert cfg.api_key is None


def test_user_id_prefix_must_not_be_blank():
    with pytest.raises(ValueError):
        CamofoxConfig(user_id_prefix="   ")


def test_extra_keys_forbidden():
    with pytest.raises(ValueError):
        CamofoxConfig(**{"unknown_key": 1})  # type: ignore[call-arg]


def test_from_settings_without_block_uses_defaults():
    cfg = CamofoxConfig.from_settings(_settings_with_camofox_block(None))
    assert cfg.enabled is False
    assert cfg.base_url is None


def test_from_settings_full_block():
    settings = _settings_with_camofox_block(
        {
            "enabled": True,
            "base_url": "http://camofox:9377",
            "timeout_seconds": 30,
            "access_key": "yaml-access",
            "api_key": "yaml-api",
            "user_id_prefix": "scraper",
        }
    )
    cfg = CamofoxConfig.from_settings(settings)
    assert cfg.enabled is True
    assert cfg.base_url == "http://camofox:9377"
    assert cfg.timeout_seconds == 30.0
    assert cfg.access_key == "yaml-access"
    assert cfg.api_key == "yaml-api"
    assert cfg.user_id_prefix == "scraper"


def test_from_settings_env_overrides_yaml(monkeypatch):
    settings = _settings_with_camofox_block(
        {
            "enabled": True,
            "base_url": "http://yaml:9377",
            "access_key": "yaml-access",
        }
    )
    monkeypatch.setenv("SCRAPER_CAMOFOX_BASE_URL", "http://env:9377")
    monkeypatch.setenv("SCRAPER_CAMOFOX_ACCESS_KEY", "env-access")
    cfg = CamofoxConfig.from_settings(settings)
    assert cfg.base_url == "http://env:9377"
    assert cfg.access_key == "env-access"


def test_from_settings_env_expanded_placeholder(monkeypatch):
    """A ${VAR} referencing a *set* variable is expanded by the YAML loader."""
    monkeypatch.setenv("CAMOFOX_ACCESS_KEY", "expanded-access")
    settings = _settings_with_camofox_block(
        {"enabled": True, "base_url": "http://camofox:9377", "access_key": "${CAMOFOX_ACCESS_KEY}"}
    )
    # Simulate the expansion the config loader performs for raw blocks.
    import re

    expanded = dict(settings.raw_yaml_camofox)  # type: ignore[misc]
    expanded["access_key"] = re.sub(
        r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), expanded["access_key"]
    )
    object.__setattr__(settings, "raw_yaml_camofox", expanded)
    cfg = CamofoxConfig.from_settings(settings)
    assert cfg.access_key == "expanded-access"


def test_settings_raw_block_carried_and_excluded_from_dump():
    settings = Settings(api_key="test-key")
    object.__setattr__(
        settings, "raw_yaml_camofox", {"enabled": True, "base_url": "http://camofox:9377"}
    )
    assert settings.raw_yaml_camofox == {"enabled": True, "base_url": "http://camofox:9377"}
    assert "raw_yaml_camofox" not in settings.model_dump()
    assert "_raw_yaml_camofox" not in settings.model_dump()


def test_settings_yaml_mapping_carries_camofox_block(tmp_path, monkeypatch):
    """The top-level ``camofox`` YAML block lands in ``raw_yaml_camofox``."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "camofox:\n"
        "  enabled: true\n"
        "  base_url: http://camofox:9377\n"
        "  access_key: ${CAMOFOX_ACCESS_KEY}\n"
    )
    monkeypatch.setenv("CONFIG_PATH", str(config_file))
    monkeypatch.delenv("CAMOFOX_ACCESS_KEY", raising=False)
    settings = Settings.load()
    assert settings.raw_yaml_camofox == {
        "enabled": True,
        "base_url": "http://camofox:9377",
        "access_key": "${CAMOFOX_ACCESS_KEY}",  # unset var stays a literal placeholder
    }


def test_build_camofox_client_disabled_returns_none():
    settings = _settings_with_camofox_block({"enabled": False})
    assert build_camofox_client(settings) is None


def test_build_camofox_client_absent_block_returns_none():
    assert build_camofox_client(Settings(api_key="test-key")) is None


def test_build_camofox_client_enabled_returns_client():
    settings = _settings_with_camofox_block(
        {"enabled": True, "base_url": "http://camofox:9377", "access_key": "a", "api_key": "b"}
    )
    client = build_camofox_client(settings)
    assert client is not None
    assert client.base_url == "http://camofox:9377"
