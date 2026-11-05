"""MCP adapter configuration (ticket #149/#154).

The adapter is configured exclusively through environment variables: the MCP
host (Pi, Claude Desktop, Cursor, ...) passes the environment of the spawned
process, which is the only portable way to hand a secret to a stdio child.

Transport (ticket #154):

- ``MCP_TRANSPORT`` — ``"stdio"`` (default) or ``"streamable-http"``.
- ``MCP_HOST``     — Bind address (default ``"127.0.0.1"`` for HTTP mode).
- ``MCP_PORT``     — Bind port (default ``8090``).
- ``MCP_PATH``     — MCP endpoint path (default ``"/mcp"``).
- ``MCP_AUTH_TOKEN`` — Bearer token guarding the HTTP endpoint (optional).

Service connection (ticket #149):

- ``MCP_SCRAPPER_BASE_URL``       — service root (no trailing slash)
- ``MCP_SCRAPPER_API_KEY``        — bearer API key (required by the service)
- ``MCP_SCRAPPER_TIMEOUT_SECONDS``— outbound request timeout (default 180)
- ``MCP_SCRAPPER_MAX_OUTPUT_CHARS``— tool output truncation cap (200000)

``MCP_SCRAPER_*`` (the repo-name spelling) is accepted as an alias of every
variable above; when both spellings are set, ``MCP_SCRAPPER_`` wins.
``SCRAPER_API_KEY`` is additionally honored as a pass-through for the key
when the adapter variables are unset (handy for ``python -m app.mcp`` in
the service's own environment).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

#: Documented variable names (``SCRAPPER_`` spelling first: it matches the
#: repo name).  The ``SCRAPER_`` spelling is accepted as an alias.
BASE_URL_VAR = "MCP_SCRAPPER_BASE_URL"
ALIAS_BASE_URL_VAR = "MCP_SCRAPER_BASE_URL"
API_KEY_VAR = "MCP_SCRAPPER_API_KEY"
ALIAS_API_KEY_VAR = "MCP_SCRAPER_API_KEY"
TIMEOUT_VAR = "MCP_SCRAPPER_TIMEOUT_SECONDS"
ALIAS_TIMEOUT_VAR = "MCP_SCRAPER_TIMEOUT_SECONDS"
MAX_OUTPUT_VAR = "MCP_SCRAPPER_MAX_OUTPUT_CHARS"
ALIAS_MAX_OUTPUT_VAR = "MCP_SCRAPER_MAX_OUTPUT_CHARS"
CONFIG_PATH_VAR = "MCP_SCRAPPER_CONFIG_PATH"
ALIAS_CONFIG_PATH_VAR = "MCP_SCRAPER_CONFIG_PATH"

# Streamable-HTTP transport (ticket #154)
TRANSPORT_VAR = "MCP_TRANSPORT"
HOST_VAR = "MCP_HOST"
PORT_VAR = "MCP_PORT"
PATH_VAR = "MCP_PATH"
AUTH_TOKEN_VAR = "MCP_AUTH_TOKEN"


def _env(name: str, alias: str | None = None) -> str | None:
    """Return the first non-empty value among ``name`` and ``alias``."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        value = os.environ.get(alias) if alias else None
    if value is None or not value.strip():
        return None
    return value.strip()


def _service_env(name: str) -> str | None:
    """Fallback to the service's own environment variables.

    ``MCP_SCRAPER_API_KEY`` doubles as a pass-through of ``SCRAPER_API_KEY``
    when only the service variable is set (handy for ``python -m app.mcp``
    in the service's own environment).
    """
    return os.environ.get(name)


class MCPAdapterConfig(BaseModel):
    """Validated adapter configuration."""

    base_url: str = Field(
        ...,
        min_length=1,
        pattern=r"^https?://",
        description=(
            "Root of the scrapping-service HTTP API, e.g. "
            "``http://localhost:8080`` (no trailing slash)."
        ),
    )
    api_key: str | None = Field(
        default=None,
        description=(
            "Bearer API key.  When the service does not require a key the "
            "adapter sends no Authorization header.  Never serialized."
        ),
        repr=False,
    )
    timeout_seconds: float = Field(
        default=180.0,
        gt=0,
        le=900,
        description=(
            "Per-request timeout in seconds.  Must comfortably exceed the "
            "service's browser-mode scrape timeout (up to 180 s)."
        ),
    )
    max_output_chars: int = Field(
        default=200_000,
        ge=1000,
        le=2_000_000,
        description=(
            "Maximum characters of a single tool result before truncation. "
            "Truncation appends a marker naming the dropped remainder."
        ),
    )
    transport: str = Field(
        default="stdio",
        description=("Transport mode: ``'stdio'`` (default) or ``'streamable-http'``."),
    )
    host: str = Field(
        default="127.0.0.1",
        description=(
            "Bind address for the HTTP endpoint.  Default ``'127.0.0.1'`` "
            "(loopback).  Binding to a non-loopback address without "
            "authentication is logged as a warning."
        ),
    )
    port: int = Field(
        default=8090,
        ge=1,
        le=65535,
        description=("Bind port for the HTTP endpoint.  Default ``8090``."),
    )
    path: str = Field(
        default="/mcp",
        description=("Path for the MCP endpoint.  Default ``'/mcp'``."),
    )
    auth_token: str | None = Field(
        default=None,
        description=(
            "Bearer token guarding the MCP endpoint.  When set, clients must "
            "send ``Authorization: Bearer <token>`` or ``x-api-key: <token>``. "
            "If unset, authentication is disabled and the adapter uses "
            "``MCP_SCRAPPER_API_KEY`` as the upstream credential."
        ),
        repr=False,
    )

    @property
    def service_url(self) -> str:
        """The service root with no trailing slash."""
        return self.base_url.rstrip("/")

    def to_dict(self) -> dict[str, Any]:
        """A redacted view (safe for logs and the ``adapter_info`` tool)."""
        return {
            "base_url": self.service_url,
            "api_key_configured": self.api_key is not None,
            "timeout_seconds": self.timeout_seconds,
            "max_output_chars": self.max_output_chars,
            "transport": self.transport,
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "auth_token_configured": self.auth_token is not None,
        }


def load_config(env: dict[str, str] | None = None) -> MCPAdapterConfig:
    """Build an :class:`MCPAdapterConfig` from the environment.

    Raises :class:`MCPConfigError` when ``MCP_SCRAPER_BASE_URL`` is missing
    or invalid, so the adapter fails at startup with an actionable message
    instead of a cryptic HTTP error on the first tool call.
    """
    env = dict(os.environ if env is None else env)

    base_url = _env(BASE_URL_VAR, ALIAS_BASE_URL_VAR) or _service_env("SCRAPER_BASE_URL")
    if not base_url:
        raise MCPConfigError(
            "MCP_SCRAPER_BASE_URL is not set. Point the adapter at a running "
            "scrapping-service, e.g. MCP_SCRAPER_BASE_URL=http://localhost:8080 "
            "(or the MCP_SCRAPPER_BASE_URL alias)."
        )

    api_key = _env(API_KEY_VAR, ALIAS_API_KEY_VAR) or _service_env("SCRAPER_API_KEY")

    timeout_raw = _env(TIMEOUT_VAR, ALIAS_TIMEOUT_VAR)
    try:
        timeout = float(timeout_raw) if timeout_raw else 180.0
    except ValueError as exc:
        raise MCPConfigError(
            f"MCP_SCRAPPER_TIMEOUT_SECONDS must be a number of seconds, got {timeout_raw!r}"
        ) from exc

    max_output_raw = _env(MAX_OUTPUT_VAR, ALIAS_MAX_OUTPUT_VAR)
    try:
        max_output = int(max_output_raw) if max_output_raw else 200_000
    except ValueError as exc:
        raise MCPConfigError(
            f"MCP_SCRAPPER_MAX_OUTPUT_CHARS must be an integer, got {max_output_raw!r}"
        ) from exc

    transport_raw = _env(TRANSPORT_VAR)
    if transport_raw is not None and transport_raw.lower() not in ("stdio", "streamable-http"):
        raise MCPConfigError(
            f"MCP_TRANSPORT must be 'stdio' or 'streamable-http', got {transport_raw!r}"
        )
    transport = transport_raw if transport_raw is not None else "stdio"

    host_raw = _env(HOST_VAR)
    host = host_raw if host_raw else "127.0.0.1"

    port_raw = _env(PORT_VAR)
    try:
        port = int(port_raw) if port_raw else 8090
    except ValueError as exc:
        raise MCPConfigError(f"MCP_PORT must be an integer, got {port_raw!r}") from exc

    path_raw = _env(PATH_VAR)
    path = path_raw if path_raw else "/mcp"

    auth_token = _env(AUTH_TOKEN_VAR)

    config_path = _env(CONFIG_PATH_VAR, ALIAS_CONFIG_PATH_VAR) or os.environ.get("CONFIG_PATH")
    if config_path:
        path_ = Path(config_path)
        if not path_.exists():
            raise MCPConfigError(f"{CONFIG_PATH_VAR} points to a file that does not exist: {path_}")

    try:
        return MCPAdapterConfig(
            base_url=base_url,
            api_key=api_key,
            timeout_seconds=timeout,
            max_output_chars=max_output,
            transport=transport,
            host=host,
            port=port,
            path=path,
            auth_token=auth_token,
        )
    except Exception as exc:  # pydantic ValidationError and friends
        raise MCPConfigError(f"Invalid MCP adapter configuration: {exc}") from exc


class MCPConfigError(Exception):
    """The adapter cannot start: its own environment is wrong.

    Rendered to the MCP host as a clean startup error (never contains the
    API key value).
    """


def _redact_secret(text: str, secret: str | None) -> str:
    """Replace every occurrence of *secret* in *text* with a marker.

    This is the last line of defence for the API key: if a service error
    message or URL ever contains it, the rendered tool result does not.
    """
    if not secret or not text:
        return text
    return text.replace(secret, "<REDACTED>")
