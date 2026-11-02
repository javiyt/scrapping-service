# Search Providers

The search API is provider-agnostic. `POST /v1/search` and
`GET /v1/search/capabilities` speak service-level concepts (verticals,
filters, providers); concrete backends live behind the
[`SearchProvider` contract](https://github.com/javiyt/scrapping-service/blob/main/app/search/contract.py).

```text
Request (SearchRequest)
  |  validation (extra="forbid" — provider-specific names are rejected)
  v
SearchCache (result cache: TTL, stale-if-error, shared by all providers)
  |  miss / force_refresh
  v
SearchRouter (capability-aware: probe, select, fall back on transient errors)
  |
  v
SearchProvider (SearXNG today; more later) -> normalised SearchResponse
  |
  v
SearchApiResponse (stable envelope — raw provider payloads never appear)
```

Observability rides along automatically: every request increments the search
counters in `/metrics` (requests, success, error, cache hits/misses/stale,
latency) and the per-provider `search_provider_*_total` labelled counters,
and `GET /health/deep` reports the subsystem (providers, availability, SearXNG
`/config` discovery state) without ever failing on a down backend.

## SearXNG

SearXNG is the first backend and is **disabled by default**. It queries a
SearXNG instance's `/config` and `/search` (JSON) endpoints, maps its
categories/engines to the normalized vocabulary, and caches the discovery.

### Run a SearXNG instance

The instance must allow JSON output. In its `settings.yml`:

```yaml
search:
  formats:
    - html
    - json
```

Everything else (enabled engines/categories) is discovered at runtime —
no per-engine configuration is needed on this service.

### Configure the provider

```yaml
# configs/config.yaml
search:
  default_provider: auto      # or an explicit provider name (no fallback)
  provider_order: [searxng]   # routing priority for auto mode
  probe_timeout_seconds: 10
  providers:
    searxng:
      enabled: true
      base_url: http://searxng:8080
      timeout_seconds: 15
      default_language: en        # used when a request has no language
      config_cache_ttl_seconds: 3600
```

Environment overrides (take priority over YAML):

| Variable | Purpose |
| --- | --- |
| `SCRAPER_SEARCH_SEARXNG_ENABLED` | Enable the provider |
| `SCRAPER_SEARCH_SEARXNG_BASE_URL` | Base URL of the instance |
| `SCRAPER_SEARCH_SEARXNG_TIMEOUT_SECONDS` | HTTP timeout (1-300 s) |
| `SCRAPER_SEARCH_SEARXNG_DEFAULT_LANGUAGE` | Default BCP-47 language tag |
| `SCRAPER_SEARCH_SEARXNG_CONFIG_CACHE_TTL_SECONDS` | `/config` discovery cache TTL (`0` = re-discover on every use; a stale discovery is served when a refresh fails) |

The search result cache policy (shared by all providers) is configured in the
same block: `search.cache.enabled`, `default_ttl_seconds` (`0` = never
expires), and `stale_if_error` (serve an expired entry when the live search
fails). Overrides: `SCRAPER_SEARCH_CACHE_*`.

### SearXNG behaviour

- **Discovery** — `/config` yields the instance's enabled categories and
  engines; the provider reports those as its verticals/sources and re-checks
  on a TTL. An unreachable instance yields a degraded
  (`available: false`) capabilities report, never a crash — the router skips
  it and the deep health check reports `discovery_state: not_discovered`.
- **Mapping** — SearXNG names live only in
  [`app/search/providers/searxng/mapping.py`](https://github.com/javiyt/scrapping-service/blob/main/app/search/providers/searxng/mapping.py):
  category → vertical, engine → source, numeric safesearch level,
  `time_range`/`pageno` parameter names. A SearXNG-specific field can never
  leak into the public API, and a contract test pins that.
- **Degradations** — parts of a request the instance cannot honor (e.g. a
  disabled category) surface as `metadata.degraded_capabilities` tokens plus
  human-readable `metadata.warnings`.

## Adding a new provider

1. **Create a package** — `app/search/providers/<name>/` with a
   `config.py` (a pydantic model, `extra="forbid"`, a `from_settings()`
   following the `SCRAPER_<SECTION>_*` env-var convention) and a
   `provider.py` implementing the contract:
   - `name` — stable, unique identifier (it becomes a label in metrics);
   - `async capabilities()` — current capabilities, including
     `available`; probe with a bounded timeout when a health check is needed;
   - `async search(request)` — map the backend into
     `SearchResponse`/`SearchResult`; set `metadata.provider` to your
     `name`; raise search errors from `app.core.errors` on failure (unavailable
     / timeout — the router falls back on transient ones); never expose raw
     backend dictionaries.
   - Optionally `diagnostics()` — a zero-I/O dict merged into the
     `/health/deep` provider report (SearXNG reports its `/config` discovery
     state this way).
2. **Register it** — add construction to `build_search_providers()` in
   `app/search/providers/__init__.py` (enabled-flag gated, like SearXNG).
   The registry and router pick it up automatically: routing priority comes
   from `search.provider_order`, capabilities from `capabilities()`,
   metrics from the `provider` label, and health from the probe.
3. **Document it** — the `search` block in `configs/config.example.yaml`,
   the env vars in `.env.example`, and a section here.
4. **Test it** — follow `tests/test_searxng_provider.py`: build the provider
   against a fake transport (e.g. `httpx.MockTransport`), assert the request
   mapping, the normalized response, failure mapping, and — critically — that
   nothing backend-specific reaches the public response.

No route, schema, cache, or metrics changes are needed: the service observes
new providers only through the contract.
