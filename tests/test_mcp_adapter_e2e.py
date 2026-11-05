"""End-to-end MCP protocol tests (ticket #150).

Drives a real MCP client session against the adapter server over in-memory
streams — exercising the full JSON-RPC protocol: initialize, list tools,
call tools, error paths, and output bounding.  The scrapping-service itself
is a mock transport; the MCP stack is the real thing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from app.mcp.client import ScrapperClient
from app.mcp.config import MCPAdapterConfig
from app.mcp.server import MCPServer


def _ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


SERVICE_HEALTH = {"status": "ok", "version": "1.0.0", "service": "scraper-api"}


@pytest.fixture(scope="module")
def session_factory() -> Any:
    """Factory that yields an initialized MCP client session over memory streams."""

    def make(handler: Callable[[httpx.Request], httpx.Response]):
        client = ScrapperClient(
            base_url="http://service.local:8080",
            api_key="e2e-key",
            transport=httpx.MockTransport(handler),
            max_output_chars=5_000,
        )
        server = MCPServer(MCPAdapterConfig(base_url="http://service.local:8080"), client)
        return create_connected_server_and_client_session(server.run_fn())

    return make


def _text(result: Any) -> str:
    """Extract the first text block from a CallToolResult."""
    for block in result.content:
        if hasattr(block, "text"):
            return block.text
    return ""


def test_full_protocol_flow(session_factory: Any) -> None:
    """initialize → list tools → healthy call → error call, all over real MCP."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return _ok(SERVICE_HEALTH)
        if path == "/v1/search":
            body = json.loads(request.content)
            return _ok(
                {
                    "query": body.get("query"),
                    "results": [{"title": "R", "url": "https://r.example", "rank": 1}],
                }
            )
        if path == "/v1/transcript":
            return _ok(
                {
                    "video_id": request.url.params.get("video_id"),
                    "title": "T",
                    "text": "hello world",
                    "total_words": 2,
                }
            )
        return _ok({"ok": True})

    async def run() -> None:
        async with session_factory(handler) as cs:
            # -- initialize (the SDK does this on session entry) --
            tools = await cs.list_tools()
            names = sorted(t.name for t in tools.tools)
            assert "search" in names and "fetch_url" in names and "adapter_info" in names

            # -- adapter_info: config + health in one call --
            res = await cs.call_tool("adapter_info", {})
            data = json.loads(_text(res))
            assert data["service"]["status"] == "ok"
            assert data["adapter"]["config"]["api_key_configured"] is True

            # -- search: argument mapping through the protocol --
            res = await cs.call_tool("search", {"query": "Dolphins", "max_results": 5})
            data = json.loads(_text(res))
            assert data["query"] == "Dolphins"
            assert data["results"][0]["rank"] == 1

            # -- transcript: query-param mapping --
            res = await cs.call_tool("transcript", {"video_id": "dQw4w9WgXcQ"})
            data = json.loads(_text(res))
            assert data["video_id"] == "dQw4w9WgXcQ"

    import asyncio

    asyncio.run(run())


def test_service_error_over_protocol(session_factory: Any) -> None:
    """A service 5xx surfaces as an error result (isError=True) with a clean message."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/search":
            return httpx.Response(
                502,
                json={
                    "error": {
                        "type": "search_error",
                        "message": "all providers failed",
                        "details": {},
                    }
                },
            )
        return _ok({})

    async def run() -> None:
        async with session_factory(handler) as cs:
            res = await cs.call_tool("search", {"query": "x"})
            assert res.isError is True
            text = _text(res)
            data = json.loads(text)
            assert data["error"]["type"] == "search_error"  # the service's own error type
            assert "all providers failed" in data["error"]["message"]
            assert data["error"]["details"]["status_code"] == 502

    import asyncio

    asyncio.run(run())


def test_output_truncation_over_protocol(session_factory: Any) -> None:
    """A huge service response is bounded to the configured max_output_chars."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"content": "z" * 100_000})

    async def run() -> None:
        async with session_factory(handler) as cs:
            res = await cs.call_tool("fetch_url", {"url": "https://example.com/big"})
            text = _text(res)
            assert len(text) < 100_000
            assert "truncated" in text

    import asyncio

    asyncio.run(run())


def test_invalid_arguments_rejected_over_protocol(session_factory: Any) -> None:
    """Wrong argument types are rejected before any network I/O."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _ok({})

    async def run() -> None:
        async with session_factory(handler) as cs:
            res = await cs.call_tool("search", {"query": 42})
            data = json.loads(_text(res))
            assert data["error"]["type"] == "adapter_error"
            assert calls == []

    import asyncio

    asyncio.run(run())


def test_browser_session_roundtrip(session_factory: Any) -> None:
    """The full browser lifecycle: create → navigate → close, with snapshot."""

    state: dict[str, Any] = {"sessions": {}, "next_id": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/browser/capabilities":
            return _ok({"enabled": True, "max_sessions": 5, "max_tabs_per_session": 8})
        if path == "/v1/browser/sessions" and request.method == "POST":
            sid = f"ses_{state['next_id']}"
            state["next_id"] += 1
            state["sessions"][sid] = {"tabs": []}
            return httpx.Response(
                201, json={"session": {"session_id": sid, "name": None, "tabs": []}}
            )
        if path == "/v1/browser/sessions" and request.method == "DELETE":
            sid = path.rsplit("/", 1)[1]
            state["sessions"].pop(sid, None)
            return _ok({})  # the service's DELETE session route returns 204/empty
        if path.endswith("/tabs") and request.method == "POST":
            sid = path.split("/sessions/")[1].split("/")[0]
            tab = {"tab_id": f"tab_{sid}", "url": "about:blank"}
            state["sessions"].setdefault(sid, {"tabs": []})["tabs"].append(tab)
            return httpx.Response(
                201,
                json={
                    "tab": tab,
                    "snapshot": {
                        "url": "about:blank",
                        "title": "Example",
                        "elements": [{"ref": "@e1", "role": "link"}],
                    },
                },
            )
        if path.endswith("/snapshot"):
            return _ok(
                {
                    "url": "https://example.com",
                    "title": "Example",
                    "elements": [{"ref": "@e1", "role": "link"}],
                }
            )
        return _ok({"ok": True})

    async def run() -> None:
        async with session_factory(handler) as cs:
            res = await cs.call_tool("browser_create_session", {})
            data = json.loads(_text(res))
            sid = data["session"]["session_id"]
            assert sid.startswith("ses_")

            res = await cs.call_tool(
                "browser_navigate", {"session_id": sid, "url": "https://example.com"}
            )
            data = json.loads(_text(res))
            assert data["snapshot"]["title"] == "Example"
            assert data["snapshot"]["elements"][0]["ref"] == "@e1"

            res = await cs.call_tool("browser_close_session", {"session_id": sid})
            assert not res.isError  # the DELETE round-tripped cleanly

    import asyncio

    asyncio.run(run())
