"""FastAPI application entry-point."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import yaml as pyyaml
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi

from app.api.dependencies import get_settings
from app.api.routes import health_router, router
from app.auth.resolver import init_profile_resolver
from app.cache.maintenance import CacheMaintenanceService
from app.cache.sqlite_cache import SqliteCache
from app.core.errors import ScraperError
from app.core.logging import setup_logging
from app.jobs.service import JobService
from app.metrics.prometheus import get_metrics

logger = logging.getLogger("scraper-api")


def custom_openapi_schema(app: FastAPI) -> dict[str, any]:
    """Load and return OpenAPI schema from openapi.yaml file."""
    try:
        with open("openapi.yaml", "r", encoding="utf-8") as f:
            schema = pyyaml.safe_load(f)
        return dict(schema)
    except FileNotFoundError:
        # Fall back to FastAPI's default schema if openapi.yaml doesn't exist
        return get_openapi(title=app.title, version=app.version)


def app_openapi_schema(app: FastAPI) -> any:
    """Override OpenAPI schema with custom file-based schema."""
    try:
        with open("openapi.yaml", "r", encoding="utf-8") as f:
            return pyyaml.safe_load(f)
    except FileNotFoundError:
        # Fall back to FastAPI's default schema if openapi.yaml doesn't exist
        return get_openapi(title=app.title, version=app.version)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan — initialises services on startup and cleans up on shutdown."""
    # ---- startup
    settings = get_settings()
    setup_logging(settings.log_level)
    logger.info("Starting scraper-api v1.0.0")
    logger.info("Config path: %s", settings.config_path)
    logger.info("Cache path: %s", settings.cache_sqlite_path)

    # Initialise the ProfileResolver from settings.
    init_profile_resolver(settings)

    # Eagerly open cache at startup (shared singleton).
    _cache = SqliteCache(
        db_path=settings.cache_sqlite_path,
        max_size_mb=settings.cache_max_html_size_mb,
    )
    _cache.open()
    # Store on app.state so dependencies can reach it.
    app.state.cache = _cache
    app.state.settings = settings

    metrics = get_metrics()
    metrics.set_up(True)

    # Initialise background cache cleanup loop if enabled.
    _maintenance: CacheMaintenanceService | None = None
    if settings.cache_cleanup_enabled:
        _maintenance = CacheMaintenanceService(cache=_cache, settings=settings)
        await _maintenance.start()
        app.state.cache_maintenance = _maintenance
        logger.info(
            "Background cache cleanup is enabled (interval=%ds)",
            settings.cache_cleanup_interval_seconds,
        )
    else:
        logger.info("Background cache cleanup is disabled")

    # ---- Job service
    # Scrapers are now created per-request/per-job with profile-specific
    # effective settings.  The JobService receives the shared cache and
    # builds per-job scrapers internally.
    _job_service: JobService | None = None
    if settings.jobs_enabled:
        _job_service = JobService(
            scraper=None,  # scraper is built per-job from effective settings
            settings=settings,
            cache=_cache,
            metrics=metrics,
        )
        await _job_service.start()
        app.state.job_service = _job_service
        logger.info(
            "Job service started (max_concurrency=%d, max_retained=%d)",
            settings.jobs_max_concurrency,
            settings.jobs_max_retained,
        )
    else:
        logger.info("Job service is disabled")

    yield

    # ---- shutdown
    logger.info("Shutting down scraper-api")
    metrics.set_up(False)
    if _job_service is not None:
        await _job_service.stop()
    if _maintenance is not None:
        await _maintenance.stop()
    try:
        _cache.close()
    except Exception:
        logger.exception("Error closing cache")


# -------------------------------------------------------------------- app

app = FastAPI(
    title="Scraper API",
    description="Containerized scraping microservice — fetch rendered HTML from URLs.",
    version="1.0.0",
    lifespan=lifespan,
)

# --------------------------------------------------------------- CORS
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.server_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------- routers
app.include_router(health_router, tags=["Health"])
app.include_router(router)

# Register custom OpenAPI schema for /docs and /redoc
custom_openapi_schema(app)


# ------------------------------------------------------- global exception handler


@app.exception_handler(ScraperError)
async def scraper_error_handler(request: Request, exc: ScraperError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(),
    )


@app.exception_handler(Exception)
async def general_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "type": "internal_error",
                "message": "An unexpected error occurred",
                "details": {},
            }
        },
    )
