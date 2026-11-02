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
