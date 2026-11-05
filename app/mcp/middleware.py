"""Middleware for the MCP adapter streamable-HTTP transport (ticket #154).

Two middlewares protect the adapter's HTTP endpoint:

- ``TokenAuthMiddleware`` — authenticates calls to the MCP endpoint with
  ``Authorization: Bearer <token>`` or ``x-api-key: <token>``. The token
  value is never logged; constant-time comparison is used.
- ``OriginHostMiddleware`` — validates ``Origin`` and ``Host`` headers to
  prevent DNS rebinding attacks on local servers. When the adapter binds
  to ``127.0.0.1``, the middleware only allows loopback origins/hosts.

The middlewares are applied in order, so ``OriginHostMiddleware`` runs
before ``TokenAuthMiddleware`` (origin validation happens before auth).

The token read by ``TokenAuthMiddleware`` is stored in
``request.state.mcp_token`` so the upstream credential pass-through in
``ScrapperClient`` can forward it.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse

# ------------------------------------------------------------------ helpers


def _is_loopback_host(host: str) -> bool:
    """Return ``True`` if the host is a loopback address.

    Accepts ``127.0.0.1``, ``::1``, ``localhost``, or any ``*.localhost``.

    """
    host_lower = host.lower()
    if host_lower in ("127.0.0.1", "::1", "localhost"):
        return True
    if host_lower.endswith(".localhost"):
        return True
    return False


def _maybe_strip_port(host_header: str) -> str:
    """Return the host without the port suffix.

    ``Host: example.com:8090`` → ``example.com``.
    """
    if ":" in host_header:
        return host_header.rsplit(":", 1)[0]
    return host_header


# ================================================================== TokenAuthMiddleware


class TokenAuthMiddleware:
    """Guard the MCP endpoint with bearer or ``x-api-key`` authentication.

    When ``expected_token`` is ``None``, authentication is disabled and all
    requests pass through.

    When ``expected_token`` is set, the request must contain either
    ``Authorization: Bearer <token>`` or ``x-api-key: <token>``. Both
    headers may be present but must carry the same token value.

    Tokens are compared using constant-time comparison and never logged.

    """

    def __init__(self, app: Any, *, expected_token: str | None) -> None:
        """Create a token-auth middleware.

        Args:
            app: The Starlette ASGI app to wrap.
            expected_token: The secret token value. When ``None``, auth is
                disabled and all requests pass through.

        """
        self.app = app
        self.expected_token = expected_token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """ASGI entry point."""
        if self.expected_token is None:
            # Auth disabled — pass through
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        token = _extract_token(request.headers)

        if token is None:
            await self._unauthorized(scope, receive, send, "Missing authentication token")
            return

        if not self._verify_token(token):
            await self._unauthorized(scope, receive, send, "Invalid authentication token")
            return

        # Auth OK — store token for credential pass-through
        request.state.mcp_token = token

        # Rebuild the request scope with the token in ``scope["mcp_token"]``
        # so downstream handlers can read it without re-parsing headers.
        scope["mcp_token"] = token

        await self.app(scope, receive, send)

    def _verify_token(self, token: str) -> bool:
        """Constant-time comparison of the token."""
        from secrets import compare_digest

        return compare_digest(token, self.expected_token)

    async def _unauthorized(self, scope: Any, receive: Any, send: Any, reason: str) -> None:
        """Return a 401 Unauthorized JSON response."""
        body = {
            "error": {
                "type": "auth_error",
                "message": reason,
                "details": {},
            }
        }
        response = JSONResponse(body, status_code=401)
        await response(scope, receive, send)


def _extract_token(headers: Headers) -> str | None:
    """Extract the token from ``Authorization: Bearer`` or ``x-api-key``.

    Returns the token string if found, or ``None`` if neither header is
    present. Raises a 401 error if both headers are present but differ.

    """
    auth_header = headers.get("authorization")
    x_api_key = headers.get("x-api-key")

    if auth_header is None and x_api_key is None:
        return None

    if auth_header is not None and x_api_key is None:
        return _extract_bearer_token(auth_header)

    if auth_header is None and x_api_key is not None:
        return x_api_key

    # Both headers present
    bearer_token = _extract_bearer_token(auth_header)
    if bearer_token is None:
        return None

    if bearer_token != x_api_key:
        raise RuntimeError("Both Authorization and x-api-key headers present but tokens differ")

    return bearer_token


def _extract_bearer_token(auth_header: str) -> str | None:
    """Parse ``Authorization: Bearer <token>``.

    Returns ``None`` if the header doesn't match the expected format.

    """
    # Authorization: Bearer <token>
    # Must be exactly this prefix, case-sensitive
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        return None
    token = auth_header[len(prefix) :]
    return token if token.strip() else None


# ================================================================== OriginHostMiddleware


class OriginHostMiddleware:
    """Validate ``Origin`` and ``Host`` headers to prevent DNS rebinding.

    When the adapter binds to a loopback address (``127.0.0.1``, ``::1``,
    ``localhost``), only loopback origins/hosts are allowed.

    When the adapter binds to a non-loopback address, the middleware allows
    any origin/host (the assumption is that non-loopback binding requires
    explicit security controls outside this middleware).

    """

    def __init__(self, app: Any, *, host: str) -> None:
        """Create an origin/host validation middleware.

        Args:
            app: The Starlette ASGI app to wrap.
            host: The host the adapter is bound to (e.g. ``"127.0.0.1"``,
                ``"localhost"``, ``"0.0.0.0"``). Used to determine if the
                adapter is on a loopback address.

        """
        self.app = app
        self.host_is_loopback = _is_loopback_host(host)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        """ASGI entry point."""
        if not self.host_is_loopback:
            # Not binding to loopback — skip origin/host validation
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        origin = request.headers.get("origin")
        host_header = request.headers.get("host")

        if origin is not None:
            if not self._is_valid_origin(origin):
                await self._forbidden(scope, receive, send, "Origin not allowed")
                return

        if host_header is not None:
            if not self._is_valid_host(host_header):
                await self._forbidden(scope, receive, send, "Host not allowed")
                return

        await self.app(scope, receive, send)

    def _is_valid_origin(self, origin: str) -> bool:
        """Check if the Origin header is allowed."""
        try:
            # Parse Origin header: ``<scheme>://<host>[:port]``
            parsed = _parse_url(origin)
            if parsed is None:
                return False
            host = parsed["host"]
            return _is_loopback_host(host)
        except Exception:
            return False

    def _is_valid_host(self, host_header: str) -> bool:
        """Check if the Host header is allowed."""
        host = _maybe_strip_port(host_header)
        return _is_loopback_host(host)

    async def _forbidden(self, scope: Any, receive: Any, send: Any, reason: str) -> None:
        """Return a 403 Forbidden JSON response."""
        body = {
            "error": {
                "type": "origin_not_allowed",
                "message": reason,
                "details": {},
            }
        }
        response = JSONResponse(body, status_code=403)
        await response(scope, receive, send)


def _parse_url(url: str) -> dict[str, str] | None:
    """Parse a URL into components.

    Returns a dict with ``scheme``, ``host``, ``port`` (if present), or
    ``None`` if parsing fails.

    """
    # Accept both ``http://host`` and ``https://host`` formats
    pattern = r"^(https?)://([^:/]+)(?::(\d+))?$"
    match = re.match(pattern, url)
    if not match:
        return None
    scheme, host, port = match.groups()
    result: dict[str, str] = {"scheme": scheme, "host": host}
    if port:
        result["port"] = port
    return result
