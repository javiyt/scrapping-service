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


# The tool catalogue advertised to MCP clients during the initialize
# handshake. Kept as a module constant so the Server instance is the single
# source of truth for its identity (name, version, instructions, and the
# capabilities it derives from the handlers it registers).
_ADAPTER_INSTRUCTIONS = (
    "Tools of the scrapping-service: web search (search, search_capabilities), "
    "URL fetch (fetch_url, fetch_url_batch), YouTube transcripts (transcript), "
    "interactive browser (browser_capabilities, browser_create_session, "
    "browser_navigate, browser_click, browser_type, browser_evaluate, "
    "browser_close_tab, browser_close_session) and cache maintenance "
    "(cache_purge, cache_delete, cache_cleanup, cache_vacuum, cache_stats). "
    "All tools proxy the service's REST API: the same security policies, "
    "caching and error contracts apply. Call adapter_info first to verify wiring."
)


def _run_loop(server: Server, read_stream: Any, write_stream: Any, *, raise_exceptions: bool = False) -> Any:
    """Run the SDK's JSON-RPC loop over the given streams.

    The lowlevel Server carries its own identity and derives its
    initialization options (name, version, instructions, and the capabilities
    implied by the handlers it registered), so every transport — stdio, streamable-HTTP,
    and the in-memory test client — drives the same options.
    """
    return server.run(read_stream, write_stream, server.create_initialization_options(), raise_exceptions=raise_exceptions)


class MCPServer:
    """The adapter's MCP server bound to one :class:`ScrapperClient`."""

    def __init__(
        self, config: MCPAdapterConfig, client: ScrapperClient, metrics: MCPMetrics | None = None
    ) -> None:
        self.config = config
        self.client = client
        self.metrics = metrics or MCPMetrics()
        self.registry = ToolRegistry(client, self.metrics)
        self._server = Server(
            name="scrapping-service",
            version=MCP_ADAPTER_VERSION,
            instructions=_ADAPTER_INSTRUCTIONS,
        )
        self._wire_handlers()

    def initialization_options(self) -> InitializationOptions:
        """The handshake options the lowlevel Server will advertise."""
        return self._server.create_initialization_options()

    # ------------------------------------------------------------- handlers
    def _wire_handlers(self) -> None:
        registry = self.registry
        server = self._server

        async def _list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
            del ctx, params  # registry is the single source of truth; no pagination
            return types.ListToolsResult(tools=[types.Tool(**spec) for spec in registry.list_tools()])

        async def _call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
            del ctx
            name = params.name
            arguments = params.arguments
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

        # mcp 2.x registers handlers via ``add_request_handler`` with a
        # ``(ctx, params)`` signature (the 1.x decorator style is gone).
        server.add_request_handler("tools/list", types.PaginatedRequestParams, _list_tools)
        server.add_request_handler("tools/call", types.CallToolRequestParams, _call_tool)

    # -------------------------------------------------------------- running
    async def run_stdio(self) -> None:
        """Run the JSON-RPC loop over stdio (the default entry point)."""
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await _run_loop(self._server, read_stream, write_stream)

    def mount_http(self, asgi_app: Any) -> None:
        """Mount the streamable-HTTP ``/mcp`` endpoint plus ops routes."""
        from mcp.server.sse import SseServerTransport

        transport = SseServerTransport(self.config.path)

        async def mcp_endpoint(scope: Any, receive: Any, send: Any) -> None:
            # Extract the token from scope (set by TokenAuthMiddleware)
            token = scope.get("mcp_token")
            if token:
                # Override the client's API key for credential pass-through
                self.client._default_headers["Authorization"] = f"Bearer {token}"
            try:
                await transport.handle_post_message(scope, receive, send)
            finally:
                # Restore default headers
                if token:
                    if self.client.api_key:
                        self.client._default_headers["Authorization"] = (
                            f"Bearer {self.client.api_key}"
                        )
                    else:
                        self.client._default_headers.pop("Authorization", None)

        asgi_app.add_route(self.config.path, mcp_endpoint)
        asgi_app.add_route("/health", health_route(self.client, self.metrics))
        asgi_app.add_route("/metrics", metrics_route(self.metrics, self.client))
        logger.info(f"MCP endpoint mounted at {self.config.path}")

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


def _parse_args() -> dict[str, Any]:
    """Parse CLI args (ticket #154)."""
    args: dict[str, Any] = {}
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            args["transport"] = sys.argv[idx + 1]
            sys.argv.pop(idx + 1)
        sys.argv.pop(idx)
    return args


def main() -> None:
    """``scrapping-service-mcp``: run the stdio MCP server.

    Options:
    --transport {stdio|streamable-http}  Override MCP_TRANSPORT env var.
    """
    args = _parse_args()
    _configure_logging()
    try:
        server = create_server()
    except MCPConfigError as exc:
        print(f"scrapping-service-mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # Dispatch transport based on config or CLI arg
    if args.get("transport") == "streamable-http" or server.config.transport == "streamable-http":
        # Run HTTP transport
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.middleware.errors import ServerErrorMiddleware

        from app.mcp.middleware import OriginHostMiddleware, TokenAuthMiddleware

        # Prepare middleware chain
        middleware_list: list[Middleware] = []
        host = server.config.host
        auth_token = server.config.auth_token

        middleware_list.append(Middleware(OriginHostMiddleware, host=host))
        middleware_list.append(Middleware(TokenAuthMiddleware, expected_token=auth_token))
        middleware_list.append(Middleware(ServerErrorMiddleware))

        app = Starlette(middleware=middleware_list)
        server.mount_http(app)
        app.router.lifespan_context = _http_lifespan(server)

        import uvicorn

        port = int(os.environ.get("MCP_HTTP_PORT", "8090"))
        if host not in ("127.0.0.1", "localhost", "::1"):
            logger.warning(
                "MCP adapter is bound to a non-loopback address (%s) without authentication. "
                "Set MCP_AUTH_TOKEN before exposing the port.",
                host,
            )
        uvicorn.run(app, host=host, port=port, log_level="info")
    else:
        # Run stdio transport
        try:
            asyncio.run(server.run_stdio())
        except (KeyboardInterrupt, SystemExit):
            pass


def main_http() -> None:
    """``scrapping-service-mcp-http``: streamable HTTP + ops routes.

    Binds ``MCP_HOST`` (default ``127.0.0.1``) port ``MCP_HTTP_PORT``
    (default ``8090``).  When ``MCP_AUTH_TOKEN`` is set, the adapter
    requires ``Authorization: Bearer <token>`` or ``x-api-key: <token>``.

    A warning is logged when binding to a non-loopback address without
    authentication.

    Origin/Host header validation is applied when the adapter binds to a
    loopback address to prevent DNS rebinding attacks.

    """
    _configure_logging()
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.errors import ServerErrorMiddleware

    from app.mcp.middleware import OriginHostMiddleware, TokenAuthMiddleware

    try:
        server = create_server()
    except MCPConfigError as exc:
        print(f"scrapping-service-mcp-http: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # Prepare middleware chain (origin/host validation runs before auth)
    middleware_list: list[Middleware] = []
    host = server.config.host
    auth_token = server.config.auth_token

    middleware_list.append(Middleware(OriginHostMiddleware, host=host))
    middleware_list.append(Middleware(TokenAuthMiddleware, expected_token=auth_token))
    middleware_list.append(Middleware(ServerErrorMiddleware))

    app = Starlette(middleware=middleware_list)
    server.mount_http(app)
    app.router.lifespan_context = _http_lifespan(server)

    import uvicorn

    port = int(os.environ.get("MCP_HTTP_PORT", "8090"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "MCP adapter is bound to a non-loopback address (%s) without authentication. "
            "Set MCP_AUTH_TOKEN before exposing the port.",
            host,
        )
    uvicorn.run(app, host=host, port=port, log_level="info")


def _http_lifespan(server: MCPServer) -> Callable[..., Any]:
    @asynccontextmanager
    async def lifespan(app: Any) -> Any:
        try:
            yield
        finally:
            await server.aclose()

    return lifespan
