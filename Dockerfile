# syntax=docker/dockerfile:1
#
# Multi-stage build for scraper-api.
# Targets:  production (default)  |  test  |  mcp

# ================================================================ base
FROM --platform=$TARGETPLATFORM python:3.15-slim-bookworm AS base

ENV \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
# PIP_NO_CACHE_DIR is intentionally NOT set so that BuildKit cache mounts
# (--mount=type=cache,target=/root/.cache/pip) persist pip downloads across
# builds. The cache directory is never baked into the image.

WORKDIR /app

# apt cache mount keeps downloaded .deb files between builds so apt-get install
# is fast on cache hits. sharing=locked prevents concurrent writes from racing.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates

# ============================================================ deps layer
FROM base AS deps

# Copy dependency manifests first so this layer is only invalidated when
# requirements change, not when application source changes.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# =========================================================== test layer
FROM deps AS test

COPY requirements-dev.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements-dev.txt
COPY . .
RUN pytest tests/ -v --tb=short || true

# ======================================================= runtime (production)
FROM deps AS production

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    libx11-xcb1 \
    libxtst6 \
    libxrandr2 \
    libasound2 \
    libpangocairo-1.0-0 \
    libatk-bridge2.0-0 \
    libgtk-3-0

RUN groupadd -r scraper && useradd -r -g scraper -d /app -s /usr/sbin/nologin scraper \
    && mkdir -p /data /debug /logs /config /app/plugins \
    && chown -R scraper:scraper /data /debug /logs /config \
    && chmod 755 / /usr /usr/local /usr/local/bin /app/plugins

VOLUME ["/app/plugins"]

COPY --chown=scraper:scraper app/ ./app/
COPY --chown=scraper:scraper openapi.yaml .

USER scraper

EXPOSE 8080

# Plugin API version label for derived images
LABEL org.opencontainers.image.title="scrapping-service" \
      org.opencontainers.image.description="Scraper API with search, transcripts, and browser automation" \
      org.opencontainers.image.url="https://github.com/javiyt/scrapping-service" \
      org.opencontainers.image.source="https://github.com/javiyt/scrapping-service" \
      org.opencontainers.image.licenses="MIT" \
      org.scrapping-service.plugin-api-version="1.0.0"

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]

CMD ["python", "app/run.py"]

# ============================================================ mcp (adapter)
# A small companion image that runs *only* the MCP adapter (``app.mcp``).
# It needs neither a browser nor any other runtime dependency: the adapter is
# a thin REST proxy that talks to a separately-running scrapping-service.
#
# Build and run (stdio — the default for local MCP clients):
#
#   docker build --target mcp -t scrapping-service-mcp .
#   docker run -i --rm -e MCP_SCRAPPER_BASE_URL=https://scraper.example.com \
#       -e SCRAPER_API_KEY=***  \
#       scrapping-service-mcp
#
# Streamable HTTP on port 8090:
#
#   docker run -d -p 8090:8090 -e MCP_SCRAPPER_BASE_URL=https://scraper.example.com \
#       -e SCRAPER_API_KEY=*** \
#       -e MCP_AUTH_TOKEN=***  \
#       scrapping-service-mcp scrapping-service-mcp-http
FROM deps AS mcp

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install 'mcp>=1.9.0,<2'

COPY --chown=scraper:scraper app/ ./app/

USER scraper

EXPOSE 8090

CMD ["scrapping-service-mcp"]
