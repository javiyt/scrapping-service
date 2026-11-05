# Architecture

This document describes the internal architecture of the Scraper API service:
how the components are organised, how data flows through them, and how the
various feature areas (scrape, search, transcript, interactive browser, MCP)
fit together.

## Component Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        FastAPI application (app/main.py)               │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────┐               │
│  │  Scrape API  │  │  Search API  │  │ Transcript API │  …routes      │
│  │  (routes.py) │  │ (search/     │  │ (transcript/   │               │
│  │              │  │  router.py)  │  │  router.py)    │               │
│  └──────┬───────┘  └──────┬───────┘  └───────┬────────┘               │
│         │                  │                   │                        │
│  ┌──────▼───────┐  ┌──────▼───────┐  ┌────────▼────────┐             │
│  │ Scraper      │  │ SearchCache  │  │ TranscriptCache │             │
│  │ Service      │  │ (search/     │  │ (transcript/    │             │
│  │              │  │  cache.py)   │  │  cache.py)      │             │
│  └──┬───────┬───┘  └──────┬───────┘  └────────┬────────┘             │
│     │       │             │                    │                      │
│  ┌──▼──┐ ┌──▼────────┐ ┌──▼───────────┐ ┌─────▼──────────┐          │
│  │HTTP │ │ Browser   │ │ SearchRouter │ │ Transcript     │          │
│  │Fetch│ │ Backends  │ │ (fallback    │ │ Router         │          │
│  │     │ │ (Camofox, │ │ chain)       │ │ (fallback      │          │
│  │     │ │ Botasaurus│ │              │ │ chain)         │          │
│  └─────┘ └───────────┘ └──────────────┘ └────────────────┘          │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  Shared infrastructure                                          │  │
│  │  • SQLite cache (app/cache/sqlite_cache.py)                     │  │
│  │  • Auth / profile resolver (app/auth/)                          │  │
│  │  • Metrics collector (app/metrics/prometheus.py)                │  │
│  │  • Agent browser service (app/browser/service.py)               │  │
│  │  • Job worker pool (app/jobs/)                                  │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

## Request Flow

### Scrape (`POST /v1/scrape`)

```
Client → Auth → ScraperService.scrape()
                         │
                         ├── 1. Normalise URL (SSRF guard)
                         ├── 2. Check cache (fresh hit → serve immediately)
                         ├── 3. HTTP fetch (http_fetcher.py)
                         │       └── fail? → 4.
                         ├── 4. Browser backends (BrowserBackendOrchestrator)
                         │       └── Botasaurus → Camofox (ordered fallback)
                         │       └── all fail? → return HTTP error (502/503)
                         ├── 5. Normalise HTML + extract (selector)
                         ├── 6. Store in cache (TTL per domain policy)
                         └── 7. Return response (from_cache, stale, metadata)
```

**Cache semantics** (stale-if-error):
- Fresh entry → served with `from_cache=true, stale=false`.
- Expired entry + live fetch succeeds → fresh result stored and served.
- Expired entry + live fetch fails with a *retryable* error → stale entry
  served with `from_cache=true, stale=true` and a warning.
- Expired entry + *definitive* error (404, 403, invalid URL) → error
  propagates; stale data never masks a real failure.

### Search (`POST /v1/search`)

```
Client → Auth → SearchCache.fetch()
                    │
                    ├── 1. Validity gate (invalid → ValidationError, no cache)
                    ├── 2. Cache check (fresh hit → serve)
                    ├── 3. SearchRouter.route()
                    │       └── ordered provider chain (SearXNG → Camofox → …)
                    │       └── capability-aware routing (time_range, domain)
                    ├── 4. Success → normalise → store → serve
                    └── 5. Failure → stale-if-error or propagate error
```

### Transcript (`GET /v1/transcript`)

```
Client → Auth → TranscriptCache.fetch()
                    │
                    ├── 1. Validity gate (invalid → ValidationError, no cache)
                    ├── 2. Cache check (fresh hit → serve)
                    ├── 3. TranscriptRouter.route()
                    │       └── ordered provider fallback chain
                    │       └── retryable failures → next provider
                    ├── 4. Success → normalise → store → serve
                    └── 5. Failure → stale-if-error or propagate error
```

### Interactive Browser (`/v1/browser/*`)

```
Client → Auth → AgentBrowserService
                    │
                    ├── Session lifecycle (create → TTL expiry → reclaim)
                    ├── Per-session tab management (max_tabs_per_session)
                    ├── Profile-aware security (SSRF guard, URL allowlist)
                    └── Provider adapter (Camofox; more to come)
```

### MCP (Model Context Protocol)

The MCP adapter (tickets #149–#155, `app/mcp`) exposes the unified web-access
capabilities as MCP tools. It is a **thin pass-through proxy**: every tool
is a plain REST call to the service, so the service's security policies
(SSRF guard, domain rules, auth, caching) apply unchanged and the adapter
never re-implements scraping logic. Transports are stdio (what MCP hosts
launch) and streamable HTTP for remote clients.

| MCP tool | Maps to | Status |
|----------|---------|--------|
| `adapter_info` | `GET /health` | Implemented (wiring check) |
| `search` / `search_capabilities` | `POST /v1/search` / `GET /v1/search/capabilities` | Implemented (provider chain) |
| `fetch_url` / `fetch_url_batch` | `POST /v1/scrape` | Implemented (HTTP + browser fallback) |
| `transcript` | `GET /v1/transcript` | Implemented (provider chain) |
| `browser_*` (capabilities, session, navigate, click, type, evaluate, close) | `/v1/browser/*` | Implemented (Camofox) |
| `cache_*` (purge, delete, cleanup, vacuum, stats) | `/v1/cache/*` | Implemented |

The adapter's test suites (`tests/test_mcp_*.py`) cover the contract
(schema snapshot), security (no key leakage, error isolation, output
bounding) and the full MCP protocol flow over in-memory streams; an opt-in
live integration test (`SCRAPER_TEST_MCP_LIVE=true`) drives the adapter
against a real deployment. Full documentation: [MCP Adapter](mcp.md).

## Module Map

| Module | Path | Responsibility |
|--------|------|---------------|
| App entry | `app/main.py` | FastAPI app factory, lifespan, route registration |
| HTTP routes | `app/api/routes.py` | Scrape + search + transcript HTTP endpoints |
| Browser routes | `app/api/browser_routes.py` | Interactive browser HTTP endpoints |
| Dependencies | `app/api/dependencies.py` | DI: settings, auth, service singletons |
| Auth | `app/auth/` | API key verification, profile resolution |
| Settings | `app/core/config.py` | Pydantic `BaseSettings` (env + YAML) |
| Errors | `app/core/errors.py` | Typed exception hierarchy + status codes |
| Security | `app/core/security.py` | URL validation, SSRF guard |
| SQLite cache | `app/cache/sqlite_cache.py` | Persistent cache storage |
| Cache models | `app/cache/models.py` | `CacheEntry`, `FetchResult` dataclasses |
| Scraper service | `app/scraper/service.py` | Orchestration: fetch → normalise → cache |
| HTTP fetcher | `app/scraper/http_fetcher.py` | httpx client with retry |
| Browser backends | `app/scraper/browser_backends/` | Camofox, Botasaurus adapters |
| Browser orchestrator | `app/scraper/browser_backends/orchestrator.py` | Ordered fallback + quality gate |
| Domain policy | `app/scraper/domain_policy.py` | Per-domain config (rate, TTL, mode) |
| HTML normaliser | `app/scraper/html_normalizer.py` | Cleanup + selector extraction |
| Search router | `app/search/router.py` | Provider fallback + capability routing |
| Search cache | `app/search/cache.py` | Result cache with stale-if-error |
| Search providers | `app/search/providers/` | SearXNG, Camofox adapters |
| Transcript router | `app/transcript/router.py` | Provider fallback chain |
| Transcript cache | `app/transcript/cache.py` | Result cache with stale-if-error |
| Transcript providers | `app/transcript/providers/` | YouTube (yt-dlp), future adapters |
| Agent browser | `app/browser/service.py` | Session pool + TTL + provider dispatch |
| Browser config | `app/browser/config.py` | `AgentBrowserConfig` (limits, TTL) |
| Camofox client | `app/camofox/` | HTTP client for the Camofox browser service |
| Job service | `app/jobs/` | Async scrape jobs with worker pool |
| Metrics | `app/metrics/prometheus.py` | Prometheus-compatible counters/gauges |

## Configuration

Settings are loaded from environment variables (prefix `SCRAPER_`) and an
optional YAML file (`SCRAPER_CONFIG_PATH`). The `Settings` class in
`app/core/config.py` is the single source of truth; the DI layer in
`app/api/dependencies.py` caches it as a process-wide singleton.

Key groups:
- **Auth**: `api_key`, `api_keys` (multi-key), profile YAML path
- **Scrape**: cache TTL, browser backend ordering, rate limits
- **Search**: provider enablement, SearXNG URLs, cache TTL
- **Transcript**: provider enablement, cache TTL
- **Agent browser**: enable, max sessions, max tabs, session TTL
- **Camofox**: service URL, auth

## Testing Strategy

### Unit tests

Each module has focused unit tests covering its logic in isolation:

| File | Covers |
|------|--------|
| `tests/test_scraper_service.py` | ScraperService orchestration |
| `tests/test_http_fetcher.py` | HTTP fetch + retry |
| `tests/test_scrape_cache.py` | Cache hit/miss/expiry/stale |
| `tests/test_search_router.py` | Search provider fallback |
| `tests/test_search_cache.py` | Search cache + stale-if-error |
| `tests/test_transcript_router.py` | Transcript provider fallback |
| `tests/test_transcript_cache.py` | Transcript cache + stale-if-error |
| `tests/test_agent_browser_service.py` | Browser session pool + limits |
| `tests/test_browser_orchestrator.py` | Browser backend fallback |
| `tests/test_domain_policy.py` | Per-domain config resolution |
| `tests/test_html_normalizer.py` | HTML cleanup + extraction |
| `tests/test_security.py` | SSRF guard, URL validation |

### Integration (E2E) tests

The integration test files exercise the full request path
(TestClient → route → service → provider/cache) with **zero network I/O** —
all external dependencies are replaced by in-memory fakes:

| File | Path covered | Fakes |
|------|-------------|-------|
| `tests/test_integration_search.py` | `POST /v1/search` → SearchCache → SearchRouter → provider | `FakeSearchProvider`, real `SqliteCache` |
| `tests/test_integration_scrape.py` | `POST /v1/scrape` → ScraperService → HTTP/browser backends → cache | `FakeHttpFetcher`, `FakeBrowserBackend`, real `SqliteCache` |
| `tests/test_integration_agent_browser.py` | `/v1/browser/*` → AgentBrowserService → provider | `FakeAgentBrowserProvider` |
| `tests/test_integration_transcript.py` | `GET /v1/transcript` → TranscriptCache → TranscriptRouter → provider | `FakeTranscriptProvider`, real `SqliteCache` |
| `tests/test_integration_mcp.py` | Pins the service contract each MCP tool proxies (search, scrape, transcript, browser) | `FakeAgentBrowserProvider` |

Shared fakes live in `tests/fakes.py` and are used by both unit and
integration tests.

## Key Design Decisions

1. **Stale-if-error, not stale-forever.** Expired cache entries are served
   only when the live fetch fails with a *retryable* error. Definitive
   failures (404, auth errors, invalid input) always propagate to the
   client so the caller can react.

2. **Provider-agnostic routers.** Both search and transcript use an ordered
   fallback chain. Adding a new provider requires only a new adapter class
   and a registry entry; the router and cache are unchanged.

3. **Single cache store, multiple key spaces.** The SQLite cache (`cache`
   table for scrape, `search_cache`, `transcript_cache`) is one file. Key
   material includes a version number so schema migrations are safe.

4. **Browser backends are composable.** The `BrowserBackendOrchestrator`
   tries backends in order (default: Botasaurus → Camofox) and applies a
   quality gate (minimum HTML length, block-signal detection) before
   accepting a result.

5. **Profile-aware auth.** API keys can map to profiles with per-profile
   overrides (rate limits, allowed domains, browser policy). The resolver
   merges profile settings onto the global defaults per request.

6. **Deterministic tests, zero network.** All integration tests use fakes
   that simulate provider behaviour in-memory. The test suite runs
   offline and in under 20 seconds.
