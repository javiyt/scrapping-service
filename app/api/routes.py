"""FastAPI route definitions for the scraper API."""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.api.dependencies import (
    get_auth_context,
    get_cache,
    get_effective_settings,
    get_expose_profile,
    get_job_service,
    get_scraper,
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
from app.scraper.response_processing import format_scrape_content, process_scrape_response
from app.scraper.service import ScraperService

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


@health_router.get("/health", response_model=HealthResponse, tags=["Health"])
async def health():
    """Liveness check.  Always returns 200 when the service is running."""
    return HealthResponse()


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
        )

        elapsed = int((time.monotonic() - start) * 1000)
        metrics_collector.inc("scrape_success_total")
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
        )

        elapsed = int((time.monotonic() - start) * 1000)
        metrics_collector.inc("scrape_success_total")
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
        metrics_collector.observe_latency(elapsed)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_dict(),
        )


# ========================================================== /v1/scrape/batch


@router.post("/v1/scrape/batch", response_model=BatchScrapeResponse, tags=["Scrape"])
async def scrape_batch(
    request: BatchScrapeRequest,
    scraper: ScraperService = Depends(get_scraper),
    metrics_collector: MetricsCollector = Depends(get_metrics),
    auth_context: AuthContext | None = Depends(get_auth_context),
    expose_profile: bool = Depends(get_expose_profile),
):
    """Scrape multiple URLs with controlled concurrency."""
    start = time.monotonic()

    semaphore = asyncio.Semaphore(request.max_concurrency)
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
                succeeded += 1
                return {"url": item.url, "success": True, "result": result, "error": None}
            except ScraperError as exc:
                metrics_collector.inc("scrape_error_total")
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
    """Return cache statistics (entries, size, expired count)."""
    stats = cache.stats()
    return CacheStats(
        total_entries=stats["total_entries"],
        total_size_bytes=stats["total_size_bytes"],
        expired_entries=stats["expired_entries"],
        cache_path=stats["cache_path"],
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
