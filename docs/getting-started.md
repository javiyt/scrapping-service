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
- Swagger UI: <http://localhost:8080/docs>
- ReDoc: <http://localhost:8080/redoc>
- OpenAPI JSON: <http://localhost:8080/openapi.json>
- OpenAPI YAML: <http://localhost:8080/openapi.yaml>

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
