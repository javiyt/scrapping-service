# Scraper API

Containerized scraping microservice — fetch rendered HTML from any URL with
configurable caching, JavaScript rendering, rate limiting, and SSRF protection.

Suitable for use as an internal scraping service from other applications,
especially Go bots running on a Raspberry Pi via Podman + Quadlet.

---

## Features

- **HTML normalisation** — per-request clean-up: remove scripts, styles,
  comments, meta tags; convert relative links to absolute; collapse whitespace;
  or minify output.
- **Selector extraction** — extract structured data (text, HTML, attributes,
  objects) from scraped HTML using CSS selectors.
- **Async jobs** — submit long-running scrapes as background jobs with
  status polling, cancellation, and controlled concurrency.
- **Dual fetch modes** — simple HTTP fetch and browser-based (JavaScript)
  rendering via Botasaurus + Chromium.
- **Auto mode** — tries HTTP first, falls back to browser if the response looks
  blocked or empty.
- **Persistent cache** — SQLite-backed HTML cache with configurable TTL per
  request, per domain, and globally.
- **SSRF protection** — blocks localhost, private IPs, Docker internal hosts,
  cloud metadata endpoints, and resolves hostnames to verify they don't point
  to private networks.
- **API key authentication** — Bearer token required on all endpoints except
  `/health`.
- **Domain policies** — per-domain rate limiting, concurrency control, and
  default TTL / mode overrides.
- **Prometheus metrics** — lightweight built-in metrics endpoint.
- **Debug features** — optional HTML dumps and screenshots.
- **Containerised** — multi-arch Docker image (`linux/amd64`, `linux/arm64`).
- **Quadlet deploy** — first-class Podman Quadlet support for Raspberry Pi.
- **Full API** — scrape, batch scrape, cache management, health checks.
- **Optional proxy support** — global or per-request proxy (HTTP, HTTPS, SOCKS5)
  with credential redaction, private-host blocking, and override controls.

---

## Architecture

```text
┌──────────┐     ┌──────────────┐     ┌─────────────────┐
│  Client   │────▶│  FastAPI      │────▶│  ScraperService  │
│ (Go bot)   │     │  (uvicorn)    │     │                 │
└──────────┘     └──────────────┘     └───┬─────────────┘
                                          │
               ┌──────────────────────────┼──────────────┐
               ▼                          ▼              ▼
        ┌───────────┐            ┌──────────────┐ ┌──────────┐
        │ HttpFetcher│            │ BrowserFetcher│ │ SQLite   │
        │ (httpx)    │            │ (Botasaurus)   │ │ Cache    │
        └───────────┘            └──────────────┘ └──────────┘
```

The service is organised into these modules:

| Module     | Responsibility                                                                            |
|------------|-------------------------------------------------------------------------------------------|
| `api/`     | FastAPI routes, request/response schemas                                                  |
| `core/`    | Config, error types, logging, URL security                                                |
| `cache/`   | SQLite cache backend                                                                      |
| `jobs/`    | In-memory async job processing with background workers and retention policy               |
| `scraper/` | Fetch orchestration, HTTP & browser fetchers, HTML normalisation, CSS selector extraction |
| `metrics/` | Prometheus-style metrics collector                                                        |

---

## Quick start

### Local development

```bash
# Clone
git clone https://github.com/your-org/scrapping-service.git
cd scrapping-service

# Environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Copy configuration
cp configs/config.example.yaml configs/config.yaml
cp .env.example .env
# Edit .env — set SCRAPER_API_KEY to a strong secret

# Run (port defaults to 8080; set SCRAPER_SERVER_PORT env var or
# change server.port in configs/config.yaml to override)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

### Run tests

```bash
pytest tests/ -v --tb=short
ruff check .
ruff format --check .
```

### Docker

```bash
# Build
docker build -t scraper-api:latest .

# Run (port defaults to 8080; set -e SCRAPER_SERVER_PORT=9090 and
# adjust -p mapping to use a different port)
docker run -d \
  --name scraper-api \
  -p 8080:8080 \
  -v $(pwd)/configs/config.yaml:/config/config.yaml:ro \
  -v $(pwd)/data:/data \
  -v $(pwd)/.env:/.env:ro \
  scraper-api:latest

# Or use docker compose
```

---

## API Reference

### `GET /health`

Liveness probe — always returns `200` when the service is running.

```json
{ "status": "ok", "version": "1.0.0", "service": "scraper-api" }
```

### `GET /ready`

Readiness probe — verifies config, cache, and internal state.

### `GET /metrics`

Prometheus-style metrics:

```text
# HELP scrape_requests_total Total scrape requests received
# TYPE scrape_requests_total counter
scrape_requests_total 42
...
```

### `POST /v1/scrape`

Scrape a single URL.

Request:

```json
{
  "url": "https://example.com",
  "mode": "auto",
  "cache_ttl_seconds": 21600,
  "force_refresh": false,
  "wait_until": "networkidle",
  "wait_selector": null,
  "timeout_seconds": 45,
  "scroll": {
    "enabled": false,
    "max_scrolls": 5,
    "delay_ms": 1000,
    "stop_when_no_growth": true
  },
  "debug": {
    "screenshot": false,
    "html_dump": false
  },
  "normalize": {
    "enabled": false,
    "absolute_urls": false,
    "remove_scripts": false,
    "remove_styles": false,
    "remove_comments": false,
    "remove_meta": false,
    "remove_noscript": false,
    "collapse_whitespace": false,
    "minify": false
  }
}
```

Per-request proxy example (requires ``proxy.allow_request_override: true``):

```json
{
  "url": "https://example.com",
  "proxy": {
    "enabled": true,
    "url": "http://user:pass@residential-proxy.example:8080",
    "country": "ES"
  }
}
```

Response:

```json
{
  "url": "https://example.com",
  "final_url": "https://example.com",
  "status_code": 200,
  "from_cache": false,
  "stale": false,
  "fetched_at": "2026-06-30T10:30:00+02:00",
  "expires_at": "2026-06-30T16:30:00+02:00",
  "html": "<html>...</html>",
  "metadata": {
    "mode": "browser",
    "elapsed_ms": 4820,
    "content_length": 834122,
    "cache_key": "abc123...",
    "normalized": false,
    "normalization": null
  }
}
```

### `POST /v1/scrape/batch`

Scrape multiple URLs with controlled concurrency.

```json
{
  "items": [
    {
      "url": "https://example.com/1",
      "normalize": {
        "enabled": true,
        "remove_scripts": true,
        "absolute_urls": true
      }
    },
    { "url": "https://example.com/2" }
  ],
  "max_concurrency": 3
}
```

### Cache management

| Method   | Endpoint            | Description                        |
|----------|---------------------|------------------------------------|
| `GET`    | `/v1/cache/stats`   | Entry count, total size, expired   |
| `DELETE` | `/v1/cache?url=...` | Remove one URL from cache          |
| `POST`   | `/v1/cache/purge`   | Clear all (or `?domain=...`) cache |

---

## Configuration

Configuration is loaded from a YAML file (`configs/config.yaml` by default) with
environment variable overrides on top.

### Environment variables

| Variable                            | Default                  | Description                 |
|-------------------------------------|--------------------------|-----------------------------|
| `SCRAPER_API_KEY`                   | `change-me`              | API key for auth            |
| `SCRAPER_SERVER_HOST`               | `0.0.0.0`                | Bind address                |
| `SCRAPER_SERVER_PORT`               | `8080`                   | HTTP port                   |
| `SCRAPER_CACHE_SQLITE_PATH`         | `/data/scraper-cache.db` | Cache database path         |
| `SCRAPER_CACHE_DEFAULT_TTL_SECONDS` | `21600`                  | Default cache TTL (6 hours) |
| `SCRAPER_LOG_LEVEL`                 | `INFO`                   | Log level                   |
| `CONFIG_PATH`                       | —                        | Path to YAML config file    |

### YAML config

See [`configs/config.example.yaml`](configs/config.example.yaml) for all options
with documentation.

### Proxy

A global proxy can be configured in YAML or via environment variables.
Per-request proxy overrides require ``proxy.allow_request_override: true``.

```yaml
proxy:
  enabled: false
  url: null                               # http://user:pass@proxy.example:8080
  country: null                            # Optional country hint for provider
  allow_request_override: false
  block_private_proxy_hosts: true
```

| Variable                               | Default  | Description                              |
|----------------------------------------|----------|------------------------------------------|
| ``SCRAPER_PROXY_ENABLED``              | ``false`` | Enable the global proxy                  |
| ``SCRAPER_PROXY_URL``                  | —        | Proxy URL (http, https, socks5)          |
| ``SCRAPER_PROXY_COUNTRY``              | —        | Optional country hint                    |
| ``SCRAPER_PROXY_ALLOW_REQUEST_OVERRIDE`` | ``false`` | Allow per-request proxy overrides      |
| ``SCRAPER_PROXY_BLOCK_PRIVATE_PROXY_HOSTS`` | ``true`` | Block proxy hosts on private IPs   |

### Configuration priority

1. **Environment variables** (highest priority)
2. **YAML config file**
3. **Hard-coded defaults**

---

## Cache behaviour

- Cache is stored in SQLite at `CACHE_SQLITE_PATH` (`/data/scraper-cache.db`).
- Each entry has a TTL; expired entries are not returned unless
  `cache.stale_if_error` is `true` and the live fetch fails.
- Per-request `cache_ttl_seconds` overrides domain-level TTL, which overrides
  the global `default_ttl_seconds`.
- `force_refresh: true` bypasses the cache entirely (the fresh result is still
  cached afterwards).
- Cache size is capped by `cache_max_html_size_mb` (default 10 MB); old entries
  are evicted when the limit is exceeded.
- The cache persists on a Docker volume — it survives container restarts.

## Cache maintenance

Without periodic cleanup the SQLite cache database grows forever — expired
rows still occupy space on disk, and the row count accumulates indefinitely.
This is especially problematic on resource-constrained devices such as a
Raspberry Pi with limited storage.

### Cache Configuration

Add the following under the ``cache`` section of ``config.yaml``:

```yaml
cache:
  # ... existing settings ...

  # Cache maintenance
  cleanup_enabled: true               # Enable automatic background cleanup
  cleanup_interval_seconds: 3600      # Run cleanup every hour
  delete_expired_after_seconds: 86400 # Delete entries expired >24h ago
  max_entries: 10000                  # Max cache rows before eviction
  max_size_mb: 512                    # Approximate max DB size
  vacuum_after_cleanup: false         # Skip VACUUM by default (can block writes)
```

All values can be overridden via environment variables:

| Variable                                              | Default |
|-------------------------------------------------------|---------|
| ``SCRAPER_CACHE_CLEANUP_ENABLED``                     | `true`  |
| ``SCRAPER_CACHE_CLEANUP_INTERVAL_SECONDS``            | `3600`  |
| ``SCRAPER_CACHE_DELETE_EXPIRED_AFTER_SECONDS``        | `86400` |
| ``SCRAPER_CACHE_MAX_ENTRIES``                         | `10000` |
| ``SCRAPER_CACHE_MAX_SIZE_MB``                         | `512`   |
| ``SCRAPER_CACHE_VACUUM_AFTER_CLEANUP``                | `false` |

### How cleanup works

The automatic background cleanup runs every ``cleanup_interval_seconds``
when ``cleanup_enabled`` is ``true``. Each cycle performs these phases:

1. **Expired entry cleanup** — deletes entries whose ``expires_at`` is older
   than ``delete_expired_after_seconds`` ago. This grace period prevents
   eagerly deleting entries that just expired.
2. **Max entries** — if the total row count exceeds ``max_entries``, the
   oldest entries (by ``fetched_at``) are deleted.
3. **Max size** — if the total content size exceeds ``max_size_mb``, entries
   are deleted in batches of 100 until the size is below the limit.
4. **VACUUM** — if ``vacuum_after_cleanup`` is ``true``, SQLite VACUUM is
   run to reclaim disk space. Off by default because VACUUM can block writes
   and is I/O intensive on SD cards.

The cleanup loop is safe to run concurrently with regular cache operations.
Exceptions are caught and logged without crashing the application.

### Manual cleanup

Run cache cleanup immediately with default or overridden parameters:

```http
POST /v1/cache/cleanup
Authorization: Bearer <API_KEY>
```

Optional request body (all fields are optional — omitted fields fall back to
config defaults):

```json
{
  "delete_expired_after_seconds": 86400,
  "max_entries": 10000,
  "max_size_mb": 512,
  "vacuum": false
}
```

Response:

```json
{
  "deleted_expired": 42,
  "deleted_by_max_entries": 10,
  "deleted_by_max_size": 0,
  "total_deleted": 52,
  "size_before_bytes": 26214400,
  "size_after_bytes": 26214400,
  "entries_before": 10042,
  "entries_after": 10000,
  "vacuumed": false
}
```

### Manual VACUUM

Run SQLite VACUUM to reclaim disk space:

```http
POST /v1/cache/vacuum
Authorization: Bearer <API_KEY>
```

Response:

```json
{
  "vacuumed": true,
  "size_before_bytes": 26214400,
  "size_after_bytes": 15728640
}
```

### Raspberry Pi recommendations

- Keep ``vacuum_after_cleanup: false`` — VACUUM rewrites the entire database
  file, which can block writes for seconds to minutes on an SD card.
- Run manual VACUUM during maintenance windows if you need to reclaim space,
  or accept that the DB file does not shrink after cleanup.
- Use ``max_entries`` and ``max_size_mb`` to cap cache growth — these prevent
  unbounded growth without requiring VACUUM.
- On a Pi 3B+ with an SD card, a 10 000-entry cleanup typically completes in
  under a second. VACUUM of a 500 MB database may take 10–30 seconds.

### SQLite file size note

When entries are deleted from SQLite, the database file does not shrink
immediately — the freed pages are marked as reusable. Only ``VACUUM``
actually reclaims the disk space. This means:

- ``size_before_bytes`` and ``size_after_bytes`` in the cleanup result may be
  identical even after many entries are deleted.
- The ``max_size_mb`` check uses ``SUM(content_length)`` as a proxy for
  database size since the actual file size only decreases after VACUUM.

---

## Security

### SSRF Protection

The service implements defence-in-depth against Server-Side Request Forgery:

- **Scheme check**: only `http://` and `https://` allowed.
- **Hostname blocklist**: `localhost`, `127.0.0.1`, `::1`, `host.docker.internal`.
- **DNS resolution**: hostnames are resolved and the resulting IPs are checked
  against private, loopback, link-local, and multicast ranges.
- **Cloud metadata**: `169.254.169.254`, `metadata.google.internal`, etc. are
  blocked.
- **Allowed domains**: optionally restrict scraping to an explicit domain list.

### Proxy credentials

Proxy credentials are handled with care:

- **Never logged**: proxy URLs containing credentials are automatically redacted
  before they appear in logs or error messages.
- **Never returned**: redacted proxy URLs are used in all logging and error
  reporting — ``http://***:***@proxy.example:8080``.
- **Private host blocking**: by default, proxy hostnames that resolve to private
  or loopback IP addresses are rejected (configurable via
  ``proxy.block_private_proxy_hosts``).
- **Override control**: per-request proxy overrides are blocked unless
  ``proxy.allow_request_override: true`` is set in the server configuration.

### Authentication

- All endpoints except `/health` require `Authorization: Bearer <SCRAPER_API_KEY>`.
- The API key is validated against the configured `scraper_api_key` value.
- Authentication can be disabled by setting `server.api_key_required: false`.

---

## Domain policies and rate limiting

Per-domain settings in `config.yaml`:

```yaml
domains:
  example.com:
    allowed: true
    min_delay_seconds: 5
    max_concurrent_requests: 1
    default_ttl_seconds: 21600
    default_mode: http
```

The rate limiter enforces both `min_delay_seconds` between requests and
`max_concurrent_requests` — it is an in-process, per-instance limiter (not
shared across replicas).

---

## Scraping modes

### `http`

Simple HTTP GET via `httpx`. No JavaScript execution. Fast and lightweight.

### `browser`

Full browser rendering via Botasaurus (Chromium). Supports:

- JavaScript execution
- `wait_until` strategies (`load`, `domcontentloaded`, `networkidle`)
- CSS selector wait (`wait_selector`)
- Page scrolling to trigger lazy-loaded content
- Global proxy support (see **Proxy** below)

**Browser proxy limitation**: proxy configuration for the browser fetcher is
only supported at **construction time** — the global ``proxy.url`` is used when
the browser driver is initialised.  Per-request proxy overrides are silently
ignored for browser-mode scrapes.  Use HTTP mode for per-request proxy routing.

### `auto` (default)

Tries HTTP first. If the response appears blocked (status 403/429/503, body <
500 chars, or common block-page markers), falls back to browser rendering.

---

## Debug features

Enable via the `debug` field in a scrape request or globally in config:

```yaml
debug:
  screenshots: false
  html_dumps: false
  dir: /debug
```

When enabled:

- **HTML dumps** are written to `debug_dir/html/`.
- **Screenshots** (browser mode only) go to `debug_dir/screenshots/`.

Debug output is off by default — only enable when troubleshooting.

---

## HTML normalisation

Apply on-the-fly clean-up to scraped HTML **without modifying the cache**.
Raw HTML is always stored in the cache as-is; normalisation runs when
building the response for a request that explicitly enables it.

All normalisation fields default to ``false`` — existing clients are
unaffected.

### Request fields

Include a ``normalize`` object in any scrape request:

```json
{
  "url": "https://example.com",
  "normalize": {
    "enabled": true,
    "absolute_urls": true,
    "remove_scripts": true,
    "remove_styles": false,
    "remove_comments": false,
    "remove_meta": false,
    "remove_noscript": false,
    "collapse_whitespace": false,
    "minify": false
  }
}
```

| Field                | Type    | Default | Description                                                |
|----------------------|---------|---------|------------------------------------------------------------|
| ``enabled``          | bool    | false   | Master switch — must be ``true`` for any normalisation.    |
| ``absolute_urls``    | bool    | false   | Converts relative ``href``, ``src``, ``action``, ``poster``|
|                      |         |         | and ``srcset`` to absolute URLs using the final URL as base.|
| ``remove_scripts``   | bool    | false   | Removes all ``<script>`` elements.                         |
| ``remove_styles``    | bool    | false   | Removes ``<style>`` elements and inline ``style`` attrs.   |
| ``remove_comments``  | bool    | false   | Removes HTML comments (``<!-- ... -->``).                  |
| ``remove_meta``      | bool    | false   | Removes all ``<meta>`` elements.                           |
| ``remove_noscript``  | bool    | false   | Removes all ``<noscript>`` elements.                       |
| ``collapse_whitespace`` | bool | false   | Collapses runs of whitespace to single spaces in text.     |
| ``minify``           | bool    | false   | Compacts HTML output without breaking semantics.           |

### Response metadata

When normalisation is active, the response metadata gains two extra fields:

```json
{
  "url": "https://example.com",
  "html": "<a href=\"https://example.com/page\">…</a>",
  "metadata": {
    "mode": "http",
    "elapsed_ms": 230,
    "content_length": 428,
    "cache_key": "abc123...",
    "normalized": true,
    "normalization": {
      "absolute_urls": true,
      "remove_scripts": true
    }
  }
}
```

- ``normalized`` — ``true`` if any active normalisation was applied.
- ``normalization`` — a map of the features that were actually enabled for
  this request.

### Cache behaviour

- **Raw HTML is always cached.** The cache key (SHA-256 of the normalised
  URL) is unchanged, and the cache stores the unmodified HTML returned by
  the fetcher.
- **Normalisation is applied at response time.** Every client that requests
  the same URL can independently choose whether to receive raw or
  normalised HTML — a cache hit serves one entry that is then processed
  per-request.
- Empty normalisation (``enabled: false`` or all features ``false``) is a
  no-op — the original HTML passes through unchanged.

---

## Selector-based extraction

Extract structured data from scraped HTML using CSS selectors — no need to
parse the full HTML on the client side.

Extraction runs **after** HTML normalisation (if enabled), so you can
clean up the markup before extracting.  It operates on whatever HTML the
response currently holds.

### Request fields

Include an ``extract`` object in any scrape request:

```json
{
  "url": "https://example.com/products",
  "extract": {
    "enabled": true,
    "base_url": null,
    "fields": {
      "title": {
        "selector": "title",
        "type": "text"
      },
      "canonical": {
        "selector": "link[rel='canonical']",
        "type": "attr",
        "attribute": "href",
        "absolute_url": true
      },
      "products": {
        "selector": ".product-card",
        "type": "object",
        "multiple": true,
        "fields": {
          "name": {
            "selector": ".product-card-title",
            "type": "text"
          },
          "price": {
            "selector": ".price",
            "type": "text"
          },
          "url": {
            "selector": "a",
            "type": "attr",
            "attribute": "href",
            "absolute_url": true
          }
        }
      }
    }
  }
}
```

| Field            | Type     | Default | Description                                                |
|------------------|----------|---------|------------------------------------------------------------|
| ``enabled``      | bool     | false   | Master switch — must be ``true`` for extraction to run.    |
| ``base_url``     | string\|null | null | Override base URL for relative URL resolution. Falls back to ``final_url``, then the request ``url``. |
| ``fields``       | object   | ``{}``  | Map of field names to field configs (see below).           |

Each **field config** supports these options:

| Option              | Type           | Default  | Description                                       |
|---------------------|---------------|----------|---------------------------------------------------|
| ``selector``        | string        | —        | **Required.** CSS selector to locate the element(s). |
| ``type``            | string        | ``"text"`` | One of ``text``, ``html``, ``attr``, ``object``.  |
| ``attribute``       | string\|null  | null     | Attribute name to read (``attr`` type only).       |
| ``multiple``        | bool          | false    | When ``true``, return a list of all matches.      |
| ``default``         | any           | null     | Fallback value when no element matches.            |
| ``required``        | bool          | false    | When ``true``, a failed match produces a structured error. |
| ``absolute_url``    | bool          | false    | Resolve relative URLs against the page base URL.   |
| ``fields``          | object\|null  | null     | Nested field map (``object`` type only).           |

### Field types

| Type     | Extracted value                                       |
|----------|-------------------------------------------------------|
| ``text`` | Inner text content (whitespace trimmed).              |
| ``html`` | Inner HTML markup (preserves tags).                   |
| ``attr`` | Value of the named HTML attribute.                    |
| ``object`` | Recursively extract sub-fields from the matched element. |

### Response

When extraction is enabled, the response includes an ``extracted`` field:

```json
{
  "url": "https://example.com",
  "html": "<html>...</html>",
  "extracted": {
    "title": "Example Domain",
    "canonical": "https://example.com/",
    "products": [
      {
        "name": "Widget Alpha",
        "price": "$19.99",
        "url": "https://example.com/products/widget-alpha"
      },
      {
        "name": "Widget Beta",
        "price": "$29.99",
        "url": "https://example.com/products/widget-beta"
      }
    ]
  }
}
```

When extraction is **disabled**, the ``extracted`` field is ``null``.

If a ``required`` field cannot be resolved, ``extracted`` is ``null`` and
``extraction_error`` provides details:

```json
{
  "url": "https://example.com",
  "html": "<html>...</html>",
  "extracted": null,
  "extraction_error": {
    "field": "products",
    "message": "No element matched CSS selector '.product-card'"
  }
}
```

### URL resolution order

When ``absolute_url: true``, the base URL for resolution is determined in
this order:

1. ``extract.base_url`` (explicit override)
2. ``final_url`` from the scrape result
3. Original request ``url``

The extractor is available at ``app/scraper/extractor.py`` and is fully
self-contained with no external network access.

---

## Async jobs

Long-running scrape requests can be submitted as **asynchronous jobs** and
polled for completion.  Jobs are processed in-order by a background worker
pool with controlled concurrency.

### Endpoints

| Method   | Endpoint                       | Description                           |
|----------|--------------------------------|---------------------------------------|
| `POST`   | `/v1/jobs`                     | Create a new async scrape job         |
| `GET`    | `/v1/jobs/{job_id}`            | Get job status and result             |
| `GET`    | `/v1/jobs`                     | List all jobs (newest first)          |
| `DELETE` | `/v1/jobs/{job_id}`            | Delete a job from the store           |
| `POST`   | `/v1/jobs/{job_id}/cancel`     | Cancel a queued job                   |

### Creating a job

The request body is identical to ``/v1/scrape``:

```json
{
  "url": "https://example.com",
  "mode": "auto",
  "extract": {
    "enabled": true,
    "fields": {
      "title": { "selector": "title", "type": "text" }
    }
  }
}
```

Response (immediate — job is queued):

```json
{
  "job_id": "job_abc123...",
  "status": "queued",
  "url": "https://example.com",
  "created_at": "2026-07-01T10:00:00+00:00",
  "updated_at": "2026-07-01T10:00:00+00:00",
  "started_at": null,
  "finished_at": null,
  "result": null,
  "error": null
}
```

### Polling for completion

```json
GET /v1/jobs/job_abc123...
{
  "job_id": "job_abc123...",
  "status": "succeeded",
  "url": "https://example.com",
  "created_at": "2026-07-01T10:00:00+00:00",
  "updated_at": "2026-07-01T10:00:05+00:00",
  "started_at": "2026-07-01T10:00:01+00:00",
  "finished_at": "2026-07-01T10:00:03+00:00",
  "result": {
    "url": "https://example.com",
    "status_code": 200,
    "html": "<html>..."
  },
  "error": null
}
```

Possible ``status`` values:

| Status        | Meaning                                 |
|---------------|-----------------------------------------|
| ``queued``    | Waiting for a worker to pick it up      |
| ``running``   | Being processed by a worker             |
| ``succeeded`` | Completed successfully — ``result`` set |
| ``failed``   | An error occurred — ``error`` set       |
| ``cancelled`` | Cancelled before processing started     |

### Cancelling a job

Only ``queued`` jobs can be cancelled.  Running or finished jobs are
unaffected.

```json
POST /v1/jobs/job_abc123.../cancel
{
  "job_id": "job_abc123...",
  "status": "cancelled",
  ...
}
```

### Configuration

Add a ``jobs`` section to ``config.yaml``:

```yaml
jobs:
  enabled: true               # Master switch
  max_retained: 500           # Max number of jobs kept in memory
  max_concurrency: 2          # How many jobs run simultaneously
  result_ttl_seconds: 86400   # How long results are retained
```

Or set environment variables with the ``SCRAPER_JOBS_`` prefix:

| Variable                          | Default  | Description                         |
|-----------------------------------|----------|-------------------------------------|
| ``SCRAPER_JOBS_ENABLED``          | ``true``  | Master switch for the job service   |
| ``SCRAPER_JOBS_MAX_RETAINED``     | ``500``   | Maximum jobs held in memory         |
| ``SCRAPER_JOBS_MAX_CONCURRENCY``  | ``2``     | Worker pool size                    |
| ``SCRAPER_JOBS_RESULT_TTL_SECONDS`` | ``86400`` | Result retention window (24 hours)  |

### Limitations

* **In-memory only.** Jobs are **not durable** across service restarts.
  All queued, running, and completed jobs are lost when the service stops.
* **Result TTL.** Completed jobs are kept for ``result_ttl_seconds``.
  After that they become eligible for eviction by the retention policy.
* **Retention cap.** Once the total exceeds ``max_retained``, the oldest
  finished jobs are automatically removed to stay under the limit.
* **Raspberry Pi.** Keep ``jobs.max_concurrency`` low (1–2) on resourceconstrained devices.  Each worker shares the Redis-free scraper pool.

### Metrics

Additional Prometheus counters are exposed:

| Metric                   | Type    | Description              |
|--------------------------|---------|--------------------------|
| ``jobs_created_total``   | counter | Jobs created             |
| ``jobs_running``         | gauge   | Currently running        |
| ``jobs_succeeded_total`` | counter | Successful jobs          |
| ``jobs_failed_total``    | counter | Failed jobs              |
| ``jobs_cancelled_total`` | counter | Cancelled jobs           |
| ``jobs_queue_size``      | gauge   | Jobs waiting in the queue|

---

## Raspberry Pi deployment (Podman + Quadlet)

### Prerequisites

- Raspberry Pi 3B+ or 4/5 running Raspberry Pi OS (64-bit recommended).
- Podman installed (`apt install podman`).

### Build on the Pi

```bash
# On the Pi
git clone https://github.com/your-org/scrapping-service.git
cd scrapping-service
podman build -t localhost/scraper-api:latest .
```

### Quadlet

The `remote/scraper-api.container` file is a Podman Quadlet unit.

```bash
# Install the Quadlet
mkdir -p ~/.config/containers/systemd/
cp remote/scraper-api.container ~/.config/containers/systemd/

# Reload and start
systemctl --user daemon-reload
loginctl enable-linger $USER
systemctl --user start scraper-api.service

# Check logs
journalctl --user -u scraper-api.service -f

# Test
curl http://localhost:8080/health
```

### Deploy script

The included [`scripts/deploy.sh`](scripts/deploy.sh) automates deployment from
your dev machine:

```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh pi@raspberrypi.local --with-env
```

See `--help` for all options.

---

## Error handling

All errors follow a consistent JSON structure:

```json
{
  "error": {
    "type": "validation_error",
    "message": "Invalid URL scheme",
    "details": {}
  }
}
```

Error types:

| Type                   | HTTP Status | Description                    |
|------------------------|-------------|--------------------------------|
| `validation_error`     | 400         | Invalid request parameters     |
| `security_error`       | 403         | URL blocked by SSRF or policy  |
| `timeout_error`        | 504         | Request exceeded timeout       |
| `blocked_error`        | 403         | Response appears blocked/empty |
| `http_error`           | 502         | Upstream HTTP request failed   |
| `browser_error`        | 502         | Browser rendering failed       |
| `cache_error`          | 500         | Cache backend error            |
| `internal_error`       | 500         | Unexpected internal error      |
| `authentication_error` | 401/403     | Invalid or missing API key     |

---

## Metrics

The `/metrics` endpoint exposes simple counters in Prometheus text format:

| Metric                   | Type    | Description                 |
|--------------------------|---------|-----------------------------|
| `scraper_up`             | gauge   | 1 = service up, 0 = down    |
| `scrape_requests_total`  | counter | All scrape requests         |
| `scrape_success_total`   | counter | Successful scrapes          |
| `scrape_error_total`     | counter | Failed scrapes              |
| `extraction_requests_total` | counter | Extraction requests      |
| `extraction_success_total`  | counter | Successful extractions   |
| `extraction_error_total`    | counter | Failed extractions       |
| `jobs_created_total`    | counter | Async jobs created            |
| `jobs_running`          | gauge   | Currently running jobs        |
| `jobs_succeeded_total`  | counter | Successful jobs               |
| `jobs_failed_total`     | counter | Failed jobs                   |
| `jobs_cancelled_total`  | counter | Cancelled jobs                |
| `jobs_queue_size`       | gauge   | Jobs waiting in the queue     |
| `extraction_success_total`  | counter | Successful extractions      |
| `extraction_error_total`    | counter | Failed extractions          |
| `cache_hits_total`       | counter | Cache hits                  |
| `cache_misses_total`     | counter | Cache misses                |
| `cache_stale_hits_total` | counter | Stale cache served on error |
| `scrape_duration_ms_sum` | counter | Total scrape duration (ms)  |
| `proxy_requests_total`   | counter | Requests routed through a proxy |
| `proxy_errors_total`     | counter | Proxy-related errors            |

---

## Troubleshooting

### Browser mode fails with "Botasaurus is not installed"

Install Botasaurus and ensure Chromium is available:

```bash
pip install botasaurus
apt install chromium chromium-driver
```

### Chromium crashes inside container

Ensure the container has enough shared memory:

```bash
podman run --shm-size=256m scraper-api:latest
```

The Quadlet file already sets `ShmSize=256M`.

### Cache performance on Raspberry Pi

SQLite with WAL mode performs well on SD cards. If you need better performance,
consider mapping the cache to an external SSD.

### "Cannot resolve hostname"

The service resolves hostnames to check for private IPs. If DNS is not
available inside the container, configure `security.block_private_ips: false`
**only** if you understand the security implications.

---

## Ethical use

This service is intended for ethical scraping:

- **Respect `robots.txt`** — check a site's robots.txt before scraping.
- **Respect terms of service** — do not scrape sites that prohibit it.
- **Respect rate limits** — configure per-domain delays to avoid overwhelming
  target servers.
- **Respect applicable laws** — scraping may be regulated in your jurisdiction.
  Ensure you comply with local, national, and international laws.

The rate limiting and domain policy features are designed to help you scrape
responsibly — but the ultimate responsibility lies with you.

---

## Development

### Project structure

```text
.
├── app/                    # Application code
│   ├── api/                # FastAPI routes and dependencies
│   ├── cache/              # SQLite cache backend
│   ├── core/               # Config, errors, logging, security
│   ├── metrics/            # Prometheus metrics
│   ├── schemas/            # Pydantic request/response models
│   └── scraper/            # Fetch service, HTTP/browser fetchers
├── configs/                # Configuration examples
├── remote/                 # Podman Quadlet files
├── scripts/                # Deploy and smoke-test scripts
├── tests/                  # pytest test suite
├── .github/                # CI/CD workflows and Dependabot
├── Dockerfile              # Multi-stage container image
└── README.md
```

### Linting and formatting

```bash
ruff check .
ruff format .
```

### Adding a new fetcher

1. Create a new class in `app/scraper/` that returns a `FetchResult`.
2. Add the fetch logic.
3. Wire it into `ScraperService.scrape()` via conditional mode selection.

---

## License

MIT — see [LICENSE](LICENSE) for details.
