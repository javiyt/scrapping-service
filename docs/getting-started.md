# Getting Started

## Local Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cp configs/config.example.yaml configs/config.yaml
cp .env.example .env

python app/run.py
```

The service listens on port `8080` by default.

## Useful URLs

- Health check: <http://localhost:8080/health>
- Deep health check (search + transcript subsystems included): <http://localhost:8080/health/deep>
- Readiness check: <http://localhost:8080/ready>
- Metrics (Prometheus): <http://localhost:8080/metrics>
- Swagger UI: <http://localhost:8080/docs>
- ReDoc: <http://localhost:8080/redoc>
- OpenAPI JSON: <http://localhost:8080/openapi.json>
- OpenAPI YAML: <http://localhost:8080/openapi.yaml>

Search endpoints (require the API key; enable SearXNG in the config first):

- `POST /v1/search` — provider-agnostic search
- `GET /v1/search/capabilities` — service-level search capabilities

The search result cache lives in a `search_cache` table inside the same
SQLite database as the HTML cache, so the existing cache volume mount covers
it.

Transcript endpoint (requires the API key; enable a transcript provider in
the config first — see [Transcripts](transcripts.md)):

- `GET /v1/transcript?url=<YouTube URL>&lang=es,en` — normalized transcript

The transcript result cache lives in a `transcript_cache` table in the same
SQLite database, so the same volume mount covers it.

## Run Tests

```bash
pytest tests/ -v --tb=short
ruff check .
ruff format --check .
```

## Docker

```bash
docker build -t scraper-api:latest .

docker run -d \
  --name scraper-api \
  -p 8080:8080 \
  -v $(pwd)/configs/config.yaml:/config/config.yaml:ro \
  -v $(pwd)/data:/data \
  -v $(pwd)/.env:/.env:ro \
  scraper-api:latest
```

For the full production topology with external SearXNG and Camofox services
(shared private network, Quadlet units, degraded behavior), see
[Deployment](deployment.md#external-search-backends-searxng-and-camofox).
