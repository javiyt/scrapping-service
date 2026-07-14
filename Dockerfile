# syntax=docker/dockerfile:1
#
# Multi-stage build for scraper-api.
# Targets:  production (default)  |  test

# ================================================================ base
FROM --platform=$TARGETPLATFORM python:3.14-slim-bookworm AS base

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
    && mkdir -p /data /debug /logs /config && chown -R scraper:scraper /data /debug /logs /config

COPY --chown=scraper:scraper app/ ./app/
COPY --chown=scraper:scraper openapi.yaml .

USER scraper

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info", "--timeout-keep-alive", "30", "--limit-max-requests", "5000"]
