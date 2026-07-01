"""FastAPI route definitions for the scraper API."""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.api.dependencies import get_cache, get_scraper, get_settings, verify_api_key
from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import ScraperError
from app.metrics.prometheus import MetricsCollector, get_metrics
from app.schemas.health import HealthResponse, ReadinessResponse
from app.schemas.scrape import (
    BatchItem,
    BatchScrapeRequest,
    BatchScrapeResponse,
    CacheStats,
    PurgeResponse,
    ScrapeRequest,
    ScrapeResponse,
)
from app.scraper.service import ScraperService

logger = logging.getLogger("scraper-api.routes")

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
):
    """Scrape a single URL and return its rendered HTML."""
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

        # Fill in elapsed time in metadata.
        if "metadata" in result:
            result["metadata"]["elapsed_ms"] = elapsed

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
                )
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
