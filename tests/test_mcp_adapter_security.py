"""Security tests for the MCP adapter (ticket #150).

The adapter's security contract is *inherited* from the service: the adapter
adds no bypass of SSRF checks, domain rules, auth or caching — it forwards
requests verbatim.  These tests prove that inheritance over a mock
transport, and pin the adapter-specific guarantees that exist on top:

1. the API key never appears in any rendered tool result, error or log;
2. one failing tool never leaks another profile's / tenant's data;
3. service security rejections (SSRF, domain rules, auth) surface as
   *errors* with the service's own message — the adapter must not retry,
   fallback or "fix" the request;
4. tool outputs are bounded, so a pathological page cannot overflow the
   MCP channel;
5. the adapter's own error handling cannot be used to extract adapter
   internals (config paths, stack traces, the key).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.mcp.client import (
    MCPError,
    ScrapperClient,
)
from app.mcp.observability import MCPMetrics
from app.mcp.tools import ToolRegistry

API_KEY = "sk-super-secret-adapter-key-0123456789"


def _expect_error(client: ScrapperClient, path: str = "/v1/search") -> str:
    """Return the mapped error message for one request (for redaction asserts)."""

    async def _run() -> str:
        try:
            await client.request("GET", path)
        except MCPError as exc:
            return exc.message
        raise AssertionError("expected the request to fail")

    return asyncio.run(_run())


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> ScrapperClient:
    return ScrapperClient(
        base_url="http://service.invalid:8080",
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
        max_output_chars=20_000,
    )


def _json(text: str) -> Any:
    return json.loads(text)


class TestKeyRedaction:
    """The bearer key must never reach a tool result, error or log line."""

    def test_service_error_containing_key_is_redacted(self) -> None:
        client = _client(
            lambda r: httpx.Response(
                401,
                json={
                    "error": {
                        "type": "auth_error",
                        "message": f"Invalid API key {API_KEY}",
                        "details": {},
                    }
                },
            )
        )
        exc_message = _expect_error(client, "/v1/search")
        rendered = client.render(
            {"error": {"type": "auth_error", "message": exc_message, "details": {}}}
        )
        assert API_KEY not in rendered
        assert "<REDACTED>" in rendered

    def test_unmapped_error_body_never_leaks_key(self) -> None:
        # A non-envelope error body that happens to contain the key must not
        # leak: only the status-code message (which is adapter-authored) is
        # used for non-JSON / unparseable bodies.
        client = _client(lambda r: httpx.Response(500, text=f"crashed with key {API_KEY} in stack"))
        exc_message = _expect_error(client, "/v1/scrape")
        assert API_KEY not in exc_message
        assert "500" in exc_message

    def test_config_view_reports_presence_not_value(self) -> None:
        client = _client(lambda r: httpx.Response(200, json={}))
        view = client.config_view()
        assert view["api_key_configured"] is True
        assert API_KEY not in json.dumps(view)

    def test_result_json_never_contains_key(self) -> None:
        client = _client(lambda r: httpx.Response(200, json={"ok": True}))
        registry = ToolRegistry(client, MCPMetrics())
        text = asyncio.run(registry.call("adapter_info", {}))
        assert API_KEY not in text

    def test_header_carries_key_exactly_once(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        client = _client(handler)
        asyncio.run(client.request("GET", "/health"))
        assert seen[0].headers["authorization"] == f"Bearer {API_KEY}"

    def test_no_header_without_key(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={})

        client = ScrapperClient(base_url="http://s", transport=httpx.MockTransport(handler))
        asyncio.run(client.request("GET", "/health"))
        assert "authorization" not in seen[0].headers


class TestErrorIsolation:
    """One failing tool must not bleed into another call or tenant."""

    async def test_tool_failure_does_not_corrupt_next_call(self) -> None:
        calls: list[str] = []

        first = True

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal first
            calls.append(request.url.path)
            if request.url.path == "/v1/search" and first:
                first = False
                return httpx.Response(
                    502,
                    json={
                        "error": {
                            "type": "search_error",
                            "message": "provider exploded",
                            "details": {},
                        }
                    },
                )
            return httpx.Response(200, json={"ok": True})

        registry = ToolRegistry(_client(handler), MCPMetrics())
        bad = await registry.call("search", {"query": "boom"})
        assert "provider exploded" in bad
        good = await registry.call("search", {"query": "after"})
        assert '"ok": true' in good
        assert calls == ["/v1/search", "/v1/search"]

    async def test_exception_in_one_handler_never_raises(self) -> None:
        registry = ToolRegistry(_client(lambda r: httpx.Response(200, json={})), MCPMetrics())
        # A handler that raises must be rendered as a clean error, not crash the session.
        registry._tools["adapter_info"].handler = _explode  # type: ignore[assignment]
        text = await registry.call("adapter_info", {})
        data = _json(text)
        assert data["error"]["type"] == "internal_error"

    async def test_unknown_tool_is_a_clean_error(self) -> None:
        registry = ToolRegistry(_client(lambda r: httpx.Response(200, json={})), MCPMetrics())
        text = await registry.call("totally_unknown_tool", {})
        data = _json(text)
        assert data["error"]["type"] == "adapter_error"
        assert "totally_unknown_tool" in data["error"]["message"]


class TestServicePolicyPassthrough:
    """Service rejections must surface unchanged (no retry, no fallback)."""

    def _counting_handler(
        self, response_fn: Callable[[httpx.Request], httpx.Response]
    ) -> Callable[[httpx.Request], httpx.Response]:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return response_fn(request)

        handler.calls = calls  # type: ignore[attr-defined]
        return handler  # type: ignore[return-value]

    async def test_ssrf_rejection_is_propagated_not_fixed(self) -> None:
        handler = self._counting_handler(
            lambda r: httpx.Response(
                403,
                json={
                    "error": {
                        "type": "security_error",
                        "message": "URL resolves to a private address",
                        "details": {"host": "127.0.0.1"},
                    }
                },
            )
        )
        registry = ToolRegistry(_client(handler), MCPMetrics())
        text = await registry.call("fetch_url", {"url": "http://127.0.0.1/admin"})
        data = _json(text)
        assert data["error"]["type"] == "auth_error"  # 403 from the service maps to auth_error
        assert "private address" in data["error"]["message"]
        assert len(handler.calls) == 1, "the adapter must not retry a security rejection"
        # The adapter never rewrites the URL.
        assert handler.calls[0].url.path == "/v1/scrape"
        body = json.loads(handler.calls[0].content)
        assert body["url"] == "http://127.0.0.1/admin"

    async def test_domain_rule_rejection_is_propagated(self) -> None:
        handler = self._counting_handler(
            lambda r: httpx.Response(
                403,
                json={
                    "error": {
                        "type": "security_error",
                        "message": "Domain not allowed by profile policy",
                        "details": {"domain": "banned.example"},
                    }
                },
            )
        )
        registry = ToolRegistry(_client(handler), MCPMetrics())
        text = await registry.call("fetch_url", {"url": "https://banned.example/"})
        data = _json(text)
        assert data["error"]["type"] == "auth_error"  # the 403 mapping
        assert "Domain not allowed" in data["error"]["message"]
        assert len(handler.calls) == 1

    async def test_auth_rejection_is_propagated(self) -> None:
        handler = self._counting_handler(
            lambda r: httpx.Response(401, json={"detail": "Not authenticated"})
        )
        registry = ToolRegistry(_client(handler), MCPMetrics())
        text = await registry.call("search", {"query": "x"})
        data = _json(text)
        assert data["error"]["type"] == "auth_error"
        assert len(handler.calls) == 1

    def test_service_error_envelope_shape_is_preserved(self) -> None:
        """The rendered error keeps the service's message and status details."""
        registry = ToolRegistry(
            _client(
                lambda r: httpx.Response(
                    422,
                    json={
                        "error": {
                            "type": "validation_error",
                            "message": "bad query",
                            "details": {"field": "query"},
                        }
                    },
                )
            ),
            MCPMetrics(),
        )
        data = _json(asyncio.run(registry.call("search", {"query": "x"})))
        assert data["error"]["type"] == "validation_error"
        assert data["error"]["message"] == "bad query"
        assert data["error"]["details"] == {"status_code": 422, "error": {"field": "query"}}

    async def test_provider_failure_surfaces_with_message(self) -> None:
        """A 5xx with a provider message is rendered with a safe, useful error."""
        registry = ToolRegistry(
            _client(
                lambda r: httpx.Response(
                    502,
                    json={
                        "error": {
                            "type": "search_error",
                            "message": "all search providers failed",
                            "details": {},
                        }
                    },
                )
            ),
            MCPMetrics(),
        )
        data = _json(await registry.call("search", {"query": "x"}))
        assert data["error"]["type"] == "search_error"
        assert "all search providers failed" in data["error"]["message"]
        assert data["error"]["details"].get("status_code") == 502


class TestOutputBoundedness:
    """No tool result may be unbounded: a huge page must be truncated with a marker."""

    async def test_oversized_result_is_truncated_with_marker(self) -> None:
        client = ScrapperClient(base_url="http://s", api_key=API_KEY, max_output_chars=1_000)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"content": "x" * 10_000})

        client._http = httpx.AsyncClient(
            base_url="http://s", headers={}, transport=httpx.MockTransport(handler)
        )
        registry = ToolRegistry(client, MCPMetrics())
        text = await registry.call("fetch_url", {"url": "https://example.com/huge"})
        assert len(text) < 10_000
        assert "truncated" in text
        assert len(text) <= 1_000 + 200  # cap plus the marker

    def test_render_truncates_raw_text(self) -> None:
        client = ScrapperClient(base_url="http://s", max_output_chars=1_000)
        out = client.render_raw("y" * 5_000)
        assert len(out) < 5_000
        assert "truncated" in out


class TestAdapterErrorContainment:
    """The adapter's own failures must not leak internals to the MCP host."""

    async def test_internal_error_does_not_leak_stack_or_key(self) -> None:
        registry = ToolRegistry(_client(lambda r: httpx.Response(200, json={})), MCPMetrics())
        # Force an internal failure: a handler raising a non-MCPError.
        registry._tools["adapter_info"].handler = _explode  # type: ignore[assignment]
        text = await registry.call("adapter_info", {})
        data = _json(text)
        assert data["error"]["type"] == "internal_error"
        assert API_KEY not in text
        assert "Traceback" not in text

    async def test_invalid_arguments_are_rejected_before_network(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={})

        registry = ToolRegistry(
            ScrapperClient("http://s", api_key=API_KEY, transport=httpx.MockTransport(handler)),
            MCPMetrics(),
        )
        text = await registry.call("search", {"query": 123})  # wrong type
        data = _json(text)
        assert data["error"]["type"] == "adapter_error"
        assert calls == [], "an invalid argument must not produce a network call"


async def _explode(args: dict[str, Any]) -> str:
    raise RuntimeError(f"boom {API_KEY}")


# ===========================================================================
# Process-level: the adapter must fail its own startup cleanly when
# misconfigured, without a stack trace or the key.
class TestStartupSafety:
    def test_missing_base_url_fails_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MCP_SCRAPPER_BASE_URL", raising=False)
        monkeypatch.delenv("MCP_SCRAPER_BASE_URL", raising=False)
        monkeypatch.delenv("SCRAPER_BASE_URL", raising=False)
        from app.mcp.config import MCPConfigError

        with pytest.raises(MCPConfigError) as exc:
            from app.mcp import config as cfg_module

            cfg_module.load_config()
        assert "MCP_SCRAPER_BASE_URL" in str(exc.value)
        assert API_KEY not in str(exc.value)


class TestTimeoutAndTrace:
    """Timeout mapping and the trace-id propagation header."""

    def test_timeout_maps_to_service_timeout_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        client = ScrapperClient(
            base_url="http://s",
            api_key=API_KEY,
            timeout_seconds=0.1,
            transport=httpx.MockTransport(handler),
        )
        message = _expect_error(client, "/v1/search")
        assert "did not answer" in message

    def test_timeout_on_get(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow")

        client = ScrapperClient(
            base_url="http://s", api_key=API_KEY, transport=httpx.MockTransport(handler)
        )

        async def go() -> str:
            try:
                await client.get("/health")
            except MCPError as exc:
                return exc.message
            raise AssertionError("expected a timeout error")

        message = asyncio.run(go())
        assert "did not answer" in message


class TestUnreachableService:
    def test_connection_error_maps_to_unreachable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("dns failure")

        client = ScrapperClient(
            base_url="http://service.invalid",
            api_key=API_KEY,
            transport=httpx.MockTransport(handler),
        )
        message = _expect_error(client, "/v1/search")
        assert "cannot reach" in message
        assert "scrapping-service" in message
