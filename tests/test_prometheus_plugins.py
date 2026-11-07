"""Tests for plugin metrics in the Prometheus collector."""

from app.metrics.prometheus import MetricsCollector


def test_observe_plugin_call_increments_counter():
    """observe_plugin_call should increment plugin_provider_attempt_total and record latency."""
    m = MetricsCollector()
    m.observe_plugin_call("search", "sample-plugin", 42.5, ok=True)
    
    rendered = m.render()
    assert "plugin_provider_attempts_total" in rendered
    assert '{"provider"="sample-plugin"}' in rendered
    assert "plugin_provider_duration_ms_sum" in rendered


def test_observe_plugin_call_error_status():
    """observe_plugin_call with ok=False should increment errors counter."""
    m = MetricsCollector()
    m.observe_plugin_call("transcript", "broken-plugin", 100.0, ok=False)
    
    rendered = m.render()
    assert '{"provider"="broken-plugin"}' in rendered
    assert "plugin_provider_errors_total" in rendered
    assert "plugin_provider_success_total" in rendered


def test_register_plugin_metadata_gauges():
    """register_plugin_metadata should set plugins_loaded and plugins_failed gauges."""
    m = MetricsCollector()
    m.register_plugin_metadata(
        loaded_count=2,
        failed_count=1,
        entries=[{
            "name": "good-plugin",
            "version": "1.0.0",
            "api_version": "1.0.0",
            "service_api_version": "1.0.0",
            "status": "loaded",
            "reason": None,
            "extensions": {"search_providers": ["gs"], "transcript_providers": ["gt"], "browser_provider": None, "extractors": []},
        }, {
            "name": "bad-plugin",
            "version": "unknown",
            "api_version": "unknown",
            "service_api_version": "1.0.0",
            "status": "failed",
            "reason": "ImportError",
            "extensions": {"search_providers": [], "transcript_providers": [], "browser_provider": None, "extractors": []},
        }],
    )
    
    rendered = m.render()
    assert "plugins_loaded" in rendered
    assert "plugins_failed" in rendered
    assert "plugins_loaded 2" in rendered
    assert "plugins_failed 1" in rendered


def test_plugin_metrics_success_error_counts():
    """Counter should separate ok vs error counts per plugin."""
    m = MetricsCollector()
    m.observe_plugin_call("search", "plugin", 10.0, ok=True)
    m.observe_plugin_call("search", "plugin", 20.0, ok=False)
    
    rendered = m.render()
    # Both calls increment attempts; success and error are separate counters
    assert '{"provider"="plugin"}' in rendered
    assert 'plugin_provider_attempts_total{"provider"="plugin"} 2' in rendered
    assert 'plugin_provider_success_total{"provider"="plugin"} 1' in rendered
    assert 'plugin_provider_errors_total{"provider"="plugin"} 1' in rendered
