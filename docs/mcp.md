# MCP Adapter

The scrapping-service exposes its capabilities as **Model Context Protocol**
(MCP) tools through `app/mcp` — a thin adapter that proxies every tool call
to the service's REST API. Agents (Claude, Pi, Cursor, ...) can therefore
drive scraping, search, transcripts and the interactive browser without
implementing any scraping logic themselves.

```text
MCP host (Pi, Claude Desktop, Claude Code, Cursor, uvx, ...)
        │  MCP: tools/list, tools/call  (stdio or streamable HTTP)
        v
app.mcp  (this adapter)
        │  REST: /v1/search, /v1/scrape, /v1/transcript,
        │        /v1/browser/*, /v1/cache/*, /health
        v
scrapping-service  (the real service: security, caching, providers)
```

The adapter is a **pass-through proxy**: it does not reimplement scraping,
search routing, caching or security. All of those policies live in the
service and apply unchanged — a URL the service rejects (SSRF guard, domain
rules) is rejected identically by the adapter, and the adapter never
retries or rewrites a rejected request.

## Quick start

Run a scrapping-service, then point the adapter at it. Only two variables
are mandatory; everything else has a default.

| Variable (alias) | Required | Default | Meaning |
| --- | --- | --- | --- |
| `MCP_SCRAPPER_BASE_URL` (`MCP_SCRAPER_BASE_URL`) | **yes** | — | Service root, e.g. `http://localhost:8080` (no trailing slash) |
| `MCP_SCRAPPER_API_KEY` (`MCP_SCRAPER_API_KEY`) | if the service needs one | — | Bearer key; `SCRAPER_API_KEY` is honored as a pass-through |
| `MCP_SCRAPPER_TIMEOUT_SECONDS` | no | `180` | Per-request timeout (must exceed the service's browser-mode timeout) |
| `MCP_SCRAPPER_MAX_OUTPUT_CHARS` | no | `200000` | Hard cap per tool result; longer output is truncated with a marker |

```bash
MCP_SCRAPPER_BASE_URL=http://localhost:8080
MCP_SCRAPPER_API_KEY=<the service's key>
```

## Installation

The adapter ships with the service repository as the `mcp` extra:

```bash
pip install -e ".[mcp]"            # or: pip install "scraper-api[mcp]"
```

That installs the `mcp` SDK and two console scripts:

| Script | Transport |
| --- | --- |
| `scrapping-service-mcp` | stdio (what MCP hosts launch) |
| `scrapping-service-mcp-http` | streamable HTTP on port 8090 (`/mcp`, `/health`, `/metrics`) |

`python -m app.mcp` runs the same stdio server. **Without cloning**, the
package is installable from git:

```bash
pip install "git+https://github.com/javiyt/scrapping-service.git#egg=scraper-api[mcp]"
```

The adapter is also available as a standalone container target that needs no
browser or other runtime dependency:

```bash
docker build --target mcp -t scrapping-service-mcp .
docker run -i --rm \
    -e MCP_SCRAPPER_BASE_URL=https://scraper.example.com \
    -e MCP_SCRAPPER_API_KEY=*** \
    scrapping-service-mcp                       # stdio
```

Streamable HTTP for remote clients:

```bash
docker run -d -p 8090:8090 \
    -e MCP_SCRAPPER_BASE_URL=https://scraper.example.com \
    -e MCP_SCRAPPER_API_KEY=*** \
    -e MCP_AUTH_TOKEN=*** \
    scrapping-service-mcp scrapping-service-mcp-http
```

`MCP_AUTH_TOKEN` (optional) adds bearer auth to the adapter's own HTTP
endpoint — recommended whenever port 8090 is reachable beyond your machine.

## Client configuration

### Pi

Add the adapter to your Pi MCP config (stdio):

```jsonc
{
  "mcp": {
    "servers": {
      "scrapping-service": {
        "command": "python",
        "args": ["-m", "app.mcp"],
        "cwd": "/path/to/scrapping-service",
        "env": {
          "MCP_SCRAPPER_BASE_URL": "http://localhost:8080",
          "MCP_SCRAPPER_API_KEY": "<key>"
        }
      }
    }
  }
}
```

If you installed the package instead of pointing at a clone, replace
`command`/`args`/`cwd` with the console script:

```jsonc
{
  "command": "scrapping-service-mcp",
  "env": { "MCP_SCRAPPER_BASE_URL": "http://localhost:8080" }
}
```

### Claude Desktop

`claude_desktop_config.json` → `mcpServers`:

```jsonc
{
  "mcpServers": {
    "scrapping-service": {
      "command": "scrapping-service-mcp",
      "env": {
        "MCP_SCRAPPER_BASE_URL": "http://localhost:8080",
        "MCP_SCRAPPER_API_KEY": "<key>"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add scrapping-service \
  -- scrapping-service-mcp \
  -e MCP_SCRAPPER_BASE_URL=http://localhost:8080 \
  -e MCP_SCRAPPER_API_KEY=<key>
```

Or the streamable-HTTP transport for a shared, remotely-hosted adapter:

```bash
claude mcp add --transport http scrapping-service http://mcp-host:8090/mcp
```

### Cursor / VS Code

`mcp.json`:

```jsonc
{
  "mcpServers": {
    "scrapping-service": {
      "command": "scrapping-service-mcp",
      "env": {
        "MCP_SCRAPPER_BASE_URL": "http://localhost:8080",
        "MCP_SCRAPPER_API_KEY": "<key>"
      }
    }
  }
}
```

### Generic stdio config

Any MCP host that speaks stdio: spawn `scrapping-service-mcp` (or
`python -m app.mcp`) with the two environment variables above and speak
JSON-RPC on stdin/stdout. The adapter's startup handshake is the standard
MCP `initialize`.

### Generic HTTP config

Streamable HTTP endpoint at `http://<adapter-host>:8090/mcp` (JSON
responses). Pass `MCP_AUTH_TOKEN` as `Authorization: Bearer <token>` if it
was set on the adapter.

## Tools reference

All 19 tools. Every result is a JSON document; the service's own error
envelope is preserved inside it.

| Tool | REST mapping | Purpose |
| --- | --- | --- |
| `adapter_info` | `GET /health` | Adapter config (key presence, URL, version) + service health. **Call this first** to verify wiring. |
| `search` | `POST /v1/search` | Web/social/videos/news search. Params: `query` (required), `verticals`, `language`, `safe_search`, `time_range`, `max_results`, `sources`, `provider`, `force_refresh`. |
| `search_capabilities` | `GET /v1/search/capabilities` | What the service can search (providers, verticals). |
| `fetch_url` | `POST /v1/scrape` | Fetch one URL. Params: `url` (required), `mode` (`http`/`browser`), `extract` (`html`/`markdown`), `timeout_seconds`. |
| `fetch_url_batch` | `POST /v1/scrape` (parallel) | Fetch up to 20 URLs in one call; per-URL results + partial failures. |
| `transcript` | `GET /v1/transcript` | YouTube captions. Params: `url` (required), `lang`, `force_refresh`. |
| `browser_capabilities` | `GET /v1/browser/capabilities` | Browser feature flags and limits for the current profile. |
| `browser_create_session` | `POST /v1/browser/sessions` | Open an interactive session. Params: `profile_id` (optional). |
| `browser_navigate` | `POST /v1/browser/{id}/tabs/{tab}/navigate` | Navigate a tab. Params: `session_id`, `tab_id`, `url` (required). |
| `browser_click` | `POST /v1/browser/{id}/click` | Click a selector. Params: `session_id`, `tab_id`, `selector`. |
| `browser_type` | `POST /v1/browser/{id}/type` | Type into a field. Params: `session_id`, `tab_id`, `selector`, `text`. |
| `browser_evaluate` | `POST /v1/browser/{id}/evaluate` | Run JS in the page (requires `SCRAPER_BROWSER_ALLOW_EVALUATE=true` server-side). Params: `session_id`, `tab_id`, `expression`. |
| `browser_close_tab` | `DELETE /v1/browser/{id}/tabs/{tab}` | Close one tab. |
| `browser_close_session` | `DELETE /v1/browser/{id}` | Close the session and its tabs. |
| `cache_purge` | `POST /v1/cache/purge` | Clear scrape/search cache entries. |
| `cache_delete` | `DELETE /v1/cache/{key}` | Delete one cache key. |
| `cache_cleanup` | `POST /v1/cache/cleanup` | Remove expired entries. |
| `cache_vacuum` | `POST /v1/cache/vacuum` | Reclaim disk space. |
| `cache_stats` | `GET /v1/cache/stats` | Cache size/TTL statistics. |

(The browser tool signatures — session/tab ids and the exact parameter
names — follow the service's Agent Browser API contract; call
`browser_capabilities` first and consult
[the API reference](api.md) for the current shape.)

### Typical agent flow

```text
adapter_info            verify the adapter reaches the service
search                  find candidate URLs
fetch_url / fetch_url_batch   read the pages (http or rendered)
browser_*               when the page needs interaction
transcript              when the result is a video
```

## Auth

- **To the service**: the adapter sends `Authorization: Bearer
  <MCP_SCRAPPER_API_KEY>` on every REST call. If no key is configured the
  adapter sends no `Authorization` header (services without auth work
  fine). The key never appears in any tool result, log line or the
  adapter's `/health` output — `adapter_info` reports
  `api_key_configured: true/false` only.
- **To the adapter** (HTTP transport only): `MCP_AUTH_TOKEN`, optional.

## Error semantics

Every tool returns `{"error": {"type", "message", "details"}}` on failure,
and the MCP `isError` flag is set, so hosts render it as a tool error.
Types:

| `error.type` | Meaning |
| --- | --- |
| `adapter_error` | The adapter itself rejected the call (unknown tool, invalid arguments, unconfigured). No service request was made. |
| `auth_error` | The service rejected the key (401/403). Check `MCP_SCRAPPER_API_KEY`. |
| `service_timeout` | The service did not answer within `MCP_SCRAPPER_TIMEOUT_SECONDS`. |
| `unreachable` | The service is not reachable (DNS/connection failure). Check `MCP_SCRAPPER_BASE_URL`. |
| `service_error` | The service answered with an error (SSRF/domain rejection, provider failure, ...). `message`/`details` come from the service. |

Security rejections (SSRF guard, domain policy) are **terminal**: the
adapter reports them and never retries with different flags.

## Limits

- **Output size**: each tool result is capped at `MCP_SCRAPPER_MAX_OUTPUT_CHARS`
  characters; longer content is truncated with a marker naming the dropped
  remainder.
- **Batch size**: `fetch_url_batch` accepts at most 20 URLs.
- **Browser**: session TTL, max sessions and max tabs per session are the
  service's limits (`browser_capabilities` reports them); the adapter adds
  none.

## Observability

- One structured JSON line per tool call on stderr: `tool`, `request_id`,
  `result` and `duration_ms` — never payloads, never the API key.
- The service's own `x-request-id` response header is captured and logged
  as `service_request_id`, so a tool call and the service's log line for
  the same request correlate.
- The adapter also sends its own `X-MCP-Request-Id` header, so the service
  can correlate in the reverse direction.
- Streamable-HTTP deployments expose `GET /metrics` (adapter Prometheus
  counters: `scraper_mcp_tool_calls_total{tool,result}`, duration
  sum/count, session errors) and `GET /health` (adapter liveness + service
  reachability).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `adapter_info` shows `service.reachable: false` | `MCP_SCRAPPER_BASE_URL` points at a running service? Firewalled? Try `curl -H "Authorization: Bearer <key>" <base>/health`. |
| `auth_error` on every tool | Key mismatch — the adapter key must be one of the service's keys. |
| `unreachable` | DNS/TLS: is the service reachable from the *MCP host* process (not just your shell)? |
| `service_timeout` on browser fetches | The browser-mode timeout inside the service can reach 180 s; raise `MCP_SCRAPPER_TIMEOUT_SECONDS` above it. |
| `browser_evaluate` refused | The service must be started with `SCRAPER_BROWSER_ALLOW_EVALUATE=true`; the adapter cannot override this. |
| Garbled output in the host | Raise `MCP_SCRAPPER_MAX_OUTPUT_CHARS`; check the truncation marker names what was dropped. |
| Streamable-HTTP 401 | `MCP_AUTH_TOKEN` was set on the adapter — send it as a bearer token. |
