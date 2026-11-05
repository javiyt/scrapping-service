"""HTTP client for the MCP adapter (ticket #149).

A thin async client over the scrapping-service REST API.  Responsibilities:

- attach ``Authorization: Bearer <key>`` to every call;
- map every failure (network, HTTP, transport) onto one of
  :class:`app.mcp.errors.MCPError` types with a *safe* message — the API
  key value never appears in a message;
- bound tool output size via :meth:`ScrapperClient.render` (JSON
  serialization + truncation) so a pathological page cannot flood the MCP
  channel;
- accept an injectable transport (tests use ``httpx.MockTransport``).

The client is created once per adapter process and shared by all tools.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import httpx

from app.mcp.config import _redact_secret

logger = logging.getLogger("scraper-api.mcp.client")


class MCPError(Exception):
    """Base class for adapter errors.  ``message`` is safe to render."""

    error_type = "adapter_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {"type": self.error_type, "message": self.message, "details": self.details}
        }


class AuthError(MCPError):
    """The service rejected our credentials (401/403 from the service)."""

    error_type = "auth_error"


class ServiceUnreachableError(MCPError):
    """The service could not be reached (connection error / DNS)."""

    error_type = "service_unreachable"


class ServiceTimeoutError(MCPError):
    """The service did not answer within the configured timeout."""

    error_type = "service_timeout"


class ServiceError(MCPError):
    """The service answered with a non-2xx status (mapped, safe message)."""

    error_type = "service_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        if error_type:
            self.error_type = error_type
        super().__init__(message, details=details)


class BadResponseError(MCPError):
    """The service answered 2xx with a body that cannot be parsed."""

    error_type = "bad_response"


def _safe_message(value: Any, *, redact: str | None, max_chars: int = 2000) -> str:
    """Build a bounded, redacted message from an unknown blob."""
    text = str(value)
    text = _redact_secret(text, redact)
    if len(text) > max_chars:
        text = text[:max_chars] + f"… (+{len(text) - max_chars} chars omitted)"
    return text


class ScrapperClient:
    """An async HTTP client bound to one scrapping-service deployment.

    The client holds a fallback ``api_key`` used when no per-request
    override is provided (ticket #154).  For credential pass-through,
    callers pass ``api_key_override`` to ``request()`` / ``get()`` /
    ``post()`` / ``delete()`` — that token replaces the client's
    ``Authorization`` header.

    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout_seconds: float = 180.0,
        max_output_chars: int = 200_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key  # fallback key
        self.max_output_chars = max_output_chars
        self._default_headers: dict[str, str] = {"Accept": "application/json"}
        if api_key:
            self._default_headers["Authorization"] = f"Bearer {api_key}"
        self._owns_transport = transport is None
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds),
            headers=self._default_headers.copy(),
            follow_redirects=True,
            transport=transport,
        )

    async def aclose(self) -> None:
        if self._owns_transport:
            await self._http.aclose()

    # ------------------------------------------------------- verb shortcuts
    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        api_key_override: str | None = None,
    ) -> httpx.Response:
        return await self.request("GET", path, params=params, api_key_override=api_key_override)

    async def post(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        api_key_override: str | None = None,
    ) -> httpx.Response:
        return await self.request(
            "POST", path, params=params, json_body=json_body, api_key_override=api_key_override
        )

    async def delete(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        api_key_override: str | None = None,
    ) -> httpx.Response:
        return await self.request("DELETE", path, params=params, api_key_override=api_key_override)

    def config_view(self) -> dict[str, Any]:
        """A redacted view of the adapter configuration (safe for logs/results)."""
        return {
            "base_url": self.base_url,
            "api_key_configured": self.api_key is not None,
            "max_output_chars": self.max_output_chars,
        }

    # ------------------------------------------------------------------ core
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        trace_id: str | None = None,
        api_key_override: str | None = None,
    ) -> httpx.Response:
        """Perform one request; map every failure onto an :class:`MCPError`.

        Args:
            method: HTTP method.
            path: Path relative to ``base_url``.
            params: Query parameters.
            json_body: JSON request body.
            trace_id: Request ID to propagate to the service.
            api_key_override: Replace the client's ``api_key`` for this
                request (credential pass-through).

        """
        headers: dict[str, str] = {}
        if trace_id:
            headers["X-MCP-Request-Id"] = trace_id
        if api_key_override:
            headers["Authorization"] = f"Bearer {api_key_override}"
        started = time.monotonic()
        try:
            response = await self._http.request(
                method, path, params=params, json=json_body, headers=headers
            )
        except httpx.TimeoutException as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            raise ServiceTimeoutError(
                f"scrapping-service did not answer within {elapsed} ms on {method} {path}",
                details={"method": method, "path": path, "elapsed_ms": elapsed},
            ) from exc
        except httpx.HTTPError as exc:
            raise ServiceUnreachableError(
                f"cannot reach scrapping-service at {self.base_url} "
                f"({type(exc).__name__}) — is the service running?",
                details={"base_url": self.base_url},
            ) from exc

        self._map_error_response(response, method, path)
        return response

    def _map_error_response(self, response: httpx.Response, method: str, path: str) -> None:
        """Raise the mapped :class:`MCPError` for a non-2xx response.

        The service's standard error envelope (``{"error": {...}}``) is
        extracted when present; the *message* is redacted against our API
        key and bounded.  Raw response bodies never pass through.
        """
        if 200 <= response.status_code < 300:
            return

        message = f"scrapping-service returned HTTP {response.status_code} for {method} {path}"
        error_type: str | None = None
        details: dict[str, Any] = {"status_code": response.status_code}
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                err_message = error.get("message")
                if isinstance(err_message, str) and err_message:
                    message = _safe_message(err_message, redact=self.api_key)
                if isinstance(error.get("type"), str):
                    error_type = error["type"]
                if isinstance(error.get("details"), dict):
                    details["error"] = _safe_details(error["details"], self.api_key)
            else:
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if isinstance(detail, str) and detail:
                    message = _safe_message(detail, redact=self.api_key)
                elif isinstance(detail, dict) and isinstance(detail.get("error"), dict):
                    inner = detail["error"]
                    err_message = inner.get("message")
                    if isinstance(err_message, str) and err_message:
                        message = _safe_message(err_message, redact=self.api_key)
                    if isinstance(inner.get("type"), str):
                        error_type = inner["type"]
        except (ValueError, TypeError):
            pass  # non-JSON error body — the status-code message stands

        if response.status_code in (401, 403):
            raise AuthError(message, details=details)
        raise ServiceError(
            message, status_code=response.status_code, error_type=error_type, details=details
        )

    # ---------------------------------------------------------------- output
    def render(self, payload: Any) -> str:
        """Serialize *payload* for a tool result and bound its size.

        Truncation is explicit: the text ends with a marker that names how
        much was dropped, so the LLM can re-request with a narrower scope
        instead of silently receiving a cut-off document.
        """
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) <= self.max_output_chars:
            return text
        dropped = len(text) - self.max_output_chars
        return (
            text[: self.max_output_chars] + f"\n… [truncated: {dropped} characters omitted; "
            "re-request with a narrower scope (e.g. a specific element, a "
            "smaller limit) to read the rest]"
        )

    def render_raw(self, text: str) -> str:
        """Bound a non-JSON text result (same truncation semantics)."""
        if len(text) <= self.max_output_chars:
            return text
        dropped = len(text) - self.max_output_chars
        return text[: self.max_output_chars] + f"\n… [truncated: {dropped} characters omitted]"


def _safe_details(details: dict[str, Any], redact: str | None) -> dict[str, Any]:
    """A redacted copy of the service's error ``details`` (bounded strings)."""
    safe: dict[str, Any] = {}
    for key, value in list(details.items())[:20]:
        if isinstance(value, str):
            safe[key] = _safe_message(value, redact=redact)
        elif isinstance(value, (int, float, bool)) or value is None:
            safe[key] = value
        else:
            safe[key] = _safe_message(value, redact=redact)
    return safe


def new_trace_id() -> str:
    """A short, unguessable request/trace id propagated to the service."""
    return uuid.uuid4().hex[:16]
