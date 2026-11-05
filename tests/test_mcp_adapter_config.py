"""Configuration and entry-point tests for the MCP adapter (ticket #150).

Covers ``app.mcp.config`` (env parsing, aliases, validation) and
``app.mcp.entrypoints`` (the installed console-script functions) without
launching processes.
"""

from __future__ import annotations

import os

import pytest

from app.mcp import MCP_ADAPTER_VERSION
from app.mcp.config import MCPAdapterConfig, MCPConfigError, load_config

_ENV_PREFIXES = ("MCP_SCRAP", "SCRAPER_API_KEY", "SCRAPER_BASE_URL", "CONFIG_PATH")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every adapter/service variable so each test starts fresh."""
    for key in [k for k in os.environ if k.startswith("MCP_SCRAP") or k in _ENV_PREFIXES]:
        monkeypatch.delenv(key, raising=False)


class TestLoadConfig:
    def test_minimal(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SCRAPPER_BASE_URL", "http://a:8080")
        cfg = load_config()
        assert cfg.service_url == "http://a:8080"
        assert cfg.api_key is None
        assert cfg.timeout_seconds == 180.0
        assert cfg.max_output_chars == 200_000

    def test_full(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SCRAPPER_BASE_URL", "http://a:8080")
        monkeypatch.setenv("MCP_SCRAPPER_API_KEY", "k")
        monkeypatch.setenv("MCP_SCRAPPER_TIMEOUT_SECONDS", "120")
        monkeypatch.setenv("MCP_SCRAPPER_MAX_OUTPUT_CHARS", "5000")
        cfg = load_config()
        assert cfg.api_key == "k"
        assert cfg.timeout_seconds == 120.0
        assert cfg.max_output_chars == 5000

    def test_repo_spelling_alias(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        """MCP_SCRAPER_* (the repo-name spelling) is accepted."""
        monkeypatch.setenv("MCP_SCRAPER_BASE_URL", "http://b:8080")
        cfg = load_config()
        assert cfg.service_url == "http://b:8080"

    def test_primary_wins_over_alias(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_SCRAPPER_BASE_URL", "http://primary:8080")
        monkeypatch.setenv("MCP_SCRAPER_BASE_URL", "http://alias:8080")
        cfg = load_config()
        assert cfg.service_url == "http://primary:8080"

    def test_service_key_passthrough(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SCRAPER_API_KEY (the service's documented variable) feeds the adapter key."""
        monkeypatch.setenv("MCP_SCRAPPER_BASE_URL", "http://a:8080")
        monkeypatch.setenv("SCRAPER_API_KEY", "svc-key")
        cfg = load_config()
        assert cfg.api_key == "svc-key"

    def test_trailing_slash_stripped(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_SCRAPPER_BASE_URL", "http://a:8080/")
        cfg = load_config()
        assert cfg.service_url == "http://a:8080"

    def test_missing_base_url_raises_helpful_error(
        self, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(MCPConfigError) as exc:
            load_config()
        assert "MCP_SCRAPPER_BASE_URL" in str(exc.value)

    def test_bad_timeout_raises(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SCRAPPER_BASE_URL", "http://a:8080")
        monkeypatch.setenv("MCP_SCRAPPER_TIMEOUT_SECONDS", "abc")
        with pytest.raises(MCPConfigError) as exc:
            load_config()
        assert "TIMEOUT" in str(exc.value).upper()

    def test_bad_max_output_raises(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SCRAPPER_BASE_URL", "http://a:8080")
        monkeypatch.setenv("MCP_SCRAPPER_MAX_OUTPUT_CHARS", "not-a-number")
        with pytest.raises(MCPConfigError):
            load_config()

    def test_key_never_in_repr(self, clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_SCRAPPER_BASE_URL", "http://a:8080")
        monkeypatch.setenv("MCP_SCRAPPER_API_KEY", "sk-dont-leak-me")
        cfg = load_config()
        assert "sk-dont-leak-me" not in repr(cfg)


class TestMCPAdapterConfig:
    def test_defaults(self) -> None:
        cfg = MCPAdapterConfig(base_url="http://x:8080")
        assert cfg.service_url == "http://x:8080"
        assert cfg.api_key is None
        assert cfg.timeout_seconds == 180.0
        assert cfg.max_output_chars == 200_000

    def test_non_http_scheme_rejected(self) -> None:
        with pytest.raises(Exception):
            MCPAdapterConfig(base_url="ftp://x:8080")

    def test_version_is_exposed(self) -> None:
        assert MCP_ADAPTER_VERSION == "1.0.0"
