"""FastAPI route definitions for the scraper API."""

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.api.dependencies import (
    get_auth_context,
    get_cache,
    get_effective_settings,
    get_expose_profile,
    get_job_service,
    get_scraper,
    get_search_cache,
    get_settings,
    verify_api_key,
)
from app.auth.models import AuthContext
from app.cache.models import CacheCleanupResult, CacheVacuumResult
from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import ScraperError
from app.jobs.models import Job, JobListResponse, JobResponse
from app.jobs.service import JobService
from app.metrics.prometheus import MetricsCollector, get_metrics
from app.schemas.health import HealthResponse, ReadinessResponse
from app.schemas.scrape import (
    BatchItem,
    BatchScrapeRequest,
    BatchScrapeResponse,
    CacheCleanupRequest,
    CacheStats,
    PurgeResponse,
    ScrapeRequest,
    ScrapeResponse,
    V2ScrapeRequest,
    V2ScrapeResponse,
)
from app.schemas.search import (
    SearchApiResponse,
    SearchCapabilitiesResponse,
    SearchRequest,
)
from app.scraper.response_processing import format_scrape_content, process_scrape_response
from app.scraper.service import ScraperService
from app.search import SearchCache, SearchCapabilities

logger = logging.getLogger("scraper-api.routes")


# ------------------------------------------------------------------- helpers


def _maybe_add_auth_profile(
    result: dict,
    auth_context: AuthContext | None,
    expose_profile: bool,
) -> dict:
    """Add ``auth_profile`` to the result metadata if configured."""
    if expose_profile and auth_context is not None:
        metadata = dict(result.get("metadata", {}))
        metadata["auth_profile"] = auth_context.profile_name
        return {**result, "metadata": metadata}
    return result


# ------------------------------------------------------------------- router

router = APIRouter(dependencies=[Depends(verify_api_key)])

# Public endpoints (no auth) — see also the auth dependency exception for /health.
health_router = APIRouter()


# ================================================================ /health


async def _search_health_report(search_cache: SearchCache) -> dict[str, Any]:
    """Build the ``search`` section of the deep health report.

    Reports whether search is configured, the result-cache policy, the
    enabled providers (in routing priority order), per-provider
    availability, and provider diagnostics (e.g. the SearXNG ``/config``
    discovery state).  Availability comes from the router's bounded
    capabilities probe; diagnostics are a zero-I/O read of cached provider
    state.  A down provider degrades the reported ``status`` but never
    fails the overall check — external search backends must not gate the
    service's restart policy.
    """
    router = search_cache.router
    providers: list[dict[str, Any]] = []
    for provider in router.ordered_candidates():
        caps = await router.probe_capabilities(provider)
        entry: dict[str, Any] = {
            "name": provider.name,
            "available": bool(caps.available) if caps is not None else False,
            "probe": "ok" if caps is not None else "failed",
        }
        diagnostics = getattr(provider, "diagnostics", None)
        if callable(diagnostics):
            entry.update(diagnostics())
        providers.append(entry)

    if not providers:
        status = "no providers configured"
    elif any(entry["available"] for entry in providers):
        status = "ok"
    else:
        status = "degraded (no provider available)"

    return {
        "status": status,
        "configured": True,
        "cache_enabled": search_cache.config.enabled,
        "providers": providers,
    }


@health_router.get("/health", response_model=HealthResponse, tags=["Health"])
async def health():
    """Liveness check.  Always returns 200 when the service is running.

    Deliberately I/O-free: it never touches the cache or any external
    dependency (including search providers), so liveness stays cheap.
    """
    return HealthResponse()


@health_router.get("/health/deep", response_model=ReadinessResponse, tags=["Health"])
async def deep_health(request: Request):
    """Deep health check for container restart policy.

    Verifies internal dependencies and recent scrape error patterns. This is
    intentionally separate from /health so cheap liveness checks still work.

    The ``search`` check reports the search subsystem: result-cache policy,
    enabled providers, per-provider availability (bounded capabilities
    probe) and provider diagnostics (e.g. SearXNG ``/config`` discovery).
    External search backends are *reported but non-fatal*: a down provider
    is surfaced in the report, but does not fail the check.  Only local
    infrastructure problems (missing cache or missing search subsystem)
    fail it, so the restart policy never depends on a search backend.
    """
    checks: dict[str, Any] = {}
    ok = True

    settings: Settings | None = getattr(request.app.state, "settings", None)
    if settings is None:
        try:
            settings = get_settings()
            checks["config"] = "ok"
        except Exception as exc:
            checks["config"] = f"error: {exc}"
            ok = False
    else:
        checks["config"] = "ok"

    cache: SqliteCache | None = getattr(request.app.state, "cache", None)
    if cache is None:
        checks["cache"] = "unavailable"
        ok = False
    else:
        try:
            cache.stats()
            checks["cache"] = "ok"
        except Exception as exc:
            checks["cache"] = f"error: {exc}"
            ok = False

    metrics = get_metrics()
    snapshot = metrics.health_snapshot()
    consecutive_errors = int(snapshot["consecutive_scrape_errors"])
    threshold = settings.health_max_consecutive_scrape_errors if settings is not None else 5
    checks["consecutive_scrape_errors"] = consecutive_errors
    checks["max_consecutive_scrape_errors"] = threshold
    checks["last_scrape_error_at"] = snapshot["last_scrape_error_at"]
    checks["last_scrape_success_at"] = snapshot["last_scrape_success_at"]

    if consecutive_errors >= threshold:
        checks["scrape_errors"] = "too_many_consecutive_errors"
        ok = False
    else:
        checks["scrape_errors"] = "ok"

    # Search subsystem — reported, non-fatal for external provider backends.
    # (A missing subsystem is a local configuration problem: fatal.)
    search_cache = getattr(request.app.state, "search_cache", None)
    if search_cache is None:
        checks["search"] = "unavailable"
        ok = False
    else:
        checks["search"] = await _search_health_report(search_cache)

    response = ReadinessResponse(status="ok" if ok else "degraded", checks=checks)
    if ok:
        return response
    return JSONResponse(status_code=503, content=response.model_dump())


# ================================================================= /ready


@router.get("/ready", response_model=ReadinessResponse, tags=["Health"])
async def readiness(
    settings: Settings = Depends(get_settings),
    cache: SqliteCache = Depends(get_cache),
):
    """Readiness probe — verifies config, cache, and scraper are all initialised."""
    checks: dict[str, str] = {}
    ok = True

    # Config loaded?
    if settings is None:
        checks["config"] = "unavailable"
        ok = False
    else:
        checks["config"] = "ok"

    # Cache reachable?
    try:
        cache.stats()
        checks["cache"] = "ok"
    except Exception as exc:
        checks["cache"] = f"error: {exc}"
        ok = False

    # Scraper version info could be added here.
    checks["version"] = "1.0.0"

    if not ok:
        return ReadinessResponse(status="degraded", checks=checks)
    return ReadinessResponse(status="ok", checks=checks)


# =============================================================== /metrics


@router.get("/metrics", tags=["Monitoring"])
async def metrics():
    """Prometheus-style text metrics."""
    collector = get_metrics()
    return collector.render()


# ============================================================== /v1/scrape


@router.post("/v1/scrape", response_model=ScrapeResponse, tags=["Scrape"])
async def scrape_url(
    request: ScrapeRequest,
    scraper: ScraperService = Depends(get_scraper),
    metrics_collector: MetricsCollector = Depends(get_metrics),
    auth_context: AuthContext | None = Depends(get_auth_context),
    expose_profile: bool = Depends(get_expose_profile),
):
    """Scrape a single URL and return its rendered HTML."""
    metrics_collector.inc("scrape_requests_total")
    start = time.monotonic()

    try:
        # Determine effective mode: request explicit > profile overrides > global.
        result = await scraper.scrape(
            url=request.url,
            mode=request.mode,
            cache_ttl_seconds=request.cache_ttl_seconds,
            force_refresh=request.force_refresh,
            wait_until=request.wait_until,
            wait_selector=request.wait_selector,
            timeout_seconds=request.timeout_seconds,
            scroll_config=request.scroll.model_dump(),
            debug_config=request.debug.model_dump(),
            proxy_config=request.proxy.model_dump(),
            cookie_config=request.cookies.model_dump(),
            header_config=request.headers.model_dump(),
        )

        elapsed = int((time.monotonic() - start) * 1000)
        metrics_collector.inc("scrape_success_total")
        metrics_collector.mark_scrape_success()
        metrics_collector.observe_latency(elapsed)

        if result.get("from_cache"):
            metrics_collector.inc("cache_hits_total")
            if result.get("stale"):
                metrics_collector.inc("cache_hits_total")  # also counts as hit
        else:
            metrics_collector.inc("cache_misses_total")

        if request.extract.enabled and request.extract.fields:
            metrics_collector.inc("extraction_requests_total")

        # Apply response-time transforms after cache/fetch. Normalisation never
        # modifies the cached raw HTML.
        result = process_scrape_response(
            result,
            normalize_config=request.normalize.model_dump(),
            extract_config=request.extract,
        )
        if request.extract.enabled and request.extract.fields:
            if result.get("extraction_error"):
                metrics_collector.inc("extraction_error_total")
            else:
                metrics_collector.inc("extraction_success_total")

        # Fill in elapsed time in metadata.
        if "metadata" in result:
            result["metadata"]["elapsed_ms"] = elapsed

        # Optionally attach auth profile name to metadata.
        result = _maybe_add_auth_profile(result, auth_context, expose_profile)

        return result

    except ScraperError as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        metrics_collector.inc("scrape_error_total")
        metrics_collector.mark_scrape_error()
        metrics_collector.observe_latency(elapsed)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_dict(),
        )


# ============================================================== /v2/scrape


@router.post("/v2/scrape", response_model=V2ScrapeResponse, tags=["Scrape"])
async def scrape_url_v2(
    request: V2ScrapeRequest,
    scraper: ScraperService = Depends(get_scraper),
    metrics_collector: MetricsCollector = Depends(get_metrics),
    auth_context: AuthContext | None = Depends(get_auth_context),
    expose_profile: bool = Depends(get_expose_profile),
):
    """Scrape a single URL and return content in the requested format."""
    metrics_collector.inc("scrape_requests_total")
    start = time.monotonic()

    try:
        result = await scraper.scrape(
            url=request.url,
            mode=request.mode,
            cache_ttl_seconds=request.cache_ttl_seconds,
            force_refresh=request.force_refresh,
            wait_until=request.wait_until,
            wait_selector=request.wait_selector,
            timeout_seconds=request.timeout_seconds,
            scroll_config=request.scroll.model_dump(),
            debug_config=request.debug.model_dump(),
            proxy_config=request.proxy.model_dump(),
            cookie_config=request.cookies.model_dump(),
            header_config=request.headers.model_dump(),
        )

        elapsed = int((time.monotonic() - start) * 1000)
        metrics_collector.inc("scrape_success_total")
        metrics_collector.mark_scrape_success()
        metrics_collector.observe_latency(elapsed)

        if result.get("from_cache"):
            metrics_collector.inc("cache_hits_total")
            if result.get("stale"):
                metrics_collector.inc("cache_hits_total")
        else:
            metrics_collector.inc("cache_misses_total")

        if request.extract.enabled and request.extract.fields:
            metrics_collector.inc("extraction_requests_total")

        result = process_scrape_response(
            result,
            normalize_config=request.normalize.model_dump(),
            extract_config=request.extract,
        )
        if request.extract.enabled and request.extract.fields:
            if result.get("extraction_error"):
                metrics_collector.inc("extraction_error_total")
            else:
                metrics_collector.inc("extraction_success_total")

        result = format_scrape_content(result, request.response_format)

        if "metadata" in result:
            result["metadata"]["elapsed_ms"] = elapsed

        result = _maybe_add_auth_profile(result, auth_context, expose_profile)

        return result

    except ScraperError as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        metrics_collector.inc("scrape_error_total")
        metrics_collector.mark_scrape_error()
        metrics_collector.observe_latency(elapsed)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_dict(),
        )


# =============================================================== /v1/search


@router.post("/v1/search", response_model=SearchApiResponse, tags=["Search"])
async def search(
    request: SearchRequest,
    search_cache: SearchCache = Depends(get_search_cache),
    metrics_collector: MetricsCollector = Depends(get_metrics),
    auth_context: AuthContext | None = Depends(get_auth_context),
    expose_profile: bool = Depends(get_expose_profile),
):
    """Run a provider-agnostic search.

    The request is validated before any provider is contacted, then served
    through the shared :class:`app.search.SearchCache` (result cache above
    the router, all providers share it).  ``provider: auto`` is the default;
    an explicit provider name targets one provider without fallback.

    The response is a service-owned envelope (results + pagination +
    metadata) — never a raw provider payload.
    """
    metrics_collector.inc("search_requests_total")
    start = time.monotonic()

    try:
        response = await search_cache.search(request.to_domain())
    except ScraperError as exc:
        elapsed = int((time.monotonic() - start) * 1000)
        metrics_collector.inc("search_error_total")
        metrics_collector.observe_search_latency(elapsed)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_dict(),
        )

    elapsed = int((time.monotonic() - start) * 1000)
    metrics_collector.inc("search_success_total")
    metrics_collector.observe_search_latency(elapsed)

    result = SearchApiResponse.from_domain(response)
    if expose_profile and auth_context is not None:
        result.metadata.auth_profile = auth_context.profile_name
    return result


# ================================================ /v1/search/capabilities


@router.get("/v1/search/capabilities", response_model=SearchCapabilitiesResponse, tags=["Search"])
async def search_capabilities(
    search_cache: SearchCache = Depends(get_search_cache),
):
    """Return service-level search capabilities.

    Exposes what clients, UIs and agents can rely on: enabled providers
    (in routing priority order), their normalized verticals, filters and
    discoverable sources, the default routing behaviour, and the result
    cache policy.  Provider capabilities are probed through the provider
    contract (bounded by the router probe timeout); a provider's own
    discovery endpoint (e.g. SearXNG ``/config``) is never proxied.
    """
    router = search_cache.router
    probes: list[tuple[str, SearchCapabilities | None]] = [
        (provider.name, await router.probe_capabilities(provider))
        for provider in router.ordered_candidates()
    ]
    return SearchCapabilitiesResponse.from_router(
        router,
        probes,
        cache_enabled=search_cache.config.enabled,
        cache_default_ttl_seconds=search_cache.config.default_ttl_seconds,
        cache_stale_if_error=search_cache.config.stale_if_error,
    )


# ========================================================== /v1/scrape/batch


@router.post("/v1/scrape/batch", response_model=BatchScrapeResponse, tags=["Scrape"])
async def scrape_batch(
    request: BatchScrapeRequest,
    scraper: ScraperService = Depends(get_scraper),
    effective_settings: Settings = Depends(get_effective_settings),
    metrics_collector: MetricsCollector = Depends(get_metrics),
    auth_context: AuthContext | None = Depends(get_auth_context),
    expose_profile: bool = Depends(get_expose_profile),
):
    """Scrape multiple URLs with controlled concurrency."""
    start = time.monotonic()

    effective_concurrency = min(request.max_concurrency, effective_settings.scraper_max_concurrency)
    semaphore = asyncio.Semaphore(effective_concurrency)
    succeeded = 0
    failed = 0
    results: list = []

    async def _scrape_one(item: BatchItem) -> dict:
        nonlocal succeeded, failed
        async with semaphore:
            try:
                result = await scraper.scrape(
                    url=item.url,
                    mode=item.mode,
                    cache_ttl_seconds=item.cache_ttl_seconds,
                    force_refresh=item.force_refresh,
                    wait_until=item.wait_until,
                    wait_selector=item.wait_selector,
                    timeout_seconds=item.timeout_seconds,
                    scroll_config=item.scroll.model_dump(),
                    debug_config=item.debug.model_dump(),
                    proxy_config=item.proxy.model_dump(),
                    cookie_config=item.cookies.model_dump(),
                    header_config=item.headers.model_dump(),
                )
                if item.extract.enabled and item.extract.fields:
                    metrics_collector.inc("extraction_requests_total")

                result = process_scrape_response(
                    result,
                    normalize_config=item.normalize.model_dump(),
                    extract_config=item.extract,
                )
                if item.extract.enabled and item.extract.fields:
                    if result.get("extraction_error"):
                        metrics_collector.inc("extraction_error_total")
                    else:
                        metrics_collector.inc("extraction_success_total")

                # Optionally attach auth profile name.
                result = _maybe_add_auth_profile(result, auth_context, expose_profile)

                metrics_collector.inc("scrape_success_total")
                metrics_collector.mark_scrape_success()
                succeeded += 1
                return {"url": item.url, "success": True, "result": result, "error": None}
            except ScraperError as exc:
                metrics_collector.inc("scrape_error_total")
                metrics_collector.mark_scrape_error()
                failed += 1
                return {
                    "url": item.url,
                    "success": False,
                    "result": None,
                    "error": exc.to_dict(),
                }

    tasks = [_scrape_one(item) for item in request.items]
    results = await asyncio.gather(*tasks)

    elapsed = int((time.monotonic() - start) * 1000)
    return BatchScrapeResponse(
        results=results,
        total=len(request.items),
        succeeded=succeeded,
        failed=failed,
        elapsed_ms=elapsed,
    )


# ============================================================= /v1/cache/stats


@router.get("/v1/cache/stats", response_model=CacheStats, tags=["Cache"])
async def cache_stats(
    cache: SqliteCache = Depends(get_cache),
):
    """Return cache statistics (entries, size, expired count, both tables)."""
    stats = cache.stats()
    return CacheStats(
        total_entries=stats["total_entries"],
        total_size_bytes=stats["total_size_bytes"],
        expired_entries=stats["expired_entries"],
        cache_path=stats["cache_path"],
        search_total_entries=stats.get("search_total_entries", 0),
        search_total_size_bytes=stats.get("search_total_size_bytes", 0),
        search_expired_entries=stats.get("search_expired_entries", 0),
    )


# ============================================================== DELETE /v1/cache


@router.delete("/v1/cache", tags=["Cache"])
async def cache_delete(
    url: str = Query(..., description="URL to remove from cache"),
    cache: SqliteCache = Depends(get_cache),
):
    """Remove one URL from the cache."""
    deleted = cache.delete_by_url(url)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "cache_error",
                    "message": "URL not found in cache",
                    "details": {},
                }
            },
        )
    return {"message": f"Deleted {deleted} entry(s) for {url}"}


# ============================================================ /v1/cache/purge


@router.post("/v1/cache/purge", response_model=PurgeResponse, tags=["Cache"])
async def cache_purge(
    domain: str | None = Query(default=None, description="Optional domain scope"),
    cache: SqliteCache = Depends(get_cache),
):
    """Purge all (or domain-scoped) cache entries."""
    try:
        purged = cache.purge(domain=domain)
        msg = f"Purged {purged} entries" + (f" for domain '{domain}'" if domain else "")
        return PurgeResponse(purged_entries=purged, message=msg)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": {"type": "cache_error", "message": str(exc), "details": {}}},
        )


# ===================================================== POST /v1/cache/cleanup


@router.post("/v1/cache/cleanup", response_model=CacheCleanupResult, tags=["Cache"])
async def cache_cleanup(
    request: CacheCleanupRequest | None = None,
    cache: SqliteCache = Depends(get_cache),
    settings: Settings = Depends(get_settings),
    metrics_collector: MetricsCollector = Depends(get_metrics),
):
    """Run cache cleanup immediately with optional overrides.

    Returns structured stats about what was deleted.
    """
    try:
        params = request or CacheCleanupRequest()
        delete_expired_after = (
            params.delete_expired_after_seconds
            if params.delete_expired_after_seconds is not None
            else settings.cache_delete_expired_after_seconds
        )
        max_entries = (
            params.max_entries if params.max_entries is not None else settings.cache_max_entries
        )
        max_size_mb = (
            params.max_size_mb if params.max_size_mb is not None else settings.cache_max_size_mb
        )
        do_vacuum = params.vacuum if params.vacuum is not None else False

        result = cache.cleanup(
            delete_expired_after_seconds=delete_expired_after,
            max_entries=max_entries,
            max_size_bytes=max_size_mb * 1024 * 1024,
            vacuum=do_vacuum,
        )

        metrics_collector.inc("cache_cleanup_runs_total")
        metrics_collector.inc("cache_cleanup_deleted_entries_total", result.total_deleted)
        metrics_collector.set_gauge("cache_size_bytes", result.size_after_bytes)
        metrics_collector.set_gauge("cache_entries_total", result.entries_after)

        return result

    except Exception as exc:
        metrics_collector.inc("cache_cleanup_errors_total")
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "type": "cache_error",
                    "message": f"Cache cleanup failed: {exc}",
                    "details": {},
                }
            },
        )


# ====================================================== POST /v1/cache/vacuum


@router.post("/v1/cache/vacuum", response_model=CacheVacuumResult, tags=["Cache"])
async def cache_vacuum(
    cache: SqliteCache = Depends(get_cache),
    metrics_collector: MetricsCollector = Depends(get_metrics),
):
    """Run SQLite VACUUM to reclaim disk space.

    This can block writes and is I/O intensive — prefer running during
    maintenance windows on Raspberry Pi.
    """
    try:
        metrics_collector.inc("cache_vacuum_runs_total")
        result = cache.vacuum()
        metrics_collector.set_gauge("cache_size_bytes", result.size_after_bytes)
        return result
    except Exception as exc:
        metrics_collector.inc("cache_vacuum_errors_total")
        raise HTTPException(
            status_code=500,
            detail={
                "error": {
                    "type": "cache_error",
                    "message": f"VACUUM failed: {exc}",
                    "details": {},
                }
            },
        )


# ============================================================== /v1/jobs


def _job_to_response(
    job: Job,
    expose_profile: bool = False,
) -> dict:
    """Convert a Job model to a response dict (excludes config)."""
    resp = {
        "job_id": job.job_id,
        "status": job.status.value,
        "url": job.url,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "result": job.result,
        "error": job.error,
    }
    if expose_profile and job.profile_name:
        resp["profile_name"] = job.profile_name
    return resp


@router.post("/v1/jobs", response_model=JobResponse, tags=["Jobs"])
async def create_job(
    request: ScrapeRequest,
    jobs: JobService = Depends(get_job_service),
    auth_context: AuthContext | None = Depends(get_auth_context),
    expose_profile: bool = Depends(get_expose_profile),
    effective_settings: Settings = Depends(get_effective_settings),
):
    """Create a new async scrape job.

    The request body is identical to ``/v1/scrape``.  The job is queued
    immediately and processed by a background worker.  Poll
    ``/v1/jobs/{job_id}`` to check progress.
    """
    if not jobs._settings.jobs_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "internal_error",
                    "message": "Async jobs are disabled",
                    "details": {},
                }
            },
        )

    scrape_config = {
        "url": request.url,
        "mode": request.mode,
        "cache_ttl_seconds": request.cache_ttl_seconds,
        "force_refresh": request.force_refresh,
        "wait_until": request.wait_until,
        "wait_selector": request.wait_selector,
        "timeout_seconds": request.timeout_seconds,
        "scroll_config": request.scroll.model_dump(),
        "debug_config": request.debug.model_dump(),
        "proxy_config": request.proxy.model_dump(),
        "cookie_config": request.cookies.model_dump(),
        "header_config": request.headers.model_dump(),
    }

    job = await jobs.create_job(
        url=request.url,
        scrape_config=scrape_config,
        normalize_config=request.normalize.model_dump(),
        extract_config=request.extract.model_dump() if request.extract.fields else None,
        profile_name=auth_context.profile_name if auth_context else None,
        effective_settings=effective_settings,
    )
    return _job_to_response(job, expose_profile=expose_profile)


@router.post("/v2/jobs", response_model=JobResponse, tags=["Jobs"])
async def create_job_v2(
    request: V2ScrapeRequest,
    jobs: JobService = Depends(get_job_service),
    auth_context: AuthContext | None = Depends(get_auth_context),
    expose_profile: bool = Depends(get_expose_profile),
    effective_settings: Settings = Depends(get_effective_settings),
):
    """Create a new async scrape job using the v2 response contract."""
    if not jobs._settings.jobs_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "internal_error",
                    "message": "Async jobs are disabled",
                    "details": {},
                }
            },
        )

    scrape_config = {
        "url": request.url,
        "mode": request.mode,
        "cache_ttl_seconds": request.cache_ttl_seconds,
        "force_refresh": request.force_refresh,
        "wait_until": request.wait_until,
        "wait_selector": request.wait_selector,
        "timeout_seconds": request.timeout_seconds,
        "scroll_config": request.scroll.model_dump(),
        "debug_config": request.debug.model_dump(),
        "proxy_config": request.proxy.model_dump(),
        "cookie_config": request.cookies.model_dump(),
        "header_config": request.headers.model_dump(),
    }

    job = await jobs.create_job(
        url=request.url,
        scrape_config=scrape_config,
        normalize_config=request.normalize.model_dump(),
        extract_config=request.extract.model_dump() if request.extract.fields else None,
        response_format=request.response_format,
        profile_name=auth_context.profile_name if auth_context else None,
        effective_settings=effective_settings,
    )
    return _job_to_response(job, expose_profile=expose_profile)


@router.get("/v2/jobs/{job_id}", response_model=JobResponse, tags=["Jobs"])
@router.get("/v1/jobs/{job_id}", response_model=JobResponse, tags=["Jobs"])
async def get_job(
    job_id: str,
    jobs: JobService = Depends(get_job_service),
    expose_profile: bool = Depends(get_expose_profile),
):
    """Return the current state and result of an async job."""
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "not_found",
                    "message": f"Job {job_id} not found",
                    "details": {},
                }
            },
        )
    return _job_to_response(job, expose_profile=expose_profile)


@router.get("/v2/jobs", response_model=JobListResponse, tags=["Jobs"])
@router.get("/v1/jobs", response_model=JobListResponse, tags=["Jobs"])
async def list_jobs(
    jobs: JobService = Depends(get_job_service),
    expose_profile: bool = Depends(get_expose_profile),
):
    """List all async jobs (newest first)."""
    job_list = jobs.list_jobs()
    responses = [_job_to_response(j, expose_profile=expose_profile) for j in job_list]
    return {"jobs": responses, "total": len(responses)}


@router.delete("/v2/jobs/{job_id}", tags=["Jobs"])
@router.delete("/v1/jobs/{job_id}", tags=["Jobs"])
async def delete_job(
    job_id: str,
    jobs: JobService = Depends(get_job_service),
):
    """Delete an async job from the store."""
    deleted = await jobs.delete_job(job_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "not_found",
                    "message": f"Job {job_id} not found",
                    "details": {},
                }
            },
        )
    return {"message": f"Job {job_id} deleted"}


@router.post("/v2/jobs/{job_id}/cancel", response_model=JobResponse, tags=["Jobs"])
@router.post("/v1/jobs/{job_id}/cancel", response_model=JobResponse, tags=["Jobs"])
async def cancel_job(
    job_id: str,
    jobs: JobService = Depends(get_job_service),
    expose_profile: bool = Depends(get_expose_profile),
):
    """Cancel a queued job.  No-op if the job is already running or finished."""
    job = await jobs.cancel_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "not_found",
                    "message": f"Job {job_id} not found",
                    "details": {},
                }
            },
        )
    return _job_to_response(job, expose_profile=expose_profile)
