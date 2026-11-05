"""MCP server assembly and entry points for the scrapping-service adapter.

Transports:

- ``stdio`` (default) — JSON-RPC over stdio; this is what MCP hosts (Pi,
  Claude Desktop, Cursor, OpenCode, ``uvx``) launch.
- ``streamable-http`` — the JSON-RPC endpoint mounted on a small standalone
  ASGI app (the adapter binds its own port, independent of the service's
  8080), plus the adapter's own ``/health`` and ``/metrics`` routes.

The server runs the official ``mcp`` SDK loop directly (no FastMCP
convenience layer) so the request/response mapping stays fully
deterministic and testable: the same handlers back every transport.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions

from app.mcp import MCP_ADAPTER_VERSION
from app.mcp.client import ScrapperClient
from app.mcp.config import MCPAdapterConfig, MCPConfigError, load_config
from app.mcp.http_routes import health_route, metrics_route
from app.mcp.observability import MCPMetrics
from app.mcp.tools import ToolRegistry

logger = logging.getLogger("scraper-api.mcp.server")


def run_server(server: MCPServer) -> Any:
    """Adapter shim for ``create_connected_server_and_client_session``."""
    return server.run_fn()


def _init_options() -> InitializationOptions:
    return InitializationOptions(
        server_name="scrapping-service",
        server_version=MCP_ADAPTER_VERSION,
        capabilities=types.ServerCapabilities(tools={"listChanged": False}),
        instructions=(
            "Tools of the scrapping-service: web search (search, search_capabilities), "
            "URL fetch (fetch_url, fetch_url_batch), YouTube transcripts (transcript), "
            "interactive browser (browser_capabilities, browser_create_session, "
            "browser_navigate, browser_click, browser_type, browser_evaluate, "
            "browser_close_tab, browser_close_session) and cache maintenance "
            "(cache_purge, cache_delete, cache_cleanup, cache_vacuum, cache_stats). "
            "All tools proxy the service's REST API: the same security policies, "
            "caching and error contracts apply. Call adapter_info first to verify wiring."
        ),
    )


def _run_loop(
    server: Server,
    read_stream: Any,
    write_stream: Any,
    init_options: InitializationOptions,
    *,
    raise_exceptions: bool = False,
) -> Any:
    """Run the SDK's JSON-RPC loop over the given streams (used by all transports)."""
    return server.run(read_stream, write_stream, init_options, raise_exceptions=raise_exceptions)


class MCPServer:
    """The adapter's MCP server bound to one :class:`ScrapperClient`."""

    def __init__(
        self, config: MCPAdapterConfig, client: ScrapperClient, metrics: MCPMetrics | None = None
    ) -> None:
        self.config = config
        self.client = client
        self.metrics = metrics or MCPMetrics()
        self.registry = ToolRegistry(client, self.metrics)
        self._server = Server(name="scrapping-service", version=MCP_ADAPTER_VERSION)
        self._wire_handlers()

    # ------------------------------------------------------------- handlers
    def _wire_handlers(self) -> None:
        registry = self.registry

        @self._server.list_tools()
        async def _list_tools() -> list[types.Tool]:
            return [types.Tool(**spec) for spec in registry.list_tools()]

        @self._server.call_tool(validate_input=False)
        async def _call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
            from pydantic_core import ValidationError  # noqa: PLC0415

            try:
                text = await registry.call(name, arguments or {})
            except ValidationError as exc:
                text = json.dumps(
                    {
                        "error": {
                            "type": "adapter_error",
                            "message": (
                                f"Invalid arguments for '{name}': "
                                f"{exc.error_count()} field error(s)"
                            ),
                            "details": {},
                        }
                    }
                )
            is_error = False
            try:
                parsed = json.loads(text)
                is_error = isinstance(parsed, dict) and "error" in parsed
            except (json.JSONDecodeError, ValueError):
                pass
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=text)],
                isError=is_error,
            )

    # -------------------------------------------------------------- running
    async def run_stdio(self) -> None:
        """Run the JSON-RPC loop over stdio (the default entry point)."""
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await _run_loop(self._server, read_stream, write_stream, _init_options())

    def run_fn(self) -> Any:
        """An object compatible with the SDK's in-memory test helper.

        ``mcp.shared.memory.create_connected_server_and_client_session``
        expects an object with ``.run(read, write, init_options, *,
        raise_exceptions)`` and ``.create_initialization_options()`` — the
        ``run`` signature matches ``mcp.server.lowlevel.Server.run``.
        """
        server = self._server
        init_options = _init_options()

        async def _run(
            read_stream: Any,
            write_stream: Any,
            _options: Any,
            *,
            raise_exceptions: bool = False,
        ) -> None:
            await _run_loop(
                server, read_stream, write_stream, init_options, raise_exceptions=raise_exceptions
            )

        return SimpleNamespace(run=_run, create_initialization_options=lambda: init_options)

    def mount_http(self, asgi_app: Any) -> None:
        """Mount the streamable-HTTP ``/mcp`` endpoint plus ops routes."""
        from mcp.server.streamable_http import StreamableHTTPServerTransport

        transport = StreamableHTTPServerTransport(
            mcp_session_id="scrapping-service-mcp",
            is_json_response_enabled=True,
        )
        asgi_app.state.mcp_transport = transport

        async def mcp_endpoint(scope: Any, receive: Any, send: Any) -> None:
            async with transport.connect() as (read_stream, write_stream):
                await _run_loop(self._server, read_stream, write_stream, _init_options())

        asgi_app.add_route("/mcp", mcp_endpoint)
        asgi_app.add_route("/health", health_route(self.client, self.metrics))
        asgi_app.add_route("/metrics", metrics_route(self.metrics, self.client))
        logger.info("MCP streamable-http endpoint mounted at /mcp")

    async def aclose(self) -> None:
        await self.client.aclose()


def create_server(
    config: MCPAdapterConfig | None = None,
    *,
    transport: Any = None,
    metrics: MCPMetrics | None = None,
) -> MCPServer:
    """Build the adapter from the environment (or an explicit config).

    ``transport`` injects an ``httpx.AsyncBaseTransport`` (tests use
    ``httpx.MockTransport``); production passes ``None``.
    """
    cfg = config or load_config()
    client = ScrapperClient(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        timeout_seconds=cfg.timeout_seconds,
        max_output_chars=cfg.max_output_chars,
        transport=transport,
    )
    return MCPServer(cfg, client, metrics)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> None:
    """``scrapping-service-mcp``: run the stdio MCP server."""
    _configure_logging()
    try:
        server = create_server()
    except MCPConfigError as exc:
        print(f"scrapping-service-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    try:
        asyncio.run(server.run_stdio())
    except (KeyboardInterrupt, SystemExit):
        pass


def main_http() -> None:
    """``scrapping-service-mcp-http``: streamable HTTP + ops routes.

    Binds 0.0.0.0:8090 by default; override with ``MCP_HTTP_PORT``.
    """
    _configure_logging()
    from starlette.applications import Starlette

    app = Starlette()
    try:
        server = create_server()
    except MCPConfigError as exc:
        print(f"scrapping-service-mcp-http: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    server.mount_http(app)
    app.router.lifespan_context = _http_lifespan(server)

    import uvicorn

    port = int(os.environ.get("MCP_HTTP_PORT", "8090"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def _http_lifespan(server: MCPServer) -> Callable[..., Any]:
    @asynccontextmanager
    async def lifespan(app: Any) -> Any:
        try:
            yield
        finally:
            await server.aclose()

    return lifespan
