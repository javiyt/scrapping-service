# Development

## Prerequisites

- Python 3.11 or newer.
- Docker with Buildx for multi-architecture image builds.
- Podman for local Quadlet deployment tests.
- Access to GitHub Container Registry when publishing images.

## Application Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

python app/run.py --reload
```

## Documentation Setup

```bash
pip install -r requirements-docs.txt
make docs-serve
```

`make docs-serve` copies the root `openapi.yaml` into the MkDocs source tree so
the API page can render the same specification that the service exposes.

## Checks

```bash
ruff check .
ruff format --check .
pytest
make docs-build
```

The GitHub Pages workflow runs `mkdocs build --strict` on pushes to `main`.
