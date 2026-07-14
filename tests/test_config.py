"""Tests for configuration loading and validation."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings


class TestConfigDefaults:
    """Verify hard-coded default values are sensible."""

    def test_default_port(self):
        s = Settings(_env_file=None, scraper_api_key="test-key")
        assert s.server_port == 8080

    def test_default_host(self):
        s = Settings(scraper_api_key="test-key")
        assert s.server_host == "0.0.0.0"

    def test_default_mode(self):
        s = Settings(scraper_api_key="test-key")
        assert s.scraper_default_mode == "auto"

    def test_default_ttl(self):
        s = Settings(scraper_api_key="test-key")
        assert s.cache_default_ttl_seconds == 21600

    def test_default_sqlite_path(self):
        s = Settings(scraper_api_key="test-key")
        assert s.cache_sqlite_path == "data/scraper-cache.db"

    def test_api_key_cannot_be_default(self):
        with pytest.raises(ValueError, match="must be changed from the default"):
            Settings(scraper_api_key="change-me")


class TestConfigValidation:
    """Verify Pydantic validation works."""

    def test_valid_port(self):
        s = Settings(scraper_api_key="test-key", server_port=3000)
        assert s.server_port == 3000

    def test_invalid_port_too_low(self):
        with pytest.raises(ValueError):
            Settings(server_port=0)

    def test_invalid_port_too_high(self):
        with pytest.raises(ValueError):
            Settings(server_port=99999)

    def test_invalid_mode(self):
        with pytest.raises(ValueError):
            Settings(scraper_default_mode="ftp")

    def test_valid_modes(self):
        for mode in ("http", "browser", "auto"):
            s = Settings(scraper_api_key="test-key", scraper_default_mode=mode)
            assert s.scraper_default_mode == mode

    def test_max_html_size_bounds(self):
        with pytest.raises(ValueError):
            Settings(cache_max_html_size_mb=0)


class TestConfigFromYaml:
    """Verify YAML loading works correctly via ``Settings.load()``."""

    def test_yaml_overrides_settings(self):
        yaml_content = {
            "server": {"port": 9090, "host": "127.0.0.1"},
            "cache": {"default_ttl_seconds": 3600},
            "scraper": {"default_mode": "http"},
        }
        tmp = Path(tempfile.mktemp(suffix=".yaml"))
        try:
            with open(tmp, "w") as f:
                yaml.dump(yaml_content, f)

            os.environ["SCRAPER_SCRAPER_API_KEY"] = "test-key"
            try:
                s = Settings.load(config_path=str(tmp))
                assert s.server_port == 9090
                assert s.server_host == "127.0.0.1"
                assert s.cache_default_ttl_seconds == 3600
                assert s.scraper_default_mode == "http"
            finally:
                del os.environ["SCRAPER_SCRAPER_API_KEY"]
        finally:
            tmp.unlink(missing_ok=True)

    def test_yaml_domains(self):
        yaml_content = {
            "domains": {
                "example.com": {
                    "allowed": True,
                    "min_delay_seconds": 5,
                    "default_ttl_seconds": 21600,
                }
            }
        }
        tmp = Path(tempfile.mktemp(suffix=".yaml"))
        try:
            with open(tmp, "w") as f:
                yaml.dump(yaml_content, f)

            os.environ["SCRAPER_SCRAPER_API_KEY"] = "test-key"
            try:
                s = Settings.load(config_path=str(tmp))
                assert "example.com" in s.domains
                assert s.domains["example.com"]["min_delay_seconds"] == 5
            finally:
                del os.environ["SCRAPER_SCRAPER_API_KEY"]
        finally:
            tmp.unlink(missing_ok=True)

    def test_env_override_yaml(self):
        """Environment variables should take priority over YAML values."""
        yaml_content = {"server": {"port": 8080}}
        tmp = Path(tempfile.mktemp(suffix=".yaml"))
        try:
            with open(tmp, "w") as f:
                yaml.dump(yaml_content, f)

            os.environ["SCRAPER_SERVER_PORT"] = "3000"
            os.environ["SCRAPER_SCRAPER_API_KEY"] = "test-key"
            try:
                s = Settings.load(config_path=str(tmp))
                assert s.server_port == 3000
            finally:
                del os.environ["SCRAPER_SERVER_PORT"]
                del os.environ["SCRAPER_SCRAPER_API_KEY"]
        finally:
            tmp.unlink(missing_ok=True)

    def test_domain_ttl(self):
        s = Settings(
            scraper_api_key="test-key",
            domains={"example.com": {"default_ttl_seconds": 7200}},
        )
        assert s.get_domain_ttl("example.com") == 7200
        assert s.get_domain_ttl("unknown.com") == s.cache_default_ttl_seconds

    def test_get_domain_config(self):
        s = Settings(scraper_api_key="test-key", domains={"example.com": {"allowed": True, "min_delay_seconds": 5}})
        cfg = s.get_domain_config("example.com")
        assert cfg == {"allowed": True, "min_delay_seconds": 5}
        assert s.get_domain_config("unknown.com") == {}

    def test_is_domain_allowed_empty_list(self):
        s = Settings(scraper_api_key="test-key", security_allowed_domains=[])
        assert s.is_domain_allowed("any.com") is True

    def test_is_domain_allowed_with_list(self):
        s = Settings(scraper_api_key="test-key", security_allowed_domains=["allowed.com"])
        assert s.is_domain_allowed("allowed.com") is True
        assert s.is_domain_allowed("other.com") is False

    def test_all_domain_names(self):
        s = Settings(scraper_api_key="test-key", security_allowed_domains=["a.com", "b.com"])
        assert s.all_domain_names() == ["a.com", "b.com"]

    def test_all_domain_names_from_domains(self):
        s = Settings(
            scraper_api_key="test-key",
            security_allowed_domains=[],
            domains={"x.com": {}, "y.com": {}},
        )
        names = s.all_domain_names()
        assert "x.com" in names
        assert "y.com" in names
