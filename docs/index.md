# Scraper API

Scraper API is a containerized FastAPI service for fetching rendered HTML with
cache control, selector extraction, JavaScript rendering, rate limiting, and
SSRF protection.

It is designed to run as an internal scraping service for other applications,
including bots deployed on a Raspberry Pi through Podman and Quadlet.

## Main Capabilities

- HTTP and browser-backed scraping modes.
- Automatic fallback from HTTP to browser rendering.
- SQLite-backed persistent HTML cache.
- Profile-aware API key authentication.
- Domain policies for rate limits, concurrency, TTL, and mode defaults.
- HTML normalization and CSS selector extraction.
- Async scrape jobs with polling and cancellation.
- Prometheus-compatible metrics.
- Optional proxy, custom header, and per-request cookie support.

## Architecture

```text
Client
  |
  v
FastAPI app
  |
  v
ScraperService
  |--------- HttpFetcher
  |--------- BrowserFetcher
  |--------- SQLite cache
  |--------- Job service
```

## Source Documents

- [README](https://github.com/javiyt/scrapping-service/blob/main/README.md)
- [Development guide](https://github.com/javiyt/scrapping-service/blob/main/DEVELOPMENT.md)
- [OpenAPI specification](api.md)
