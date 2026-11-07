"""Tests for plugin loader metadata collection.

These tests verify that PluginLoadResult.metadata is populated correctly
for loaded, skipped, and failed plugins.
"""

from unittest.mock import patch

import httpx


def test_loader_collects_metadata_for_loaded_plugin():
    """Loader must populate metadata for plugins that load successfully."""
    from app.core.config import Settings
    from app.plugins.loader import load_plugins

    settings = Settings.load("configs/config.yaml")
    settings.raw_yaml_plugin = {
        "enabled": True,
        "allowlist": ["good-plugin"],
    }

    class GoodPlugin:
        name = "good-plugin"
        version = "1.2.3"
        api_version = "1.0.0"

        def register(self, registry, ctx):
            pass

    class FakeEntryPoint:
        name = "good-plugin"
        value = "test_loader_collects_metadata_for_loaded_plugin.GoodPlugin"

        def load(self):
            return GoodPlugin

    with patch("importlib.metadata.entry_points") as mock_ep:
        mock_ep.return_value = [FakeEntryPoint()]

        result = load_plugins(settings, http_client=httpx.AsyncClient())

        assert result.loaded == ["good-plugin"]
        assert len(result.metadata) == 1
        meta = result.metadata[0]
        assert meta["name"] == "good-plugin"
        assert meta["version"] == "1.2.3"
        assert meta["api_version"] == "1.0.0"
        assert meta["service_api_version"] == "1.0.0"
        assert meta["status"] == "loaded"
        assert meta["reason"] is None
        assert "extensions" in meta


def test_loader_collects_metadata_for_skipped_plugin():
    """Loader must populate metadata for plugins that are skipped."""
    from app.core.config import Settings
    from app.plugins.loader import load_plugins

    settings = Settings.load("configs/config.yaml")
    settings.raw_yaml_plugin = {
        "enabled": True,
        "allowlist": ["allowed"],
    }

    class AllowedPlugin:
        name = "allowed"
        version = "2.0.0"
        api_version = "1.0.0"

        def register(self, registry, ctx):
            pass

    class DisallowedPlugin:
        name = "disallowed"
        version = "1.0.0"
        api_version = "1.0.0"

        def register(self, registry, ctx):
            pass

    class FakeEntryPoint:
        def __init__(self, name, value):
            self.name = name
            self.value = value

        def load(self):
            return AllowedPlugin if self.name == "allowed" else DisallowedPlugin

    with patch("importlib.metadata.entry_points") as mock_ep:
        mock_ep.return_value = [
            FakeEntryPoint("allowed", "x"),
            FakeEntryPoint("disallowed", "y"),
        ]

        result = load_plugins(settings, http_client=httpx.AsyncClient())

        assert result.loaded == ["allowed"]
        assert len(result.skipped) == 1
        assert result.skipped[0]["name"] == "disallowed"
        # skipped plugins should appear in metadata with status=skipped
        assert len(result.metadata) == 2
        
        allowed_meta = next(m for m in result.metadata if m["name"] == "allowed")
        assert allowed_meta["status"] == "loaded"
        
        skipped_meta = next(m for m in result.metadata if m["name"] == "disallowed")
        assert skipped_meta["status"] == "skipped"
        assert "allowlist" in skipped_meta["reason"]


def test_loader_collects_metadata_for_failed_plugin():
    """Loader must populate metadata for plugins that fail to load."""
    from app.core.config import Settings
    from app.plugins.loader import load_plugins

    settings = Settings.load("configs/config.yaml")
    settings.raw_yaml_plugin = {
        "enabled": True,
        "allowlist": ["broken"],
        "on_error": "warn",
    }

    class FakeEntryPoint:
        name = "broken"
        value = "nonexistent.module:Plugin"

        def load(self):
            raise ImportError("Module not found")

    with patch("importlib.metadata.entry_points") as mock_ep:
        mock_ep.return_value = [FakeEntryPoint()]

        result = load_plugins(settings, http_client=httpx.AsyncClient())

        # Plugin should be skipped due to import error
        assert result.loaded == []
        assert len(result.skipped) == 1
        assert result.skipped[0]["name"] == "broken"
        # Failed plugins should appear in metadata with status=failed
        assert len(result.metadata) == 1
        meta = result.metadata[0]
        assert meta["name"] == "broken"
        assert meta["status"] == "failed"
        assert "ImportError" in meta["reason"]


def test_loader_metadata_includes_extensions():
    """Metadata must include extensions dict for each plugin."""
    from app.core.config import Settings
    from app.plugins.loader import load_plugins

    settings = Settings.load("configs/config.yaml")
    settings.raw_yaml_plugin = {
        "enabled": True,
        "allowlist": ["multi-plugin"],
    }

    # Fake provider objects for testing
    class FakeSearchProvider:
        name = "search1"
        domain = "example.com"

    class FakeSearchProvider2:
        name = "search2"
        domain = "example.com"

    class FakeTranscriptProvider:
        name = "trans1"

    class FakeBrowserProvider:
        name = "browser1"

    class FakeExtractor:
        domain = "example.com"
        name = "extract1"
        extractor_type = "css"

    class MultiPlugin:
        name = "multi-plugin"
        version = "3.0.0"
        api_version = "1.0.0"

        def register(self, registry, ctx):
            registry.register_search_provider(FakeSearchProvider())
            registry.register_search_provider(FakeSearchProvider2())
            registry.register_transcript_provider(FakeTranscriptProvider())
            registry.register_browser_provider(FakeBrowserProvider())
            registry.register_extractor(FakeExtractor())

    class FakeEntryPoint:
        name = "multi-plugin"
        value = "test_loader_metadata_includes_extensions.MultiPlugin"

        def load(self):
            return MultiPlugin

    with patch("importlib.metadata.entry_points") as mock_ep:
        mock_ep.return_value = [FakeEntryPoint()]

        result = load_plugins(settings, http_client=httpx.AsyncClient())

        assert result.loaded == ["multi-plugin"]
        assert len(result.metadata) == 1
        meta = result.metadata[0]
        assert "extensions" in meta
        ext = meta["extensions"]
        assert isinstance(ext, dict)
        # The extensions dict should contain at least the keys
        assert "search_providers" in ext
        assert "transcript_providers" in ext
        assert "browser_provider" in ext
        assert "extractors" in ext
