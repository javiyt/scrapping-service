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
- Provider-agnostic web search (`POST /v1/search`) with capability-aware
  routing, fallback, a normalized result cache, and stale-if-error —
  see [Search Providers](search-providers.md).
- YouTube transcripts (`GET /v1/transcript`) with an ordered provider
  fallback chain, a normalized result cache, and stale-if-error —
  see [Transcripts](transcripts.md).
- Prometheus-compatible metrics, including per-provider search and
  transcript counters.
- Optional proxy, custom header, and per-request cookie support.

## Architecture

```text
Client
  |
  v
FastAPI app
  |
  v
ScraperService                  Search API                  Transcript API
  |--------- HttpFetcher         |--------- SearchRouter           |--------- TranscriptRouter (fallback chain)
  |--------- BrowserFetcher      |--------- Search result cache      |--------- Transcript result cache (SQLite)
  |--------- SQLite cache        |--------- SearXNG / Camofox providers |--------- Camofox provider (more to come)
  |--------- Job service
  |
  v
/health, /health/deep, /ready, /metrics, /docs
```

## Source Documents

- [README](https://github.com/javiyt/scrapping-service/blob/main/README.md)
- [Development guide](https://github.com/javiyt/scrapping-service/blob/main/DEVELOPMENT.md)
- [OpenAPI specification](api.md)
