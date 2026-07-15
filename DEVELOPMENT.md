# Development

## Prerequisites

- **Python 3.11+** with `venv`
- **Docker** with [Buildx](https://docs.docker.com/build/buildx/install/) (for multi-arch builds)
- **Podman** (optional — for testing Quadlet deployment locally)
- Access to **GitHub Container Registry** (`ghcr.io`)

---

## Local Development

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run the API (config loads from configs/config.yaml by default)
# Use the app/run.py wrapper (same as production, normalizes config)
python app/run.py

# Quick shortcuts with CLI arguments:
python app/run.py --port 9090
python app/run.py --port 9090 --log-level debug
python app/run.py --reload  # Enable auto-reload on file changes
```

### Configuration: CLI arguments, environment variables, or defaults

The `app/run.py` wrapper supports multiple configuration methods (CLI args take priority):

```bash
# Via CLI arguments (highest priority)
python app/run.py --port 9090 --log-level debug --reload

# Via environment variables
export SCRAPER_SERVER_PORT=9090
export LOG_LEVEL=debug
export TIMEOUT_KEEP_ALIVE=30
export LIMIT_MAX_REQUESTS=5000
python app/run.py

# Mixed: CLI args override environment variables
export SCRAPER_SERVER_PORT=8080
python app/run.py --port 9090  # Runs on 9090, not 8080
```

**Configuration options:**

```bash
--port PORT                    # Port to listen on (default: 8080)
--log-level LEVEL              # Log level: info, debug, warning, error, critical, trace
                               # (accepts any case: INFO, info, Debug, etc.)
--timeout-keep-alive SECONDS   # Keep-alive timeout (default: 30)
--limit-max-requests COUNT     # Max requests per worker (default: 5000)
--reload                       # Enable auto-reload on file changes (dev mode)
```

The app reads configuration from `configs/config.yaml` by default. See
[config.example.yaml](configs/config.example.yaml) for the full reference.

---

## Building Docker Images

### Local build (your host architecture)

```bash
docker build -t scraper-api:latest .
```

### Multi-architecture build (amd64 + arm64)

Useful when you want to test the exact image that will run on a Raspberry Pi:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t scraper-api:latest \
  --load .
```

> `--load` only works for a single platform at a time. To load both locally, build
> once per platform or push directly to a registry (see below).

---

## Publishing to GitHub Container Registry

### One-time: authenticate with GHCR

```bash
echo <GITHUB_TOKEN> | docker login ghcr.io -u <USERNAME> --password-stdin
```

Your token needs the `write:packages` scope. A fine-grained token with
**Contents: read** and **Packages: write** permissions on this repository works.

### Manual push

```bash
# Tag the image
export VERSION=1.0.0
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/javiyt/scrapping-service:${VERSION} \
  -t ghcr.io/javiyt/scrapping-service:latest \
  --push \
  .
```

The `--push` flag builds for both platforms and pushes the multi-arch manifest.

### CI-automated push (recommended)

Every push to `main` or a tag matching `v*` triggers the
[.github/workflows/docker.yml](.github/workflows/docker.yml) workflow, which:

1. Builds for `linux/amd64` and `linux/arm64` simultaneously
2. Pushes to `ghcr.io/javiyt/scrapping-service` with these tags:

   | Trigger | Tags pushed |
   |---|---|
   | Push to `main` | `latest`, `sha-<short>` |
   | Tag `v1.2.3` | `1.2.3`, `1.2`, `latest`, `sha-<short>` |

> **Note:** The CI workflow strips the `v` prefix from git tags when
> publishing Docker images. A git tag `v1.0.0` produces an image tagged
> `1.0.0` on GHCR. The deploy script handles this automatically — you
> can pass `--tag v1.0.0` or `--tag 1.0.0` and it will resolve to `1.0.0`.

---

## Creating a Release

```bash
# 1. Update the version in pyproject.toml
#    (current: 1.0.0)
#    Bump according to semver:
#      - MAJOR: breaking API changes
#      - MINOR: new features, backwards compatible
#      - PATCH: bug fixes

# 2. Commit the version bump
git add pyproject.toml
git commit -m "Bump version to 1.1.0"

# 3. Tag and push
git tag v1.1.0
git push origin main --tags
```

Pushing the tag triggers the CI workflow (`docker.yml`), which builds the
multi-arch image and pushes it with the semver tags. The deploy script can
then pull that exact version:

```bash
./scripts/deploy.sh javiyt@raspberry5 --tag v1.1.0 --with-env
```

### Tag naming convention

| Kind | Example | Description |
|---|---|---|
| Full release | `v1.2.3` | Stable, tested release |
| Pre-release | `v1.2.0-rc.1` | Release candidate (not pushed as `latest`) |
| Hotfix | `v1.2.1` | Patch on the latest release branch |

---

## Deploying with a Pre-Built Image

The deploy script now supports a `--tag` flag that pulls a pre-built image
from GHCR instead of building on the remote host:

```bash
# Deploy the latest published image
./scripts/deploy.sh javiyt@raspberry5 --tag latest --with-env

# Deploy a specific version
./scripts/deploy.sh javiyt@raspberry5 --tag v1.1.0 --with-env
```

When `--tag` is used, the script:

1. Copies `.env` and `configs/config.yaml` to the remote host
2. Pulls `ghcr.io/javiyt/scrapping-service:<tag>`
3. Patches the Quadlet file to reference the pulled image
4. Restarts the service and runs the health check

It skips the source code transfer and the remote Docker build entirely —
much faster, especially on a Raspberry Pi.

---

## Project Structure

```
├── app/                    # Python application
│   ├── main.py             # FastAPI entry point
│   ├── api/                # REST routes & dependencies
│   ├── cache/              # SQLite-backed response cache
│   ├── core/               # Config, errors, logging, security
│   ├── metrics/            # Prometheus metrics
│   ├── schemas/            # Pydantic request/response models
│   └── scraper/            # HTTP & browser-based fetching
├── configs/                # YAML configuration files
├── data/                   # SQLite database (gitignored)
├── debug/                  # Screenshots & HTML dumps (gitignored)
├── logs/                   # Runtime logs (gitignored)
├── remote/                 # Quadlet container definition
├── scripts/                # Deploy & smoke-test helpers
├── tests/                  # pytest suite
├── Dockerfile              # Multi-stage container build
└── .github/workflows/      # CI + Docker publish workflows
```
