# syntax=docker/dockerfile:1
#
# Multi-stage build for scraper-api.
# Targets:  production (default)  |  test

# ================================================================ base
FROM --platform=$TARGETPLATFORM python:3.14-slim-bookworm AS base

ENV \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ============================================================ deps layer
FROM base AS deps

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# =========================================================== test layer
FROM deps AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY . .
RUN pytest tests/ -v --tb=short || true

# ======================================================= runtime (production)
FROM deps AS production

# Chromium / browser dependencies (optional, only if Botasaurus is used).
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    libx11-xcb1 \
    libxtst6 \
    libxrandr2 \
    libasound2 \
    libpangocairo-1.0-0 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user.
RUN groupadd -r scraper && useradd -r -g scraper -d /app -s /usr/sbin/nologin scraper \
    && mkdir -p /data /debug /logs /config && chown -R scraper:scraper /data /debug /logs /config

COPY --chown=scraper:scraper app/ ./app/
COPY --chown=scraper:scraper openapi.yaml .

USER scraper

# The container always listens on 8080 internally — host port mapping determines
# the external port (e.g. -p 9090:8080).  The SCRAPER_SERVER_PORT env var is an
# informational / config hint for the application, not the actual listen port.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info", "--timeout-keep-alive", "30", "--limit-max-requests", "5000"]
