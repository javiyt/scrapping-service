"""Tests for the example plugin used in derived Docker image documentation.

These tests verify that the example plugin package can be imported and
satisfies the plugin contract without requiring external dependencies.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Add the parent directory of the test_plugin package to sys.path
EXAMPLE_PLUGIN_DIR = Path(__file__).parent.parent / "docs" / "examples" / "plugins" / "test_plugin"


def test_example_plugin_can_be_imported():
    """The example plugin must be importable via importlib."""
    sys.path.insert(0, str(EXAMPLE_PLUGIN_DIR))

    try:
        import test_plugin
    except ImportError as exc:
        raise AssertionError(f"Failed to import test_plugin: {exc}") from exc

    assert test_plugin is not None


def test_example_plugin_has_required_attributes():
    """The example plugin must have name, version, and api_version."""
    sys.path.insert(0, str(EXAMPLE_PLUGIN_DIR))

    try:
        import test_plugin.plugin
    except ImportError as exc:
        raise AssertionError(f"Failed to import test_plugin.plugin: {exc}") from exc

    # Mock external dependencies (save original to restore)
    _original_plugin_api = sys.modules.get("app.plugin_api")
    sys.modules["app.plugin_api"] = MagicMock()
    sys.modules["app.plugin_api"].Plugin = object
    sys.modules["app.plugin_api"].PluginContext = MagicMock
    sys.modules["app.plugin_api"].PluginRegistry = MagicMock

    try:
        # Reload to pick up mocks
        if "test_plugin.plugin" in sys.modules:
            del sys.modules["test_plugin.plugin"]
        if "test_plugin" in sys.modules:
            del sys.modules["test_plugin"]

        # Reimport with mocks
        import test_plugin.plugin

        plugin = test_plugin.plugin.TestPlugin()

        assert hasattr(plugin, "name")
        assert isinstance(plugin.name, str)
        assert plugin.name == "test-plugin"

        assert hasattr(plugin, "version")
        assert isinstance(plugin.version, str)
        assert plugin.version == "0.1.0"

        assert hasattr(plugin, "api_version")
        assert isinstance(plugin.api_version, str)
        assert plugin.api_version == "1.0.0"
    finally:
        # Restore the original module to avoid poisoning other tests
        if _original_plugin_api is not None:
            sys.modules["app.plugin_api"] = _original_plugin_api


def test_example_plugin_registers_search_provider():
    """The example plugin must register a search provider when register() is called."""
    sys.path.insert(0, str(EXAMPLE_PLUGIN_DIR))

    # Mock dependencies
    mock_registry = MagicMock()
    mock_registry.search_registry = MagicMock()
    mock_registry.search_registry.all = MagicMock(return_value=[])

    mock_http_client = MagicMock()
    mock_http_client.__aenter__ = MagicMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = MagicMock(return_value=None)

    import test_plugin.plugin

    plugin = test_plugin.plugin.TestPlugin()

    # Call register - this should not raise
    plugin.register(mock_registry, None)

    # Verify a search provider was registered
    mock_registry.register_search_provider.assert_called_once()
