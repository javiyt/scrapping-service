"""Tests for the plugin loader.

These tests verify that the loader discovers plugins via entry points,
validates api_version, respects allowlists, and handles errors correctly.

"""

from unittest.mock import patch

import httpx


def test_loader_with_zero_entry_points():
    """Loader must succeed when there are no entry points (normal CI/dev state)."""
    from app.core.config import Settings
    from app.plugins.loader import load_plugins

    settings = Settings.load("configs/config.yaml")

    with patch("importlib.metadata.entry_points") as mock_ep:
        # Return empty list for scrapping_service.plugins
        mock_ep.return_value = []

        result = load_plugins(settings, http_client=httpx.AsyncClient())

        assert result.loaded == []
        assert result.search_providers == []
        assert result.transcript_providers == []
        assert result.browser_provider is None
        assert result.skipped == []


def test_loader_rejects_wrong_api_version():
    """Loader must reject plugins with mismatched major api_version."""
    from app.core.config import Settings
    from app.plugins.loader import load_plugins

    settings = Settings.load("configs/config.yaml")

    class BadPlugin:
        name = "bad-plugin"
        version = "1.0.0"
        api_version = "2.0.0"  # Wrong major!

        def register(self, registry, ctx):
            pass

    class FakeEntryPoint:
        name = "bad-plugin"
        value = "test_loader_rejects_wrong_api_version.BadPlugin"

        def load(self):
            return BadPlugin

    with patch("importlib.metadata.entry_points") as mock_ep:
        mock_ep.return_value = [FakeEntryPoint()]

        # With no enabled list, no plugins are loaded (opt-in)
        result = load_plugins(settings, http_client=httpx.AsyncClient())

        # Plugin should be skipped because it's not in enabled list
        assert result.loaded == []
        assert len(result.skipped) == 1
        assert result.skipped[0]["name"] == "bad-plugin"
        assert "not enabled" in result.skipped[0]["reason"]


def test_loader_allowlist_filters():
    """Loader must skip plugins not in allowlist when allowlist is non-empty."""
    from app.core.config import Settings
    from app.plugins.loader import load_plugins

    # Load settings to get raw_yaml_plugin, then modify allowlist
    settings = Settings.load("configs/config.yaml")
    # Directly modify raw_yaml_plugin for testing
    settings.raw_yaml_plugin = {
        "enabled": True,
        "allowlist": ["allowed-plugin"],  # Only this plugin allowed
    }

    class AllowedPlugin:
        name = "allowed-plugin"
        version = "1.0.0"
        api_version = "1.0.0"

        def register(self, registry, ctx):
            pass

    class DisallowedPlugin:
        name = "disallowed-plugin"
        version = "1.0.0"
        api_version = "1.0.0"

        def register(self, registry, ctx):
            pass

    class FakeEntryPoint:
        def __init__(self, name, value):
            self.name = name
            self.value = value

        def load(self):
            return AllowedPlugin if self.name == "allowed-plugin" else DisallowedPlugin

    with patch("importlib.metadata.entry_points") as mock_ep:
        mock_ep.return_value = [
            FakeEntryPoint("allowed-plugin", "x"),
            FakeEntryPoint("disallowed-plugin", "y"),
        ]

        result = load_plugins(settings, http_client=httpx.AsyncClient())

        # Only allowed-plugin should be in loaded
        assert result.loaded == ["allowed-plugin"]
        # disallowed-plugin should be in skipped
        assert len(result.skipped) == 1
        assert result.skipped[0]["name"] == "disallowed-plugin"
        assert "allowlist" in result.skipped[0]["reason"]


def test_loader_import_failure():
    """Loader must handle entry point import failures gracefully."""
    from app.core.config import Settings
    from app.plugins.loader import load_plugins

    settings = Settings.load("configs/config.yaml")

    class FakeEntryPoint:
        name = "broken-plugin"
        value = "nonexistent.module:Plugin"

        def load(self):
            raise ImportError("Module not found")

    with patch("importlib.metadata.entry_points") as mock_ep:
        mock_ep.return_value = [FakeEntryPoint()]

        result = load_plugins(settings, http_client=httpx.AsyncClient())

        # Plugin should be skipped because it's not enabled
        assert result.loaded == []
        assert len(result.skipped) == 1
        assert result.skipped[0]["name"] == "broken-plugin"
        assert "not enabled" in result.skipped[0]["reason"].lower()


def test_loader_http_client_factory():
    """Loader must inject http_client_factory into plugin context."""
    import httpx

    from app.core.config import Settings
    from app.plugins.loader import load_plugins

    settings = Settings.load("configs/config.yaml")
    settings.raw_yaml_plugin = {
        "enabled": True,
        "allowlist": ["http-client-plugin"],
    }
    client = httpx.AsyncClient()

    captured_client = None

    class HttpClientPlugin:
        name = "http-client-plugin"
        version = "1.0.0"
        api_version = "1.0.0"

        def register(self, registry, ctx):
            # Call factory and capture result
            nonlocal captured_client
            captured_client = ctx.http_client_factory()

    class FakeEntryPoint:
        name = "http-client-plugin"
        value = "test_loader_http_client_factory.HttpClientPlugin"

        def load(self):
            return HttpClientPlugin

    with patch("importlib.metadata.entry_points") as mock_ep:
        mock_ep.return_value = [FakeEntryPoint()]

        result = load_plugins(settings, http_client=client)

        # Plugin should have been loaded
        assert result.loaded == ["http-client-plugin"]
        # Verify the captured client is the injected one
        assert captured_client is client

    # Clean up
    import asyncio

    asyncio.run(client.aclose())


def test_loader_provides_logger_with_name():
    """PluginContext must provide a logger with plugin name."""
    from app.core.config import Settings
    from app.plugins.loader import load_plugins

    settings = Settings.load("configs/config.yaml")
    settings.raw_yaml_plugin = {
        "enabled": True,
        "allowlist": ["logger-plugin"],
    }

    captured_logger_name = None

    class LoggerPlugin:
        name = "logger-plugin"
        version = "1.0.0"
        api_version = "1.0.0"

        def register(self, registry, ctx):
            nonlocal captured_logger_name
            captured_logger_name = ctx.logger.name

    class FakeEntryPoint:
        name = "logger-plugin"
        value = "test_loader_provides_logger.LoggerPlugin"

        def load(self):
            return LoggerPlugin

    with patch("importlib.metadata.entry_points") as mock_ep:
        mock_ep.return_value = [FakeEntryPoint()]

        result = load_plugins(settings, http_client=httpx.AsyncClient())

        # Plugin should have been loaded
        assert result.loaded == ["logger-plugin"]
        # Logger should have the plugin name in its path
        assert "logger-plugin" in captured_logger_name
