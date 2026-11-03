# Configuration

Configuration is loaded from `configs/config.yaml` by default, with environment
variable overrides applied on top. Request fields and authenticated profile
overrides can further specialize scrape behavior.

## Priority

1. Request fields.
2. Profile overrides.
3. Environment variables.
4. YAML config file.
5. Application defaults.

## Common Environment Variables

| Variable | Purpose |
| --- | --- |
| `SCRAPER_API_KEY` | Legacy single API key. |
| `SCRAPER_API_KEYS` | Comma-separated global API keys. |
| `SCRAPER_SERVER_HOST` | Bind address. |
| `SCRAPER_SERVER_PORT` | HTTP port. |
| `SCRAPER_CACHE_SQLITE_PATH` | SQLite cache path. |
| `SCRAPER_CACHE_DEFAULT_TTL_SECONDS` | Default cache TTL. |
| `SCRAPER_SCRAPER_MAX_CONCURRENCY` | Server-side scrape concurrency cap. |
| `SCRAPER_BROWSER_IDLE_TIMEOUT_SECONDS` | Browser idle release timeout. |
| `LOG_LEVEL` | Application log level. |
| `CONFIG_PATH` | Alternative YAML config path. |
| `SCRAPER_SEARCH_SEARXNG_ENABLED` | Enable the SearXNG search provider. |
| `SCRAPER_SEARCH_SEARXNG_BASE_URL` | Base URL of the SearXNG instance. |
| `SCRAPER_SEARCH_SEARXNG_TIMEOUT_SECONDS` | SearXNG HTTP timeout. |
| `SCRAPER_SEARCH_SEARXNG_DEFAULT_LANGUAGE` | Default BCP-47 language. |
| `SCRAPER_SEARCH_SEARXNG_CONFIG_CACHE_TTL_SECONDS` | SearXNG `/config` discovery cache TTL. |
| `SCRAPER_SEARCH_CACHE_ENABLED` | Enable the search result cache. |
| `SCRAPER_SEARCH_CACHE_DEFAULT_TTL_SECONDS` | Default search result TTL (`0` = never expires). |
| `SCRAPER_SEARCH_CACHE_STALE_IF_ERROR` | Serve an expired search entry when the live search fails. |

See
[`configs/config.example.yaml`](https://github.com/javiyt/scrapping-service/blob/main/configs/config.example.yaml)
for the full reference.

## Proxy

Global proxy settings can be configured in YAML or environment variables.
Per-request proxy overrides require `proxy.allow_request_override: true`.

```yaml
proxy:
  enabled: false
  url: null
  country: null
  allow_request_override: false
  block_private_proxy_hosts: true
```

## Cache Maintenance

The cache maintenance loop can delete expired rows, enforce row limits, enforce
approximate size limits, and optionally run `VACUUM`.

```yaml
cache:
  cleanup_enabled: true
  cleanup_interval_seconds: 3600
  delete_expired_after_seconds: 86400
  max_entries: 10000
  max_size_mb: 512
  vacuum_after_cleanup: false
```

## Search

The `search` section configures the provider-agnostic search API (see
[Search Providers](search-providers.md)).

```yaml
search:
  default_provider: auto        # or an explicit provider name (no fallback)
  provider_order: [searxng]     # routing priority for auto mode
  probe_timeout_seconds: 10     # bound for the capabilities/health probe
  cache:
    enabled: true
    default_ttl_seconds: 900
    stale_if_error: true
  providers:
    searxng:
      enabled: false            # disabled by default
      base_url: null            # e.g. http://searxng:8080
      timeout_seconds: 15
      default_language: null
      config_cache_ttl_seconds: 3600
```

Requests override the cache TTL per request (`cache_ttl_seconds`) and bypass
it (`force_refresh`). Stale-if-error serves an expired entry — flagged via
`metadata.stale` and a warning — when the live search fails, so search
availability degrades gracefully. Down search backends are reported by
`GET /health/deep` but never fail it: container restart policy stays
decoupled from search availability.

## Browser

The scraper service renders pages through a pluggable **browser backend**
abstraction.  **Botasaurus** is the *primary* backend (the engine that has
always been used).  **Camofox** is an *optional fallback* backend backed by
the external [camofox-browser](https://github.com/jo-inc/camofox-browser)
REST service.

### Backend selection

```yaml
browser:
  primary_backend: botasaurus        # always the primary engine
  fallback_backends: []              # bounded, operator-configured set
  # To enable the Camofox fallback:
  #   fallback_backends: ["camofox"]
```

When the primary backend fails with an **appropriate** error — a driver
crash, a navigation timeout, a blocked/challenge page, or a missing import —
the orchestrator tries each fallback backend **exactly once** (no retry
loop).  Failures that are *not* a browser problem (security/invalid-input
errors) are raised immediately and never trigger a fallback.  When every
backend fails, the **primary** backend's error is raised, preserving the
historical "browser failed" semantics and the original root cause.

The Camofox fallback only participates when it is listed in
`fallback_backends` **and** the top-level `camofox:` section is enabled.
When no fallback is configured, the service behaves exactly as before
(Botasaurus only).  The Camofox *service* runs as a separate container
(see [Deployment](deployment.md#external-search-backends-searxng-and-camofox));
its client is configured in the top-level `camofox` section
(see [Camofox client](#camofox-client)).

### Observability

* Every scrape response's `metadata` carries `fetch_backend` — the name of
  the backend that actually produced the HTML (`botasaurus`, `camofox`, or
  `null` for HTTP-only fetches).  The same value is recorded in the cache
  entry so a cached hit reports the backend that fetched it.
* Bounded Prometheus counters are emitted with a `backend` label:
  `browser_backend_attempt_total{backend}`,
  `browser_backend_success_total{backend}`,
  `browser_backend_error_total{backend}`, and
  `browser_backend_fallback_total{backend}`.  The label set is limited to the
  operator-configured backend names — it never grows from user input.

### Request modes (unchanged)

`mode` remains `http`, `browser`, or `auto` and is the **only** user-facing
control, exactly as before.  Backend selection is *not* a request field; it is
server configuration.  `auto` still prefers HTTP and escalates to the browser
backend(s) only when the HTTP result looks blocked or the HTTP fetch fails.

## Camofox client

The top-level `camofox` section configures the reusable HTTP client for the
**external** Camofox browser service.  It is shared by three consumers: the
Camofox search provider, the optional browser fallback, and the interactive
`/v1/browser` API.  Nothing browser-related is ever launched inside this
process — the client only talks to the external service over HTTP.

```yaml
camofox:
  enabled: false          # master switch (client built only when true)
  base_url: null          # e.g. http://camofox:9377
  timeout_seconds: 60     # per-request timeout (1-3600 s)
  access_key: null        # e.g. ${CAMOFOX_ACCESS_KEY}  — never a literal
  api_key: null           # e.g. ${CAMOFOX_API_KEY}    — never a literal
  user_id_prefix: scrapping-service
```

Environment overrides: `SCRAPER_CAMOFOX_ENABLED`, `SCRAPER_CAMOFOX_BASE_URL`,
`SCRAPER_CAMOFOX_TIMEOUT_SECONDS`, `SCRAPER_CAMOFOX_ACCESS_KEY`,
`SCRAPER_CAMOFOX_API_KEY`, `SCRAPER_CAMOFOX_USER_ID_PREFIX`.

**Secrets** — `access_key` / `api_key` must come from the environment (the
`${VAR}` form or the direct `SCRAPER_CAMOFOX_*` overrides).  Never put
literal key values in the config file.  An unexpanded `${VAR}` placeholder
(variable not set) is treated as *unset* with a startup warning and is never
sent on the wire.  Keys are excluded from all serialization and masked in
`repr()`.

**Session limits** are set by the consumers, not the client: the
interactive API (`agent_browser.max_sessions` / `max_tabs_per_session`)
and the scraper's own concurrency/pool settings bound fallback usage.

Deployment of the external Camofox service (networking, health, degraded
behavior, secrets handling) is documented in
[Deployment](deployment.md#external-search-backends-searxng-and-camofox).
