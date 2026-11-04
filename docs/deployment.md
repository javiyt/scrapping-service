# Deployment

## Docker Image

The Docker workflow publishes multi-architecture images to GitHub Container
Registry on pushes to `main`, version tags, and manual dispatches.

Published image:

```text
ghcr.io/javiyt/scrapping-service
```

## Raspberry Pi Deploy

The deploy script can pull a pre-built image from GHCR and update the remote
Quadlet service.

```bash
./scripts/deploy.sh javiyt@raspberry5 --tag latest --with-env
```

For a release tag:

```bash
./scripts/deploy.sh javiyt@raspberry5 --tag v1.1.0 --with-env
```

## GitHub Pages

Documentation is deployed by `.github/workflows/pages.yml` after a successful
build from the `main` branch.

The published site is:

```text
https://javiyt.github.io/scrapping-service/
```

## Health & Observability

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET /health` | none | Liveness — always `200` while the process runs. Deliberately I/O-free. |
| `GET /health/deep` | none | Container probe — cache + scrape-error trend, and the search subsystem (providers, availability, SearXNG discovery state). `503` on local problems or the error threshold. |
| `GET /ready` | API key | Readiness for load balancers. |
| `GET /metrics` | none | Prometheus text metrics. |

The Quadlet unit's `HealthCmd` targets `/health/deep`; the deep check
**never fails because an external search backend is down** — that state is
reported under `checks.search` so you can alert on it without coupling
restarts to search availability.

A Prometheus scrape config for the container:

```yaml
scrape_configs:
  - job_name: scraper-api
    static_configs:
      - targets: ["scraper:8080"]
    metrics_path: /metrics
```

Useful search metrics: `search_requests_total`, `search_error_total`,
`search_fallbacks_total`, `search_cache_hits_total` / `search_cache_misses_total`,
`search_duration_ms_sum`, and the per-provider `search_provider_attempts_total` /
`search_provider_success_total` / `search_provider_error_total` (labelled only
by the bounded, configured `provider` name).

Useful transcript metrics (same shape): `transcript_requests_total`,
`transcript_error_total`, `transcript_fallbacks_total`,
`transcript_cache_hits_total` / `transcript_cache_misses_total` /
`transcript_cache_stale_hits_total`, `transcript_duration_ms_sum`, and the
per-provider `transcript_provider_attempts_total` /
`transcript_provider_success_total` / `transcript_provider_error_total`.

## External Search Backends: SearXNG and Camofox

Search and browser automation are backed by **external** services that are
deployed independently.  Neither is ever embedded in the scraper image:

```text
             scraper-api
              /           \
             /             \
        SearXNG           Camofox
          :8080             :9377
```

- **SearXNG** — metasearch instance for the provider-agnostic search API
  ([`search.providers.searxng`](https://github.com/javiyt/scrapping-service/blob/main/configs/config.example.yaml)).
- **Camofox** — browser REST service powering the Camofox search provider,
  the optional browser fallback (`browser.fallback_backends`), and the
  interactive `/v1/browser` API.
- **Botasaurus** remains inside the scraper runtime as the primary browser
  engine; nothing here changes that.

Both backends are optional: with `enabled: false` for each, the service
runs exactly as the standalone deployment above (plain HTTP + Botasaurus,
no search).

### Container networking

Deploy the three containers on a **private bridge network**
(`scraper-network`).  The scraper resolves the backends by container name
(`http://searxng:8080`, `http://camofox:9377`).  Only the scraper API port
is published on the host; SearXNG and Camofox publish **no** host ports —
the scraper API is the externally consumed facade.

Copy-ready Quadlet units for all three (plus the shared network and the
SearXNG `settings.yml`) live in
[`remote/`](https://github.com/javiyt/scrapping-service/tree/main/remote);
see [`remote/README.md`](https://github.com/javiyt/scrapping-service/blob/main/remote/README.md)
for the step-by-step host setup.

```bash
# host: install units + files (see remote/README.md for the full list)
podman build -t localhost/scraper-api:latest .
systemctl --user daemon-reload
systemctl --user start scraper-api-network searxng camofox scraper-api
```

### Configuration reference

The complete required settings with environment-variable overrides:

**SearXNG** — `search.providers.searxng` in the config file, or
`SCRAPER_SEARCH_SEARXNG_*` environment variables:

| Setting | Env var | Purpose |
| --- | --- | --- |
| `enabled` | `SCRAPER_SEARCH_SEARXNG_ENABLED` | Master switch (default `false`) |
| `base_url` | `SCRAPER_SEARCH_SEARXNG_BASE_URL` | e.g. `http://searxng:8080` |
| `timeout_seconds` | `SCRAPER_SEARCH_SEARXNG_TIMEOUT_SECONDS` | HTTP timeout (1-300 s) |
| `default_language` | `SCRAPER_SEARCH_SEARXNG_DEFAULT_LANGUAGE` | BCP-47 tag used when a request has none |
| `config_cache_ttl_seconds` | `SCRAPER_SEARCH_SEARXNG_CONFIG_CACHE_TTL_SECONDS` | `/config` discovery cache TTL |

**Camofox** — top-level `camofox` section (shared by the search provider,
the browser fallback, and the interactive API), or
`SCRAPER_CAMOFOX_*` environment variables:

| Setting | Env var | Purpose |
| --- | --- | --- |
| `enabled` | `SCRAPER_CAMOFOX_ENABLED` | Master switch (default `false`) |
| `base_url` | `SCRAPER_CAMOFOX_BASE_URL` | e.g. `http://camofox:9377` |
| `timeout_seconds` | `SCRAPER_CAMOFOX_TIMEOUT_SECONDS` | Per-request timeout |
| `access_key` | `CAMOFOX_ACCESS_KEY` | General-purpose key (**secret — env only**) |
| `api_key` | `CAMOFOX_API_KEY` | Elevated key for sensitive routes (**secret — env only**) |

Session/tabs limits are configured by the consumers, not the client:
`SCRAPER_AGENT_BROWSER_MAX_SESSIONS` / `MAX_TABS_PER_SESSION` bound the
interactive API, and the scraper's own concurrency settings
(`SCRAPER_SCRAPER_MAX_CONCURRENCY`, `SCRAPER_BROWSER_MAX_POOL_SIZE`) bound
fallback usage.

The SearXNG instance itself must allow JSON output (`search.formats:
[html, json]` in its `settings.yml` — a ready template ships in
[`remote/config/`](https://github.com/javiyt/scrapping-service/tree/main/remote/config)).

### Health

`/health/deep` reports each external dependency **without failing the
check**:

- `checks.search` — search provider availability (SearXNG reachability and
  `/config` discovery state, Camofox search provider probe).
- `checks.camofox` — the Camofox service's own health probe
  (reachability, pool size, browser connection state) when the `camofox`
  client is enabled; `disabled` otherwise.
- Botasaurus runs in-process; browser problems surface through the
  scrape-error trend check instead of a dependency probe.

`/health` remains a cheap, I/O-free liveness check that never touches any
external service.

### Degraded behavior

External backends are **reported, not fatal**: the container restart policy
(`HealthOnFailure` on `/health/deep`) never depends on SearXNG or Camofox
availability.

**SearXNG unavailable**

- `provider: auto` search falls back to the next healthy provider —
  typically Camofox (the router probes capabilities before selecting).
- An explicit `provider: searxng` request fails clearly with
  `search_provider_unavailable` (503).
- If enabled, a stale (expired) search cache entry may be returned when the
  live search fails (`search.cache.stale_if_error` — on by default).
- Normal scraping is unaffected.

**Camofox unavailable**

- SearXNG searches continue (the router skips the down provider).
- HTTP scraping always continues; browser-mode scraping falls back from
  Botasaurus as usual — with Camofox down, `browser.fallback_backends:
  ["camofox"]` simply has no fallback and the primary Botasaurus result
  stands.
- Camofox-specific search verticals (Google / Reddit / YouTube via the
  search macros) become unavailable.
- Interactive `/v1/browser` operations return "provider unavailable"
  (503) with the failure reason — sessions close cleanly.
- The Camofox *search provider* is unaffected by the browser fallback
  configuration; both ride the same external service.

**Both unavailable**

The service operates as a standalone scraper: plain HTTP + Botasaurus
scraping works normally; every search request returns a clear
`no_provider_available` (503) error rather than hanging.

### Secrets

- **Camofox keys** — `CAMOFOX_ACCESS_KEY` / `CAMOFOX_API_KEY` in the host
  environment file (`~/scraper-api/.env`, mode `0600`); the config file
  references them as `${CAMOFOX_ACCESS_KEY}` and is unexpanded → treated
  as unset (with a startup warning) rather than sent on the wire.
- **Scraper API key** — `SCRAPER_API_KEY` in the same file, referenced via
  `${SCRAPER_API_KEY}` in the config.
- Never commit literal key values; the example files in the repository only
  contain `change-me` placeholders and `${...}` references.

### Standalone deployment (no external backends)

Unchanged: run `scraper-api.container` alone (host networking) with
`search.providers.*.enabled: false` and `camofox.enabled: false` in the
config.  Search and the interactive browser API are then simply not
registered; everything else behaves exactly as before.
