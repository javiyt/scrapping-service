"""Observability tests for the MCP adapter (ticket #155).

Pins the two guarantees the ticket requires:

1. tool-call logging carries tool name, duration and outcome — and never a
   payload or secret;
2. the adapter's metrics counters are correct for a mixed success/failure
   workload and render valid Prometheus text.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from app.mcp.client import ScrapperClient
from app.mcp.observability import MCPMetrics, ToolCallContext
from app.mcp.tools import ToolRegistry


def _client() -> ScrapperClient:
    handler = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True}))
    return ScrapperClient(base_url="http://s", api_key="secret-value", transport=handler)


class TestToolCallContext:
    def test_context_captures_tool_and_request_id(self) -> None:
        ctx = ToolCallContext("search", "req-1")
        assert ctx.tool == "search"
        assert ctx.request_id == "req-1"
        assert ctx.profile_id is None

    def test_log_line_carries_name_duration_outcome_and_no_secret(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="scraper-api.mcp"):
            ctx = ToolCallContext("search", "req-1")
            ctx.log_end("ok")
        record = [r for r in caplog.records if "mcp_tool_call" in r.getMessage()]
        assert record, "a structured tool-call line must be emitted"
        payload = json.loads(record[-1].message.split("mcp_tool_call ", 1)[1])
        assert payload["tool"] == "search"
        assert payload["request_id"] == "req-1"
        assert payload["result"] == "ok"
        assert "duration_ms" in payload
        assert "secret" not in record[-1].getMessage()

    def test_log_end_never_raises(self) -> None:
        ctx = ToolCallContext("search", "req-1", profile_id="prof-1")
        ctx.log_end("service_error", status=502)
        ctx.log_end("internal_error")


class TestMCPMetrics:
    def test_tool_call_counters_render_prometheus(self) -> None:
        metrics = MCPMetrics()
        metrics.record_tool_call("search", "ok", 0.1)
        metrics.record_tool_call("search", "service_error", 0.2)
        metrics.record_tool_call("fetch_url", "ok", 0.3)
        text = metrics.render_prometheus()
        assert 'tool_calls_total{"result"="ok","tool"="search"} 1' in text
        assert 'tool_calls_total{"result"="service_error","tool"="search"} 1' in text
        assert 'tool_calls_total{"result"="ok","tool"="fetch_url"} 1' in text
        assert "scraper_mcp_uptime_seconds" in text

    def test_same_series_is_updated_not_duplicated(self) -> None:
        metrics = MCPMetrics()
        metrics.record_tool_call("search", "ok", 0.1)
        metrics.record_tool_call("search", "ok", 0.2)
        text = metrics.render_prometheus()
        assert text.count('"result"="ok","tool"="search"') == 1
        assert '{"result"="ok","tool"="search"} 2' in text

    def test_render_lines_are_prometheus_grammar(self) -> None:
        metrics = MCPMetrics()
        metrics.record_tool_call("search", "ok", 0.1)
        metrics.record_session_error("protocol")
        for line in metrics.render_prometheus().strip().splitlines():
            assert line.startswith("#") or " " in line, f"not valid exposition: {line!r}"

    def test_recording_never_raises(self) -> None:
        metrics = MCPMetrics()
        for tool in ("search", "fetch_url", "browser_navigate"):
            for result in ("ok", "service_error", "internal_error"):
                metrics.record_tool_call(tool, result, 0.1)
        metrics.record_session_error("boom")
        metrics.render_prometheus()


class TestRegistryObservability:
    async def test_mixed_workload_updates_metrics_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        metrics = MCPMetrics()
        registry = ToolRegistry(_client(), metrics)
        with caplog.at_level(logging.INFO, logger="scraper-api.mcp"):
            await registry.call("adapter_info", {})
            await registry.call("search", {"query": 123})  # invalid arg → adapter_error
        text = metrics.render_prometheus()
        assert '"tool"="adapter_info"' in text
        assert '"tool"="search"' in text
        # Both calls were logged with name + outcome.
        logged = [r.message for r in caplog.records if "mcp_tool_call" in r.getMessage()]
        assert len(logged) == 2
        assert any("adapter_info" in line and '"ok"' in line for line in logged)
        assert any('"search"' in line and "adapter_error" in line for line in logged)
        # No secret in any log line.
        assert all("secret-value" not in line for line in logged)

    def test_full_render_after_workload(self) -> None:
        async def run() -> str:
            registry = ToolRegistry(_client(), MCPMetrics())
            await registry.call("adapter_info", {})
            await registry.call("search", {"query": "x"})
            return registry._metrics.render_prometheus()

        text = asyncio.run(run())
        assert "adapter_info" in text
        assert "search" in text
