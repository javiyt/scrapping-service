"""Tests for the /v1/plugins endpoints.

These tests verify the plugin health and metadata endpoints without
actually loading real plugins (uses PluginLoadResult directly).
"""

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.plugins.loader import PluginLoadResult

VALID_API_KEY = "test-key-for-tests"
AUTH_HEADER = {"Authorization": f"Bearer {VALID_API_KEY}"}


def _set_clean_app_state():
    """Clean and set up app.state with required attributes."""
    # Clean state
    for attr in ["plugin_load_result", "settings"]:
        if hasattr(app.state, attr):
            delattr(app.state, attr)

    # Set required attributes
    app.state.settings = Settings(api_key=VALID_API_KEY)


def test_plugins_endpoint_without_auth():
    """GET /v1/plugins without auth should return 401."""
    _set_clean_app_state()
    client = TestClient(app)
    response = client.get("/v1/plugins")
    assert response.status_code == 401


def test_plugins_endpoint_with_auth():
    """GET /v1/plugins with auth should return plugin metadata."""
    _set_clean_app_state()

    # Simulate what load_plugins returns: loaded is a list of metadata dicts
    # The endpoint iterates over loaded and reads metadata from entries
    mock_metadata = {
        "name": "sample-plugin",
        "version": "1.2.3",
        "api_version": "1.0.0",
        "service_api_version": "1.0.0",
        "status": "loaded",
        "reason": None,
        "extensions": {
            "search_providers": ["sample-search"],
            "transcript_providers": ["sample-transcript"],
            "browser_provider": None,
            "extractors": [],
        },
    }

    # Create a mock result where loaded contains dicts with metadata attribute
    class MockPluginEntry:
        def __init__(self, metadata):
            self.metadata = metadata

    app.state.plugin_load_result = PluginLoadResult(
        search_providers=[],
        transcript_providers=[],
        browser_provider=None,
        extractor_registry=None,
        loaded=[MockPluginEntry(mock_metadata)],
        skipped=[],
        failed=[],
        enabled=["sample-plugin"],
        on_error="fail",
        metadata=[mock_metadata],
    )
    client = TestClient(app)

    response = client.get("/v1/plugins", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert "plugins" in data
    assert len(data["plugins"]) == 1
    assert data["plugins"][0]["name"] == "sample-plugin"
    assert data["plugins"][0]["version"] == "1.2.3"


def test_plugins_endpoint_with_no_plugins():
    """GET /v1/plugins with no plugins loaded should return empty list."""
    _set_clean_app_state()

    app.state.plugin_load_result = PluginLoadResult(
        search_providers=[],
        transcript_providers=[],
        browser_provider=None,
        extractor_registry=None,
        loaded=[],
        skipped=[],
        failed=[],
        enabled=[],
        on_error="fail",
        metadata=[],
    )
    client = TestClient(app)

    response = client.get("/v1/plugins", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert "plugins" in data
    assert data["plugins"] == []


def test_plugins_status_endpoint_without_auth():
    """GET /v1/plugins/status without auth should return 401."""
    _set_clean_app_state()
    client = TestClient(app)
    response = client.get("/v1/plugins/status")
    assert response.status_code == 401


def test_plugins_status_endpoint_with_auth():
    """GET /v1/plugins/status with auth should return status."""
    _set_clean_app_state()

    app.state.plugin_load_result = PluginLoadResult(
        search_providers=[],
        transcript_providers=[],
        browser_provider=None,
        extractor_registry=None,
        loaded=["sample-plugin"],
        skipped=[],
        failed=[],
        enabled=["sample-plugin"],
        on_error="fail",
        metadata=[],
    )
    client = TestClient(app)

    response = client.get("/v1/plugins/status", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    assert "status" in data
    assert "plugins" not in data  # status endpoint doesn't return plugins field
    assert data["loaded"] == ["sample-plugin"]
    assert "message" in data


def test_plugins_status_endpoint_with_failed_plugin():
    """GET /v1/plugins/status with failed plugin should report error."""
    _set_clean_app_state()

    app.state.plugin_load_result = PluginLoadResult(
        search_providers=[],
        transcript_providers=[],
        browser_provider=None,
        extractor_registry=None,
        loaded=[],
        skipped=[],
        failed=[{"name": "broken-plugin", "reason": "ImportError: no module"}],
        enabled=[],
        on_error="fail",
        metadata=[],
    )
    client = TestClient(app)

    response = client.get("/v1/plugins/status", headers=AUTH_HEADER)
    assert response.status_code == 200
    data = response.json()
    # When no loaded and failed, status should be "no_plugins"
    assert data["loaded"] == []
    assert data["failed"] == [{"name": "broken-plugin", "reason": "ImportError: no module"}]
