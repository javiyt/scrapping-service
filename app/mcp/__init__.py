"""MCP adapter for scrapping-service (ticket #149).

A standalone Model Context Protocol server that exposes the scrapping-service
capabilities as MCP tools: web search, URL fetch, interactive browser,
YouTube transcripts, search capabilities and cache operations.

The adapter is deliberately *thin*: every tool call is a plain REST call to
the scrapping-service HTTP API.  It never talks to SearXNG, Camofox,
Botasaurus or YouTube directly — all security policies (SSRF guards, domain
rules, auth profiles, result caching, resource limits) stay in the service.

Entry points:

- ``scrapping-service-mcp``  — console script (stdlib + ``mcp`` SDK)
- ``python -m app.mcp``      — equivalent
- ``uvx --from <local or package> scrapping-service-mcp``

Configuration (environment variables, all prefixed ``MCP_SCRAPPER_``):

- ``MCP_SCRAPPER_BASE_URL``   — the service root, e.g. ``http://scraper:8080``
- ``MCP_SCRAPPER_API_KEY``    — bearer API key (required by the service)
- ``MCP_SCRAPPER_TIMEOUT_SECONDS``  — outbound request timeout (default 180)
- ``MCP_SCRAPPER_MAX_OUTPUT_CHARS`` — tool output truncation cap (default 200000)

``MCP_SCRAPER_*`` is accepted as an alias of every variable above, and
``SCRAPER_API_KEY`` passes through for the key when the adapter variable is
unset. The adapter has no YAML config of its own; the ``mcp`` key of
``configs/config.yaml`` documents the same variables for the in-process
entry point.

Security model:

- The API key travels only in the ``Authorization: Bearer`` header of the
  outbound HTTP request.  It is never logged, never included in tool
  results, and error payloads are sanitized before being rendered to the
  MCP client.
- Tool outputs are bounded (truncated with a marker) so a large page can
  never produce an unbounded MCP message.
"""

from __future__ import annotations

__all__ = ["MCP_ADAPTER_VERSION"]

MCP_ADAPTER_VERSION = "1.0.0"
