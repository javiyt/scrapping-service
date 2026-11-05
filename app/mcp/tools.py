"""MCP tools for scrapping-service (ticket #149).

Each tool is a plain REST call to the service, mapped onto the service's
request schemas.  Tool inputs are ``dict[str, Any]`` (the MCP JSON-object
shape); invalid arguments raise :class:`app.mcp.client.MCPError` *before*
any network I/O so the model gets a precise correction.  Results are the
service's JSON responses, bounded by :meth:`ScrapperClient.render`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.mcp.client import MCPError, ScrapperClient, new_trace_id
from app.mcp.observability import MCPMetrics, ToolCallContext

logger = logging.getLogger("scraper-api.mcp.tools")

ToolFn = Callable[[dict[str, Any]], Awaitable[str]]

#: The service's search vertical vocabulary (mirrors ``app.schemas.search.Vertical``).
VALID_VERTICALS = ("web", "social", "videos", "news", "news_articles", "local")
VALID_SAFE_SEARCH = ("off", "moderate", "strict")
VALID_TIME_RANGES = ("day", "week", "month", "year")
VALID_SCRAPE_MODES = ("auto", "http", "browser")


# ================================================================== input parsing
def _value(args: dict[str, Any], key: str) -> Any:
    """Look up a tool argument, accepting ``snake_case`` and its aliases."""
    if key in args and args[key] is not None:
        return args[key]
    for alias in _ALIASES.get(key, ()):
        if alias in args and args[alias] is not None:
            return args[alias]
    return None


def _str(
    value: Any, *, flag: str, required: bool = False, maximum: int | None = None
) -> str | None:
    if value is None:
        if required:
            raise MCPError(f"{flag} is required")
        return None
    if not isinstance(value, str):
        raise MCPError(f"{flag} must be a string, got {type(value).__name__}")
    text = value.strip()
    if not text and required:
        raise MCPError(f"{flag} is required")
    if maximum is not None and len(text) > maximum:
        raise MCPError(f"{flag} must be at most {maximum} characters")
    return text


def _bool(value: Any, *, flag: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes"):
            return True
        if lowered in ("0", "false", "no"):
            return False
    raise MCPError(f"{flag} must be a boolean")


def _number(
    value: Any, *, flag: str, minimum: float | None = None, maximum: float | None = None
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MCPError(f"{flag} must be a number")
    num = float(value)
    if minimum is not None and num < minimum:
        raise MCPError(f"{flag} must be >= {minimum:g}")
    if maximum is not None and num > maximum:
        raise MCPError(f"{flag} must be <= {maximum:g}")
    return num


def _int(
    value: Any, *, flag: str, minimum: int | None = None, maximum: int | None = None
) -> int | None:
    num = _number(
        value,
        flag=flag,
        minimum=float(minimum) if minimum is not None else None,
        maximum=float(maximum) if maximum is not None else None,
    )
    if num is None:
        return None
    return int(num)


def _enum(value: Any, *, flag: str, choices: tuple[str, ...]) -> str | None:
    text = _str(value, flag=flag)
    if text is None:
        return None
    if text not in choices:
        raise MCPError(f"{flag} must be one of: {', '.join(repr(c) for c in choices)}")
    return text


def _json_list(value: Any, *, flag: str) -> list[Any] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise MCPError(f"{flag} must be a list")
    return value


def _string_list(value: Any, *, flag: str, max_items: int = 20) -> list[str] | None:
    items = _json_list(value, flag=flag)
    if items is None:
        return None
    if len(items) > max_items:
        raise MCPError(f"{flag} must contain at most {max_items} items")
    out = []
    for item in items:
        text = _str(item, flag=f"{flag}[]")
        if text:
            out.append(text)
    return out


# Aliases so that REST-style and MCP-style parameter names both work.
_ALIASES: dict[str, tuple[str, ...]] = {
    "verticals": ("vertical",),
    "safe_search": (),
    "time_range": (),
    "limit": ("max_results",),
    "force_refresh": ("ignore_cache",),
    "wait_selector": (),
    "lang": ("languages",),
    "url": (),
}


# ================================================================== JSON schemas
def _json_schema(
    properties: dict[str, dict[str, Any]], required: list[str] | None = None
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _props(*names: str) -> dict[str, dict[str, Any]]:
    return {name: dict(_PROPS[name]) for name in names}


_PROPS: dict[str, dict[str, Any]] = {
    "url": {
        "type": "string",
        "minLength": 1,
        "maxLength": 8192,
        "description": "Absolute http(s) URL.",
    },
    "selector": {
        "type": "string",
        "description": "CSS selector of the element(s) to extract from the fetched page.",
    },
    "mode": {
        "type": "string",
        "enum": list(VALID_SCRAPE_MODES),
        "description": "Fetch strategy: auto (default), http, or browser.",
    },
    "timeout_seconds": {
        "type": "number",
        "minimum": 5,
        "maximum": 180,
        "description": "Fetch timeout in seconds (default 90).",
    },
    "wait_selector": {
        "type": "string",
        "description": "Browser mode only: CSS selector to wait for before extraction.",
    },
    "wait_until": {
        "type": "string",
        "enum": ["load", "domcontentloaded", "networkidle", "networkidle0"],
        "description": "Browser wait strategy (default networkidle).",
    },
    "force_refresh": {
        "type": "boolean",
        "description": "Bypass the cache and fetch fresh content.",
    },
    "cache_ttl_seconds": {
        "type": "integer",
        "minimum": 60,
        "maximum": 2592000,
        "description": "Per-request cache TTL. Overrides the domain/global defaults.",
    },
    "query": {"type": "string", "minLength": 1, "maxLength": 1000, "description": "Search query."},
    "verticals": {
        "type": "array",
        "items": {"type": "string", "enum": list(VALID_VERTICALS)},
        "minItems": 1,
        "uniqueItems": True,
        "description": 'Verticals to search (default ["web"]).',
    },
    "language": {"type": "string", "description": "BCP-47 language tag, e.g. ``en-US``."},
    "safe_search": {
        "type": "string",
        "enum": list(VALID_SAFE_SEARCH),
        "description": "Safe-search level: off, moderate (default) or strict.",
    },
    "time_range": {
        "type": "string",
        "enum": list(VALID_TIME_RANGES),
        "description": "Restrict results to this time range.",
    },
    "max_results": {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "description": "Maximum number of results (alias of ``limit``).",
    },
    "limit": {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "description": "Maximum number of results.",
    },
    "sources": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Preferred underlying sources (provider-defined).",
    },
    "provider": {"type": "string", "description": "Provider name, or ``auto`` (default)."},
    "video_id": {
        "type": "string",
        "description": "YouTube video ID (11 characters) or a full YouTube URL.",
    },
    "languages": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Preferred caption languages in priority order (alias of ``lang``).",
    },
    "session_id": {
        "type": "string",
        "description": "Browser session id from browser_create_session.",
    },
    "tab_id": {"type": "string", "description": "Tab id from the session/tab responses."},
    "ref": {
        "type": "string",
        "description": "Accessibility ref from the page snapshot, e.g. ``@e2``.",
    },
    "name": {"type": "string", "description": "Optional session name."},
    "ttl_seconds": {
        "type": "integer",
        "minimum": 60,
        "maximum": 86400,
        "description": "Session TTL.",
    },
    "new_tab": {
        "type": "boolean",
        "description": "Open the URL in a new tab (the service creates the tab).",
    },
    "timeout_ms": {
        "type": "integer",
        "minimum": 1,
        "maximum": 120000,
        "description": "Browser operation timeout in milliseconds.",
    },
    "text": {"type": "string", "description": "Text to type."},
    "clear": {"type": "boolean", "description": "Clear the field before typing."},
    "key": {
        "type": "string",
        "enum": [
            "Enter",
            "Tab",
            "Escape",
            "Backspace",
            "Delete",
            "ArrowLeft",
            "ArrowRight",
            "ArrowUp",
            "ArrowDown",
        ],
        "description": "Key to press after typing.",
    },
    "expression": {
        "type": "string",
        "description": "JavaScript expression to evaluate in the page.",
    },
    "domain": {"type": "string", "description": "Domain whose cache entries should be purged."},
    "url_prefix": {"type": "string", "description": "URL prefix filter."},
    "max_links": {
        "type": "integer",
        "minimum": 1,
        "maximum": 1000,
        "description": "Maximum links to extract.",
    },
    "include_external": {
        "type": "boolean",
        "description": "Include links outside the page origin.",
    },
    "include_content": {
        "type": "boolean",
        "description": "Include the scraped content of each linked URL.",
    },
    "depth": {
        "type": "integer",
        "minimum": 0,
        "maximum": 5,
        "description": "Maximum link-follow depth.",
    },
    "delete_expired_after_seconds": {
        "type": "integer",
        "minimum": 0,
        "description": "Delete entries older than this.",
    },
    "max_entries": {
        "type": "integer",
        "minimum": 0,
        "description": "Shrink the cache to at most this many entries.",
    },
    "max_size_mb": {
        "type": "integer",
        "minimum": 0,
        "description": "Shrink the cache to at most this many megabytes.",
    },
    "vacuum": {"type": "boolean", "description": "Also run VACUUM."},
}


# ================================================================== registry
class ToolSpec:
    """One MCP tool: name, description, JSON schema and handler."""

    def __init__(
        self, name: str, description: str, input_schema: dict[str, Any], handler: ToolFn
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler

    def to_mcp_tool(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class ToolRegistry:
    """The adapter's tool table plus the call dispatch.

    Credential pass-through (ticket #154): when created with a
    ``get_credential`` callback, tool handlers pass the returned token
    to ``ScrapperClient`` so the caller's upstream credential is forwarded.

    """

    def __init__(
        self,
        client: ScrapperClient,
        metrics: MCPMetrics,
        *,
        get_credential: Callable[[], str | None] | None = None,
    ) -> None:
        self._client = client
        self._metrics = metrics
        self._get_credential = get_credential
        self._tools: dict[str, ToolSpec] = {}
        self._register_all()

    # ------------------------------------------------------------- registration
    def _register_all(self) -> None:
        self.register(
            ToolSpec(
                "adapter_info",
                "Report the MCP adapter's configuration (service URL, key presence, version) "
                "and the service's health status. Use first to verify the wiring.",
                _json_schema({}),
                self.info,
            )
        )
        self.register(
            ToolSpec(
                "search",
                "Search the web (or social/videos/news verticals) via the service's search "
                "providers. Returns ranked results with title, url, snippet, vertical and rank. "
                "No search is run for an empty query.",
                _json_schema(
                    _props(
                        "query",
                        "verticals",
                        "language",
                        "safe_search",
                        "time_range",
                        "max_results",
                        "sources",
                        "provider",
                        "force_refresh",
                    ),
                    required=["query"],
                ),
                self.search,
            )
        )
        self.register(
            ToolSpec(
                "search_capabilities",
                "List the service's search providers and their capabilities (verticals, "
                "filters, time ranges, sources). Read-only discovery; no search is performed.",
                _json_schema({}),
                self.search_capabilities,
            )
        )
        self.register(
            ToolSpec(
                "fetch_url",
                "Fetch one URL and return its content (markdown or JSON) plus metadata. "
                "Supports cached http mode and headless-browser mode, optional CSS-scoped "
                "extraction, and link extraction with follow depth.",
                _json_schema(
                    _props(
                        "url",
                        "mode",
                        "timeout_seconds",
                        "wait_selector",
                        "wait_until",
                        "force_refresh",
                        "cache_ttl_seconds",
                        "selector",
                        "max_links",
                        "include_external",
                        "include_content",
                        "depth",
                    ),
                    required=["url"],
                ),
                self.fetch_url,
            )
        )
        self.register(
            ToolSpec(
                "fetch_url_batch",
                "Fetch up to 50 URLs in parallel (service caps concurrency). Each item: "
                "{url, mode?, timeout_seconds?, force_refresh?}. Results are per-URL; "
                "one failure does not fail the batch.",
                _json_schema(
                    {
                        "urls": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 50,
                            "items": _json_schema(
                                _props("url", "mode", "timeout_seconds", "force_refresh"),
                                required=["url"],
                            ),
                            "description": (
                                "Up to 50 URLs to fetch. Each item: "
                                "{url, mode?, timeout_seconds?, force_refresh?}."
                            ),
                        },
                        "max_concurrency": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    required=["urls"],
                ),
                self.fetch_url_batch,
            )
        )
        self.register(
            ToolSpec(
                "transcript",
                "Get a YouTube video transcript (timestamped segments + full text). The video "
                "is identified by a bare 11-character ID or any YouTube URL. Caption tracks are "
                "chosen by preference: ``languages`` is the priority order (alias of ``lang``).",
                _json_schema(
                    {
                        "video_id": dict(_PROPS["video_id"]),
                        "url": dict(_PROPS["url"]),
                        "languages": dict(_PROPS["languages"]),
                        "lang": {
                            "type": "string",
                            "description": (
                                "Comma-separated priority list, e.g. ``es,en`` "
                                "(alias of ``languages``)."
                            ),
                        },
                        "provider": dict(_PROPS["provider"]),
                        "force_refresh": dict(_PROPS["force_refresh"]),
                    },
                ),
                self.transcript,
            )
        )
        self.register(
            ToolSpec(
                "browser_capabilities",
                "Report whether interactive browser sessions are enabled on the service and "
                "their limits (max sessions/tabs, TTL, evaluate support).",
                _json_schema({}),
                self.browser_capabilities,
            )
        )
        self.register(
            ToolSpec(
                "browser_create_session",
                "Create an interactive browser session. Returns the ``session_id`` and a "
                "``tabs`` list. Pass ``url`` to also open the first tab (its ``tab_id`` is "
                "returned). Keep both ids: the other browser tools require them. Close the "
                "session with browser_close_session when done.",
                _json_schema(
                    _props("url", "name", "ttl_seconds"),
                ),
                self.browser_create_session,
            )
        )
        self.register(
            ToolSpec(
                "browser_navigate",
                "Navigate a tab to a URL (or open a new tab with ``new_tab: true``) and "
                "return the new page state: url, title, and an accessibility snapshot with "
                "element refs (``@eN``) you can pass to browser_click / browser_type.",
                _json_schema(
                    _props(
                        "session_id", "tab_id", "url", "new_tab", "wait_selector", "timeout_seconds"
                    ),
                    required=["session_id", "url"],
                ),
                self.browser_navigate,
            )
        )
        self.register(
            ToolSpec(
                "browser_click",
                "Click an element in a tab, identified by its accessibility ``ref`` (e.g. "
                "``@e2`` from a snapshot) or a CSS ``selector``. One of the two is required.",
                _json_schema(
                    _props("session_id", "tab_id", "ref", "selector"),
                    required=["session_id", "tab_id"],
                ),
                self.browser_click,
            )
        )
        self.register(
            ToolSpec(
                "browser_type",
                "Type text into a form field (by ``ref`` or CSS ``selector``, or the active "
                "element when neither is given). ``clear`` empties the field first; ``key`` "
                "presses a key afterwards (e.g. submit with ``Enter``).",
                _json_schema(
                    _props("session_id", "tab_id", "text", "ref", "selector", "clear", "key"),
                    required=["session_id", "tab_id", "text"],
                ),
                self.browser_type,
            )
        )
        self.register(
            ToolSpec(
                "browser_evaluate",
                "Evaluate a JavaScript *expression* in a tab and return its value (requires "
                "the service to allow evaluate mode). The expression must be a single "
                "expression returning a JSON-serializable value, e.g. "
                "``document.title`` or ``JSON.stringify([...].map(...))``.",
                _json_schema(
                    _props("session_id", "tab_id", "expression", "timeout_ms"),
                    required=["session_id", "tab_id", "expression"],
                ),
                self.browser_evaluate,
            )
        )
        self.register(
            ToolSpec(
                "browser_close_tab",
                "Close one tab of a browser session.",
                _json_schema(_props("session_id", "tab_id"), required=["session_id", "tab_id"]),
                self.browser_close_tab,
            )
        )
        self.register(
            ToolSpec(
                "browser_close_session",
                "Close a browser session and all of its tabs. Always call this when the "
                "user is done browsing to free the session slot.",
                _json_schema(_props("session_id"), required=["session_id"]),
                self.browser_close_session,
            )
        )
        self.register(
            ToolSpec(
                "cache_purge",
                "Delete all cached scrape entries, optionally scoped to one ``domain``. "
                "Destructive: use only when the user asks to clear the cache.",
                _json_schema(_props("domain")),
                self.cache_purge,
            )
        )
        self.register(
            ToolSpec(
                "cache_delete",
                "Remove one exact URL from the scrape cache.",
                _json_schema(_props("url"), required=["url"]),
                self.cache_delete,
            )
        )
        self.register(
            ToolSpec(
                "cache_cleanup",
                "Run a cache maintenance pass (delete expired entries, enforce size/entry "
                "caps, optional VACUUM) and report exactly what was deleted.",
                _json_schema(
                    _props("delete_expired_after_seconds", "max_entries", "max_size_mb", "vacuum")
                ),
                self.cache_cleanup,
            )
        )
        self.register(
            ToolSpec(
                "cache_vacuum",
                "Run SQLite VACUUM to reclaim disk space (I/O intensive; maintenance windows "
                "on low-power hosts preferred).",
                _json_schema({}),
                self.cache_vacuum,
            )
        )
        self.register(
            ToolSpec(
                "cache_stats",
                "Report cache size, entry counts and expiry breakdowns for the scrape, "
                "search and transcript result caches.",
                _json_schema({}),
                self.cache_stats,
            )
        )

    # ------------------------------------------------------------- dispatch
    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def list_tools(self) -> list[dict[str, Any]]:
        return [spec.to_mcp_tool() for spec in self._tools.values()]

    async def call(self, name: str, arguments: dict[str, Any] | None) -> str:
        """Dispatch one tool call. Returns bounded JSON text; never raises.

        Credential pass-through (ticket #154): when ``_get_credential`` is
        set (HTTP mode), it's called before each tool and passed as
        ``api_key_override`` to ``ScrapperClient`` so the caller's token
        is forwarded upstream.  When ``_get_credential`` is ``None`` (stdio
        mode), the client's default ``api_key`` is used.

        """
        spec = self._tools.get(name)
        if spec is None:
            return _error_json(f"Unknown tool '{name}'. Available tools: {', '.join(self.names())}")
        if not isinstance(arguments, dict):
            arguments = {}
        request_id = new_trace_id()
        context = ToolCallContext(name, request_id)
        started = time.monotonic()
        try:
            text = await self._call_with_credential(spec.handler, arguments)
            result = "ok"
        except MCPError as exc:
            text = self._client.render(exc.to_dict())
            result = exc.error_type
        except Exception as exc:  # noqa: BLE001 — never crash the MCP session
            logger.exception("tool %s failed unexpectedly", name)
            from app.mcp.config import _redact_secret  # noqa: PLC0415

            text = _error_json(
                _redact_secret(
                    f"Internal adapter error: {type(exc).__name__}: {exc}", self._client.api_key
                ),
                error_type="internal_error",
            )
            result = "internal_error"
        duration = time.monotonic() - started
        self._metrics.record_tool_call(name, result, duration)
        context.log_end(result)
        return text

    async def _call_with_credential(self, handler: ToolFn, arguments: dict[str, Any]) -> str:
        """Call a handler with credential pass-through when configured."""
        credential = self._get_credential() if self._get_credential else None
        return await handler(arguments, api_key_override=credential)

    # ------------------------------------------------------------- tool bodies
    async def info(self, args: dict[str, Any], *, api_key_override: str | None = None) -> str:
        health = await self._client.get("/health", api_key_override=api_key_override)
        return self._client.render(
            {
                "adapter": {
                    "name": "scrapping-service-mcp",
                    "version": "1.0.0",
                    "config": self._client.config_view(),
                },
                "service": _jsonable(health),
            }
        )

    async def search(self, args: dict[str, Any], *, api_key_override: str | None = None) -> str:
        query = _str(args.get("query"), flag="query", required=True)
        payload: dict[str, Any] = {"query": query}
        verticals = _string_list(args.get("verticals"), flag="verticals", max_items=6)
        if verticals:
            for v in verticals:
                if v not in VALID_VERTICALS:
                    raise MCPError(f"verticals item must be one of: {', '.join(VALID_VERTICALS)}")
            payload["verticals"] = verticals
        if (v := _str(args.get("language"), flag="language")) is not None:
            payload["language"] = v
        if (
            v := _enum(args.get("safe_search"), flag="safe_search", choices=VALID_SAFE_SEARCH)
        ) is not None:
            payload["safe_search"] = v
        if (
            v := _enum(args.get("time_range"), flag="time_range", choices=VALID_TIME_RANGES)
        ) is not None:
            payload["time_range"] = v
        if (
            v := _int(
                args.get("max_results") or args.get("limit"),
                flag="max_results",
                minimum=1,
                maximum=100,
            )
        ) is not None:
            payload["limit"] = v
        if (v := _string_list(args.get("sources"), flag="sources", max_items=50)) is not None:
            payload["sources"] = v
        if (v := _str(args.get("provider"), flag="provider")) is not None:
            payload["provider"] = v
        if (
            _bool(
                args.get("force_refresh") if "force_refresh" in args else args.get("ignore_cache"),
                flag="force_refresh",
            )
            is True
        ):
            payload["force_refresh"] = True
        response = await self._client.post(
            "/v1/search", json_body=payload, api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def search_capabilities(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        response = await self._client.get(
            "/v1/search/capabilities", api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def fetch_url(self, args: dict[str, Any], *, api_key_override: str | None = None) -> str:
        payload: dict[str, Any] = {"url": _str(args.get("url"), flag="url", required=True)}
        for key, flag in (
            ("mode", "mode"),
            ("wait_selector", "wait_selector"),
            ("wait_until", "wait_until"),
            ("selector", "selector"),
        ):
            value = _str(args.get(key), flag=flag)
            if value is not None:
                payload[key] = value
        if (v := _enum(args.get("mode"), flag="mode", choices=VALID_SCRAPE_MODES)) is not None:
            payload["mode"] = v
        if (
            v := _number(
                args.get("timeout_seconds"), flag="timeout_seconds", minimum=5, maximum=180
            )
        ) is not None:
            payload["timeout_seconds"] = int(v)
        if (
            _bool(
                args.get("force_refresh") if "force_refresh" in args else args.get("ignore_cache"),
                flag="force_refresh",
            )
            is True
        ):
            payload["force_refresh"] = True
        if (
            v := _int(
                args.get("cache_ttl_seconds"), flag="cache_ttl_seconds", minimum=60, maximum=2592000
            )
        ) is not None:
            payload["cache_ttl_seconds"] = v
        # ``selector`` scoping rides on the service's ``extract`` config.
        if (v := _str(args.get("selector"), flag="selector")) is not None:
            payload["extract"] = {"enabled": True, "fields": {"content": {"selector": v}}}
        response = await self._client.post(
            "/v1/scrape", json_body=payload, api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def fetch_url_batch(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        urls = _json_list(args.get("urls"), flag="urls") or []
        if not 1 <= len(urls) <= 50:
            raise MCPError("urls must contain between 1 and 50 items")
        items: list[dict[str, Any]] = []
        for raw in urls:
            if not isinstance(raw, dict):
                raise MCPError("each urls item must be an object with at least url")
            item: dict[str, Any] = {"url": _str(raw.get("url"), flag="urls[].url", required=True)}
            if (
                v := _enum(raw.get("mode"), flag="urls[].mode", choices=VALID_SCRAPE_MODES)
            ) is not None:
                item["mode"] = v
            if (
                v := _number(
                    raw.get("timeout_seconds"),
                    flag="urls[].timeout_seconds",
                    minimum=5,
                    maximum=180,
                )
            ) is not None:
                item["timeout_seconds"] = int(v)
            if _bool(raw.get("force_refresh"), flag="urls[].force_refresh") is True:
                item["force_refresh"] = True
            items.append(item)
        payload: dict[str, Any] = {"items": items}
        if (
            v := _int(args.get("max_concurrency"), flag="max_concurrency", minimum=1, maximum=10)
        ) is not None:
            payload["max_concurrency"] = v
        response = await self._client.post(
            "/v1/scrape/batch", json_body=payload, api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def transcript(self, args: dict[str, Any], *, api_key_override: str | None = None) -> str:
        video_id = _str(args.get("video_id"), flag="video_id")
        url = _str(args.get("url"), flag="url")
        if video_id is None and url is None:
            raise MCPError("transcript needs a video_id (11-character ID or a YouTube URL)")
        # ``video_id`` accepts either a bare ID or a full URL; the service
        # accepts a bare ID in ``url`` but a full URL does *not* round-trip
        # through ``video_id``, so resolve the parameter deliberately.
        params: dict[str, Any] = {}
        if url is None and video_id is not None and len(video_id) == 11:
            params["video_id"] = video_id
        elif url is None and video_id is not None:
            params["url"] = video_id
        else:
            params["url"] = url
        languages = _string_list(args.get("languages"), flag="languages")
        if languages is None and (v := _str(args.get("lang"), flag="lang")) is not None:
            languages = [part.strip() for part in v.split(",") if part.strip()]
        if languages:
            params["lang"] = ",".join(languages)
        if (v := _str(args.get("provider"), flag="provider")) is not None:
            params["provider"] = v
        if _bool(args.get("force_refresh"), flag="force_refresh") is True:
            params["force_refresh"] = "true"
        response = await self._client.get(
            "/v1/transcript", params=params, api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def browser_capabilities(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        response = await self._client.get(
            "/v1/browser/capabilities", api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def browser_create_session(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        params: dict[str, Any] = {}
        if (v := _str(args.get("name"), flag="name")) is not None:
            params["name"] = v
        if (
            v := _int(args.get("ttl_seconds"), flag="ttl_seconds", minimum=60, maximum=86400)
        ) is not None:
            params["ttl_seconds"] = v
        if (v := _str(args.get("url"), flag="url")) is not None:
            params["url"] = v
        response = await self._client.post(
            "/v1/browser/sessions", params=params or None, api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def browser_navigate(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        session_id = _str(args.get("session_id"), flag="session_id", required=True)
        url = _str(args.get("url"), flag="url", required=True)
        payload: dict[str, Any] = {"url": url}
        if (v := _str(args.get("wait_selector"), flag="wait_selector")) is not None:
            payload["wait_selector"] = v
        if (
            v := _number(
                args.get("timeout_seconds"), flag="timeout_seconds", minimum=1, maximum=300
            )
        ) is not None:
            payload["timeout_seconds"] = v

        tab_id = _str(args.get("tab_id"), flag="tab_id")
        if _bool(args.get("new_tab"), flag="new_tab") is True or (tab_id is None):
            # Open a new tab: the service creates it and returns tab + snapshot.
            response = await self._client.post(
                f"/v1/browser/sessions/{session_id}/tabs",
                json_body=payload,
                api_key_override=api_key_override,
            )
            return self._client.render(response.json())
        response = await self._client.post(
            f"/v1/browser/sessions/{session_id}/tabs/{tab_id}/navigate",
            json_body=payload,
            api_key_override=api_key_override,
        )
        body = response.json()
        snapshot = await self._client.get(
            f"/v1/browser/sessions/{session_id}/tabs/{tab_id}/snapshot",
            api_key_override=api_key_override,
        )
        body["snapshot"] = snapshot.json()
        return self._client.render(body)

    async def browser_click(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        session_id = _str(args.get("session_id"), flag="session_id", required=True)
        tab_id = _str(args.get("tab_id"), flag="tab_id", required=True)
        payload: dict[str, Any] = {}
        if (v := _str(args.get("ref"), flag="ref")) is not None:
            payload["ref"] = v
        if (v := _str(args.get("selector"), flag="selector")) is not None:
            payload["selector"] = v
        if not payload:
            raise MCPError("browser_click needs either a ref or a selector")
        response = await self._client.post(
            f"/v1/browser/sessions/{session_id}/tabs/{tab_id}/click",
            json_body=payload,
            api_key_override=api_key_override,
        )
        return self._client.render(response.json())

    async def browser_type(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        session_id = _str(args.get("session_id"), flag="session_id", required=True)
        tab_id = _str(args.get("tab_id"), flag="tab_id", required=True)
        payload: dict[str, Any] = {"text": _str(args.get("text"), flag="text", required=True)}
        if (v := _str(args.get("ref"), flag="ref")) is not None:
            payload["ref"] = v
        if (v := _str(args.get("selector"), flag="selector")) is not None:
            payload["selector"] = v
        if (v := _bool(args.get("clear"), flag="clear")) is not None:
            payload["clear"] = v
        if (v := _str(args.get("key"), flag="key")) is not None:
            payload["key"] = v
        response = await self._client.post(
            f"/v1/browser/sessions/{session_id}/tabs/{tab_id}/type",
            json_body=payload,
            api_key_override=api_key_override,
        )
        return self._client.render(response.json())

    async def browser_evaluate(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        session_id = _str(args.get("session_id"), flag="session_id", required=True)
        tab_id = _str(args.get("tab_id"), flag="tab_id", required=True)
        payload: dict[str, Any] = {
            "expression": _str(args.get("expression"), flag="expression", required=True)
        }
        if (
            v := _int(args.get("timeout_ms"), flag="timeout_ms", minimum=1, maximum=120000)
        ) is not None:
            payload["timeout_ms"] = v
        response = await self._client.post(
            f"/v1/browser/sessions/{session_id}/tabs/{tab_id}/evaluate",
            json_body=payload,
            api_key_override=api_key_override,
        )
        return self._client.render(response.json())

    async def browser_close_tab(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        session_id = _str(args.get("session_id"), flag="session_id", required=True)
        tab_id = _str(args.get("tab_id"), flag="tab_id", required=True)
        response = await self._client.delete(
            f"/v1/browser/sessions/{session_id}/tabs/{tab_id}", api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def browser_close_session(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        session_id = _str(args.get("session_id"), flag="session_id", required=True)
        response = await self._client.delete(
            f"/v1/browser/sessions/{session_id}", api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def cache_purge(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        domain = _str(args.get("domain"), flag="domain")
        params = {"domain": domain} if domain else None
        response = await self._client.post(
            "/v1/cache/purge", params=params, api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def cache_delete(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        url = _str(args.get("url"), flag="url", required=True)
        response = await self._client.delete(
            "/v1/cache", params={"url": url}, api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def cache_cleanup(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        payload: dict[str, Any] = {}
        for key in ("delete_expired_after_seconds", "max_entries", "max_size_mb"):
            if (v := _int(args.get(key), flag=key, minimum=0)) is not None:
                payload[key] = v
        if (v := _bool(args.get("vacuum"), flag="vacuum")) is not None:
            payload["vacuum"] = v
        response = await self._client.post(
            "/v1/cache/cleanup", json_body=payload or None, api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def cache_vacuum(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        response = await self._client.post(
            "/v1/cache/vacuum", json_body=None, api_key_override=api_key_override
        )
        return self._client.render(response.json())

    async def cache_stats(
        self, args: dict[str, Any], *, api_key_override: str | None = None
    ) -> str:
        response = await self._client.get("/v1/cache/stats", api_key_override=api_key_override)
        return self._client.render(response.json())


def _error_json(message: str, *, error_type: str = "adapter_error") -> str:
    return json.dumps(
        {"error": {"type": error_type, "message": message, "details": {}}}, ensure_ascii=False
    )


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of a response body to JSON-safe values.

    Accepts an ``httpx.Response`` (its parsed JSON), or any JSON-able value.
    """
    if isinstance(value, httpx.Response):
        try:
            return value.json()
        except ValueError:
            return {"text": value.text[:2000]}
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:  # noqa: BLE001
        return str(value)
