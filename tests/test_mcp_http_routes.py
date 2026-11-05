"""HTTP route tests for the MCP adapter (ticket #155).

Pins the adapter's ops surface for the streamable-HTTP transport:
``/health`` (adapter up + service reachability, no secrets) and
``/metrics`` (adapter Prometheus counters).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.mcp.client import ScrapperClient
from app.mcp.config import MCPAdapterConfig
from app.mcp.server import MCPServer, create_server


def _config() -> MCPAdapterConfig:
    return MCPAdapterConfig(base_url="http://s", api_key="secret-value")


def _client(handler: httpx.MockTransport) -> ScrapperClient:
    return ScrapperClient(base_url="http://s", api_key="secret-value", transport=handler)


def _app_for(client: ScrapperClient, metrics: Any = None) -> TestClient:
    from starlette.applications import Starlette

    from app.mcp.observability import MCPMetrics

    app = Starlette()
    server = MCPServer(_config(), client, metrics or MCPMetrics())
    server.mount_http(app)
    return TestClient(app, raise_server_exceptions=False)


def test_health_reports_adapter_and_service_state() -> None:
    handler = httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok"}))
    client = _app_for(_client(handler))
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["adapter"]["up"] is True
    assert body["service"]["reachable"] is True
    assert body["service"]["body"] == {"status": "ok"}


def test_health_degrades_when_service_down() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = _app_for(_client(handler))
    body = client.get("/health").json()
    assert body["adapter"]["up"] is True  # the adapter itself is fine
    assert body["service"]["reachable"] is False
    assert "secret" not in str(body)


def test_metrics_renders_prometheus() -> None:
    import asyncio

    from starlette.applications import Starlette

    from app.mcp.observability import MCPMetrics

    handler = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    client = _client(handler)
    metrics = MCPMetrics()
    server = MCPServer(_config(), client, metrics)
    asyncio.run(server.registry.call("adapter_info", {}))
    app = Starlette()
    server.mount_http(app)
    test_client = TestClient(app, raise_server_exceptions=False)
    response = test_client.get("/metrics")
    assert response.status_code == 200
    assert "scraper_mcp_tool_calls_total" in response.text
    assert '"tool"="adapter_info"' in response.text
    assert "secret" not in response.text


def test_create_server_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_SCRAPPER_BASE_URL", "http://service")
    monkeypatch.setenv("MCP_SCRAPPER_API_KEY", "env-key")
    server = create_server()
    assert server.config.base_url == "http://service"
    assert server.config.api_key == "env-key"


def test_create_server_explicit_config() -> None:
    server = create_server(_config())
    assert server.registry.names()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
