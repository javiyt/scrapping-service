"""FastAPI application entry-point."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import yaml as pyyaml
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse

from app.api.browser_routes import browser_router, get_agent_browser_service  # noqa: F401
from app.api.dependencies import get_settings
from app.api.routes import health_router, plugins_router, router
from app.auth.resolver import init_profile_resolver
from app.cache.maintenance import CacheMaintenanceService
from app.cache.sqlite_cache import SqliteCache
from app.camofox import build_camofox_client
from app.core.errors import ScraperError
from app.core.logging import setup_logging
from app.jobs.service import JobService
from app.metrics.prometheus import get_metrics
from app.plugins.loader import load_plugins
from app.scraper.browser_pool import BrowserFetcherPool
from app.search import build_search_cache, build_search_router
from app.transcript import build_transcript_cache, build_transcript_router

logger = logging.getLogger("scraper-api")
OPENAPI_YAML_PATH = Path(__file__).resolve().parents[1] / "openapi.yaml"


def custom_openapi_schema(app: FastAPI) -> dict[str, Any]:
    """Load and return OpenAPI schema from openapi.yaml file."""
    if app.openapi_schema:
        return app.openapi_schema

    try:
        with OPENAPI_YAML_PATH.open(encoding="utf-8") as f:
            schema = pyyaml.safe_load(f)
        app.openapi_schema = dict(schema)
    except FileNotFoundError:
        # Fall back to FastAPI's default schema if openapi.yaml doesn't exist
        app.openapi_schema = get_openapi(title=app.title, version=app.version, routes=app.routes)

    return app.openapi_schema


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
    _browser_fetcher_pool = BrowserFetcherPool(max_size=settings.browser_max_pool_size)
    app.state.browser_fetcher_pool = _browser_fetcher_pool

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

    # Build search router first to get camofox_client from the provider
    search_router = build_search_router(settings, metrics=metrics)
    app.state.search_router = search_router
    search_providers = search_router.registry.all()
    app.state.search_providers = search_providers

    # Extract camofox_client from search providers (if any)
    _camofox_client = None
    for provider in search_providers:
        provider_client = getattr(provider, "client", None)
        if provider_client is not None and getattr(provider, "name", None) == "camofox":
            _camofox_client = provider_client
            break
    if _camofox_client is None:
        _camofox_client = build_camofox_client(settings)
    app.state.camofox_client = _camofox_client
    if _camofox_client is not None:
        logger.info("Camofox client ready (external service client; no browser launched here)")
    else:
        logger.info("Camofox client disabled")

    # Load plugins AFTER search providers so we can pass camofox_client
    _shared_http = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        follow_redirects=False,
    )
    app.state.http_client = _shared_http
    _plugin_result = load_plugins(
        settings, camofox_client=_camofox_client, http_client=_shared_http
    )
    # Store plugin load result for /v1/plugins/status endpoint
    app.state.plugin_load_result = _plugin_result
    app.state.extractor_registry = _plugin_result.extractor_registry
    logger.info(
        "Plugin loading complete: loaded=%s, skipped=%d",
        _plugin_result.loaded,
        len(_plugin_result.skipped),
    )
    if _plugin_result.skipped:
        for entry in _plugin_result.skipped:
            logger.warning("Plugin '%s' skipped: %s", entry["name"], entry["reason"])
    if search_providers:
        order = ", ".join(search_router.config.provider_order) or "registration"
        logger.info(
            "Search router ready (default_provider=%s, priority=[%s]): %s",
            search_router.config.default_provider,
            order,
            ", ".join(provider.name for provider in search_providers),
        )
    else:
        logger.info("No search providers enabled")

    # ---- Search result cache
    # Provider-agnostic cache for normalized search responses, sitting above
    # the router (all providers share it).  Entries live in the shared
    # SQLite database, in their own table, maintained by the same loop as
    # every other cache entry.  The search API routes depend on it.
    search_cache = build_search_cache(settings, search_router, _cache)
    app.state.search_cache = search_cache
    if search_cache.config.enabled:
        logger.info(
            "Search result cache enabled (default_ttl=%ss, stale_if_error=%s)",
            search_cache.config.default_ttl_seconds,
            str(search_cache.config.stale_if_error).lower(),
        )
    else:
        logger.info("Search result cache disabled")

    # ---- Camofox client
    # Reusable client for the external Camofox browser REST service.  When
    # the Camofox *search provider* is enabled the lifespan shares the
    # client that provider already built (one HTTP client per process —
    # sessions are still per-search); otherwise it builds its own for the
    # browser-fallback consumer.  Camofox is an external service: nothing is
    # launched here, only an HTTP client.  (Resolving it before the
    # transcript router so the transcript provider can reuse it.)
    _camofox_client = None
    for provider in search_providers:
        provider_client = getattr(provider, "client", None)
        if provider_client is not None and getattr(provider, "name", None) == "camofox":
            _camofox_client = provider_client
            break
    if _camofox_client is None:
        _camofox_client = build_camofox_client(settings)
    app.state.camofox_client = _camofox_client
    if _camofox_client is not None:
        logger.info("Camofox client ready (external service client; no browser launched here)")
    else:
        logger.info("Camofox client disabled")

    # ---- Transcript router + result cache
    # Provider-agnostic YouTube transcript stack (mirrors the search
    # stack): an ordered provider registry + capability router, and a
    # provider-agnostic result cache above it (own table, shared
    # maintenance loop).  The Camofox provider reuses the shared client
    # resolved above; with none enabled the router reports
    # no_transcript_provider_available.
    transcript_router = build_transcript_router(
        settings,
        metrics=metrics,
        camofox_client=_camofox_client,
        extra_providers=_plugin_result.transcript_providers,
    )
    app.state.transcript_router = transcript_router
    transcript_providers = transcript_router.registry.all()
    app.state.transcript_providers = transcript_providers
    transcript_cache = build_transcript_cache(settings, transcript_router, _cache, metrics=metrics)
    app.state.transcript_cache = transcript_cache
    if transcript_providers:
        logger.info(
            "Transcript router ready (default_provider=%s, priority=[%s]): %s",
            transcript_router.config.default_provider,
            ", ".join(transcript_router.config.provider_order) or "registration",
            ", ".join(provider.name for provider in transcript_providers),
        )
    else:
        logger.info("No transcript providers enabled")
    if transcript_cache.config.enabled:
        logger.info(
            "Transcript result cache enabled (default_ttl=%ss, stale_if_error=%s)",
            transcript_cache.config.default_ttl_seconds,
            str(transcript_cache.config.stale_if_error).lower(),
        )
    else:
        logger.info("Transcript result cache disabled")

    # ---- Interactive agent browser service
    # Owns the /v1/browser/... contract.  Built only when the agent browser is
    # enabled AND a Camofox client is ready; otherwise the state holds None and
    # the routes return a clean 503.  The maintenance loop (idle-session TTL)
    # is started here and stopped on shutdown.
    from app.browser import AgentBrowserService, create_provider

    _agent_browser_provider = create_provider(settings, _camofox_client)
    _agent_browser_service = None
    if _agent_browser_provider is not None:
        _agent_browser_service = AgentBrowserService(_agent_browser_provider, settings)
        await _agent_browser_service.start_maintenance()
        app.state.agent_browser_service = _agent_browser_service
        logger.info(
            "Agent browser service ready (provider=%s, interactive API enabled)",
            _agent_browser_provider.name,
        )
    else:
        app.state.agent_browser_service = None
        if settings.agent_browser_enabled:
            logger.info(
                "Agent browser enabled but no provider ready (Camofox client missing) "
                "— /v1/browser will report unavailable"
            )
        else:
            logger.info("Agent browser service disabled")

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
            browser_fetcher_pool=_browser_fetcher_pool,
            camofox_client=_camofox_client,
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
    if _agent_browser_service is not None:
        await _agent_browser_service.stop_maintenance()
    if _job_service is not None:
        await _job_service.stop()
    if _maintenance is not None:
        await _maintenance.stop()
    _browser_fetcher_pool.close_all()
    for provider in search_providers:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()
    for provider in transcript_providers:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()
    if _camofox_client is not None:
        await _camofox_client.close()
    # Close the shared http client used by plugins
    if hasattr(app.state, "http_client"):
        await app.state.http_client.aclose()
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
app.include_router(plugins_router, tags=["Plugins"])
app.include_router(browser_router)

# Register custom OpenAPI schema for /docs and /redoc
app.openapi = lambda: custom_openapi_schema(app)  # type: ignore[method-assign]


@app.get("/openapi.yaml", include_in_schema=False)
async def openapi_yaml() -> FileResponse:
    """Return the source OpenAPI YAML specification."""
    return FileResponse(OPENAPI_YAML_PATH, media_type="application/yaml")


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
    metrics = get_metrics()
    if request.url.path.startswith(("/v1/scrape", "/v2/scrape")):
        metrics.inc("scrape_error_total")
        metrics.mark_scrape_error()
    elif request.url.path.startswith("/v1/search"):
        metrics.inc("search_error_total")
    elif request.url.path.startswith("/v1/transcript"):
        metrics.inc("transcript_error_total")
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
