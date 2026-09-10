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
