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
SearchProvider (SearXNG / Camofox / 4get today; more later) -> normalised SearchResponse
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

## 4get

4get is a privacy-oriented metasearch frontend that scrapes several engines
(DuckDuckGo, Brave, Google, Yandex, Marginalia, ...) and exposes them
through a JSON API. It is **disabled by default** and must be pointed at a
**self-hosted** instance:

- 4get's API terms forbid intensive scraping, SEO scanning and non-human
  automated traffic, and ask clients to cache responses.
- **Self-host** an instance (https://git.lolcat.ca/lolcat/4get) and set
  `base_url` to it. Do **not** point production traffic at 4get.ca.
- Rely on the generic search result cache (`search.cache.*`, shared by all
  providers). A generous TTL (e.g. `default_ttl_seconds: 900` or higher)
  is recommended to keep request volume low on quota-metered instances.

Public instances may require a **pass cookie** (captcha-derived, roughly
100 requests / 24h). Configure it as `pass_token`; the provider sends it as
the `pass` cookie on every request. The token is a secret: excluded from
serialization, masked in `repr()`, and never present in logs, errors, or
`/health/deep` output. An HTTP 429 (missing/invalid pass) raises a clear,
**non-retryable** configuration error (`no_provider_available`) telling the
operator to fix `SCRAPER_SEARCH_4GET_PASS_TOKEN` — the router never falls
back on it.

### Configure the provider

```yaml
# configs/config.yaml
search:
  provider_order: [searxng, camofox, 4get]   # routing priority for auto mode
  providers:
    4get:
      enabled: true
      base_url: http://4get:8080        # self-hosted instance
      timeout_seconds: 15
      pass_token: ${SCRAPER_4GET_PASS}  # secret, never a literal
      scraper: duckduckgo               # optional default engine
      default_language: en              # sent as `lang` when a request has none
      default_region: us                # sent as `country`
      default_nsfw: false               # nsfw=1 at the moderate safe-search level
      health_cache_ttl_seconds: 300     # /ami4get probe cache TTL
```

Environment overrides (take priority over YAML): `SCRAPER_SEARCH_4GET_ENABLED`,
`SCRAPER_SEARCH_4GET_BASE_URL`, `SCRAPER_SEARCH_4GET_TIMEOUT_SECONDS`,
`SCRAPER_SEARCH_4GET_PASS_TOKEN`, `SCRAPER_SEARCH_4GET_SCRAPER`,
`SCRAPER_SEARCH_4GET_DEFAULT_LANGUAGE`, `SCRAPER_SEARCH_4GET_DEFAULT_REGION`,
`SCRAPER_SEARCH_4GET_DEFAULT_NSFW`, `SCRAPER_SEARCH_4GET_HEALTH_CACHE_TTL_SECONDS`.

### 4get behaviour

- **Health** — `capabilities()` probes `/ami4get` (instance info) and caches
  the result for `health_cache_ttl_seconds`; no search endpoint is ever used
  for health. A failed probe reports `available: false`, never raises.
- **Endpoints** — verticals map 1:1: `web` → `/api/v1/web`, `news` →
  `/api/v1/news`, `images` → `/api/v1/images`, `videos` → `/api/v1/videos`,
  `music` → `/api/v1/music`. One search performs exactly one 4get request
  (the first supported vertical); other requested verticals are reported as
  `metadata.degraded_capabilities`. The other domain verticals
  (social, it, science, maps, files) have no 4get endpoint and are not
  advertised.
- **Parameters relied on** (instance-defined; craft requests in the 4get
  web UI and swap `/web` → `/api/v1/web` to verify yours): `s` (query),
  `scraper` (engine — from the request's `sources` or the configured
  default), `lang` (request language or configured default), `country`
  (configured default region), `nsfw` (safe-search: `0` = safe,
  `1` = allow; `off` always sends `1`, `strict` always sends `0`,
  `moderate` follows `default_nsfw`). `extendedsearch` (ddg-only) and the
  instance date filters are deliberately not used.
- **Paging (sequential-only)** — 4get paginates with opaque, single-use
  `npt` tokens that expire after ~15 minutes. The provider serves **page 1
  only**: it never sends `npt` and never exposes a token to clients, so
  paging is not advertised (`supports_paging: false`) and the router never
  routes page > 1 requests to 4get. A direct call with `page > 1` degrades
  to page 1 with a `paging` token and warning.
- **Normalization** — `title`, `url`, `snippet`, `engine` → `source`,
  `image` → `thumbnail_url`, `date` (epoch seconds) → `published_at` (UTC);
  music items fall back to `artist — album` for the snippet. The `time`
  field (video duration, seconds) is intentionally not mapped: the domain
  `SearchResult` has no duration field. Rich web extras (`answer`,
  `spelling`, `related`, `table`) are ignored. 4get-specific names live only
  in `app/search/providers/fourget/mapping.py`; nothing backend-specific
  reaches the public response.
- **Errors** — connection/5xx → `search_provider_unavailable`, timeout →
  `search_provider_timeout`, `status != "ok"` / invalid JSON →
  `invalid_provider_response`, HTTP 429 → `no_provider_available` (clear,
  non-retryable pass-configuration error). No upstream body, instance URL,
  or pass token ever appears in a raised error.

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
