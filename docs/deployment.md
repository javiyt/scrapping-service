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
