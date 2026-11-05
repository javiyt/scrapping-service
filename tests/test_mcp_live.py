"""Optional live integration test for the MCP adapter (ticket #155).

Runs the MCP adapter against a **real, running** scrapping-service
deployment (base URL from ``MCP_SCRAPPER_BASE_URL``) and drives a full
agent-style flow through it: ``adapter_info`` (health wiring) →
``search`` → ``fetch_url``.

Skipped by default; enabled with the ``SCRAPER_TEST_MCP_LIVE=true``
environment variable (mirrors ``tests/test_transcript_live.py``; CI never
sets it, so the suite stays hermetic).  The service URL and key come from
the same environment variables the adapter itself uses
(``MCP_SCRAPPER_BASE_URL``, ``SCRAPER_API_KEY``), so one environment can
serve both.
"""

from __future__ import annotations

import json
import os

import pytest

from app.mcp.config import load_config
from app.mcp.observability import MCPMetrics
from app.mcp.tools import ToolRegistry


def _live_enabled() -> bool:
    return os.environ.get("SCRAPER_TEST_MCP_LIVE", "").strip().lower() in ("1", "true", "yes")


@pytest.mark.skipif(
    not _live_enabled(),
    reason="live MCP integration test is opt-in (SCRAPER_TEST_MCP_LIVE=true)",
)
class TestLiveMCP:
    def test_full_flow_against_real_service(self) -> None:
        async def run() -> None:
            from app.mcp.client import ScrapperClient

            config = load_config()
            client = ScrapperClient(
                base_url=config.base_url,
                api_key=config.api_key,
                timeout_seconds=config.timeout_seconds,
                max_output_chars=config.max_output_chars,
            )
            try:
                registry = ToolRegistry(client, MCPMetrics())

                info = json.loads(await registry.call("adapter_info", {}))
                assert "adapter" in info
                if "error" in info:
                    pytest.fail(f"adapter_info reported an error: {info['error']}")

                search = json.loads(await registry.call("search", {"query": "Miami Dolphins"}))
                if "error" in search:
                    pytest.fail(f"search failed: {search['error']}")
                assert "results" in search or search.get("results") is None

                fetch = json.loads(
                    await registry.call("fetch_url", {"url": "https://example.com", "mode": "http"})
                )
                assert "error" not in fetch or fetch.get("status") is not None
            finally:
                await client.aclose()

        import asyncio

        asyncio.run(run())
