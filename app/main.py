"""FastAPI application entry-point."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.dependencies import get_settings
from app.api.routes import health_router, router
from app.cache.maintenance import CacheMaintenanceService
from app.cache.sqlite_cache import SqliteCache
from app.core.errors import ScraperError
from app.core.logging import setup_logging
from app.metrics.prometheus import get_metrics

logger = logging.getLogger("scraper-api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan — initialises services on startup and cleans up on shutdown."""
    # ---- startup
    settings = get_settings()
    setup_logging(settings.log_level)
    logger.info("Starting scraper-api v1.0.0")
    logger.info("Config path: %s", settings.config_path)
    logger.info("Cache path: %s", settings.cache_sqlite_path)

    # Eagerly open cache at startup.
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

    yield

    # ---- shutdown
    logger.info("Shutting down scraper-api")
    metrics.set_up(False)
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
