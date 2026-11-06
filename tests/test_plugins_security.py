"""Tests for plugin security features.

These tests verify the allowlist, denylist, and failure isolation
features implemented per GitHub issue #174.

"""

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.plugins.loader import load_plugins

VALID_API_KEY = "test-key-for-tests"
AUTH_HEADER = {"Authorization": f"Bearer {VALID_API_KEY}"}


def test_plugins_status_missing_load_result():
    """/plugins/status must return not_loaded status when plugin_load_result is absent."""
    # Ensure plugin_load_result is not set (cleanup from previous tests)
    if hasattr(app.state, "plugin_load_result"):
        delattr(app.state, "plugin_load_result")

    # Configure app with API key
    settings = Settings(api_key=VALID_API_KEY)
    app.state.settings = settings

    client = TestClient(app)
    resp = client.get("/v1/plugins/status", headers=AUTH_HEADER)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "not_loaded"
    assert "message" in data
    assert "Plugins have not been loaded yet" in data["message"]
    assert data["loaded"] == []
    assert data["skipped"] == []
    assert data["failed"] == []
    assert data["enabled"] == []
    assert data["on_error"] == "fail"


def test_plugins_status_loaded():
    """/plugins/status must return loaded status with all plugin metadata."""
    settings = Settings(api_key=VALID_API_KEY)
    # Allowlist with both plugins, denylist blocks one
    settings.raw_yaml_plugin = {
        "enabled": True,
        "allowlist": ["mock-plugin", "blocked-plugin"],
        "denylist": ["blocked-plugin"],
    }
    app.state.settings = settings

    class MockPlugin:
        name = "mock-plugin"
        version = "1.0.0"
        api_version = "1.0.0"

        def register(self, registry, ctx):
            pass

    class BlockedPlugin:
        name = "blocked-plugin"
        version = "1.0.0"
        api_version = "1.0.0"

        def register(self, registry, ctx):
            pass

    class FakeEntryPoint:
        def __init__(self, name, value):
            self.name = name
            self.value = value

        def load(self):
            return MockPlugin if self.name == "mock-plugin" else BlockedPlugin

    def mock_entry_points(*args, **kwargs):
        class MockEP:
            def __init__(self, name, value):
                self.name = name
                self.value = value

            def load(self):
                return MockPlugin if self.name == "mock-plugin" else BlockedPlugin

        return [MockEP("mock-plugin", "x"), MockEP("blocked-plugin", "y")]

    import importlib.metadata as metadata

    original_entry_points = metadata.entry_points

    try:
        metadata.entry_points = mock_entry_points

        # Simulate loading with mock plugin in allowlist, blocked-plugin in denylist
        result = load_plugins(settings, http_client=None)

        assert result.loaded == ["mock-plugin"]
        assert len(result.skipped) == 1
        assert result.skipped[0]["name"] == "blocked-plugin"
        # denylist removes from enabled list
        assert result.enabled == ["mock-plugin"]
        assert result.on_error == "fail"
        assert result.failed == []

        # Now test via API
        # Set plugin_load_result directly
        app.state.plugin_load_result = result

        client = TestClient(app)
        resp = client.get("/v1/plugins/status", headers=AUTH_HEADER)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "loaded"
        assert data["loaded"] == ["mock-plugin"]
        # Verify denylist reason in skipped
        skipped_reason = data["skipped"][0]["reason"]
        assert "denied" in skipped_reason.lower() or "denylist" in skipped_reason.lower()
        assert data["enabled"] == ["mock-plugin"]
        assert data["on_error"] == "fail"
        assert data["failed"] == []
        assert data["allowlist_enabled"] is True
        assert data["allowlist"] == ["mock-plugin", "blocked-plugin"]
        assert data["denylist"] == ["blocked-plugin"]
    finally:
        metadata.entry_points = original_entry_points


def test_per_plugin_env_vars():
    """SCRAPER_PLUGIN_<NAME>_.* env vars should be available to plugins."""
    import os

    settings = Settings(api_key=VALID_API_KEY)
    settings.raw_yaml_plugin = {"enabled": True, "allowlist": ["mock-plugin"]}
    app.state.settings = settings

    captured_env = {}

    class MockPlugin:
        name = "mock-plugin"
        version = "1.0.0"
        api_version = "1.0.0"

        def register(self, registry, ctx):
            # Capture per-plugin env from ctx.per_plugin_env
            captured_env.update(getattr(ctx, "per_plugin_env", {}) or {})

    class FakeEntryPoint:
        def __init__(self, name, value):
            self.name = name
            self.value = value

        def load(self):
            return MockPlugin

    def mock_entry_points(*args, **kwargs):
        return [FakeEntryPoint("mock-plugin", "x")]

    import importlib.metadata as metadata

    original_entry_points = metadata.entry_points

    try:
        # Set per-plugin env var
        os.environ["SCRAPER_PLUGIN_MOCK_PLUGIN_API_KEY"] = "secret-value"
        os.environ["SCRAPER_PLUGIN_MOCK_PLUGIN_TIMEOUT"] = "30"

        metadata.entry_points = mock_entry_points

        result = load_plugins(settings, http_client=None)

        assert result.loaded == ["mock-plugin"]
        # Verify per-plugin env vars were captured during plugin registration
        assert captured_env.get("api-key") == "secret-value"
        assert captured_env.get("timeout") == "30"
    finally:
        os.environ.pop("SCRAPER_PLUGIN_MOCK_PLUGIN_API_KEY", None)
        os.environ.pop("SCRAPER_PLUGIN_MOCK_PLUGIN_TIMEOUT", None)
        metadata.entry_points = original_entry_points


def test_on_error_warn_mode():
    """on_error="warn" should allow startup even with plugin failures."""
    import os

    os.environ["SCRAPER_PLUGINS_ON_ERROR"] = "warn"
    try:
        settings = Settings(api_key=VALID_API_KEY)
        settings.raw_yaml_plugin = {
            "enabled": True,
            "allowlist": ["failing-plugin"],
        }
        app.state.settings = settings

        class FailingPlugin:
            name = "failing-plugin"
            version = "1.0.0"
            api_version = "1.0.0"

            def register(self, registry, ctx):
                raise RuntimeError("Intentional failure for testing")

        class FakeEntryPoint:
            def __init__(self, name, value):
                self.name = name
                self.value = value

            def load(self):
                return FailingPlugin

        import importlib.metadata as metadata

        original_entry_points = metadata.entry_points

        try:
            metadata.entry_points = lambda *args, **kwargs: [FakeEntryPoint("failing-plugin", "x")]

            result = load_plugins(settings, http_client=None)

            # Plugin failed but startup continued in warn mode
            assert result.loaded == []
            assert len(result.failed) == 1
            assert result.failed[0]["name"] == "failing-plugin"
            assert result.on_error == "warn"
            assert len(result.skipped) == 1  # Failed plugin is skipped
        finally:
            metadata.entry_points = original_entry_points
    finally:
        os.environ.pop("SCRAPER_PLUGINS_ON_ERROR", None)
