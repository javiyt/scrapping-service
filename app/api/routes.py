"""FastAPI route definitions for the scraper API."""

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.api.dependencies import get_cache, get_scraper, get_settings, verify_api_key
from app.cache.models import CacheCleanupResult, CacheVacuumResult
from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import ScraperError
from app.metrics.prometheus import MetricsCollector, get_metrics
from app.schemas.health import HealthResponse, ReadinessResponse
from app.schemas.scrape import (
    BatchItem,
    BatchScrapeRequest,
    BatchScrapeResponse,
    CacheCleanupRequest,
    CacheStats,
    ExtractConfig,
    PurgeResponse,
    ScrapeRequest,
    ScrapeResponse,
)
from app.scraper.extractor import ExtractionError
from app.scraper.extractor import extract as run_extraction
from app.scraper.html_normalizer import normalize_html
from app.scraper.service import ScraperService

logger = logging.getLogger("scraper-api.routes")


# ------------------------------------------------------------------- helpers


def _normalize_response(
    result: dict,
    normalize_config: dict | None,
) -> dict:
    """Apply HTML normalisation to a copy of *result* (if enabled).

    Returns a new dict so that the caller's original data (including any
    module-level sample dicts used in tests) is never mutated.
    """
    if not normalize_config or not normalize_config.get("enabled"):
        return result

    raw_html = result.get("html", "")
    base_url = result.get("final_url", "")
    if not raw_html:
        return result

    normalized_html, applied = normalize_html(raw_html, base_url, **normalize_config)

    metadata = dict(result.get("metadata", {}))
    metadata["normalized"] = True
    metadata["normalization"] = applied
    metadata["content_length"] = len(normalized_html)

    return {
        **result,
        "html": normalized_html,
        "metadata": metadata,
    }


def _extract_response(
    result: dict,
    extract_config: ExtractConfig,
) -> dict:
    """Apply CSS-selector-based extraction to a response dict (if enabled).

    Extraction runs on whatever ``html`` the response currently holds —
    that is, after normalisation if it was applied, or raw HTML otherwise.

    Returns the result dict with ``extracted`` (and optionally
    ``extraction_error``) added.
    """
    if not extract_config.enabled or not extract_config.fields:
        return result

    html_to_extract = result.get("html", "")
    if not html_to_extract:
        return result

    fields = {name: field.model_dump() for name, field in extract_config.fields.items()}
    # Resolve base URL: explicit extract.base_url » final_url » request url
    base_url = extract_config.base_url or result.get("final_url") or result.get("url", "")

    try:
        extracted_data = run_extraction(html_to_extract, fields, base_url=base_url)
        return {**result, "extracted": extracted_data}
    except ExtractionError as exc:
        return {
            **result,
            "extracted": None,
            "extraction_error": exc.to_dict(),
        }


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

        # Apply HTML normalisation at response time (cache always stores raw).
        result = _normalize_response(result, request.normalize.model_dump())

        # Apply CSS-selector extraction (operates on whatever HTML the
        # response currently holds — normalised if enabled, raw otherwise).
        if request.extract.enabled and request.extract.fields:
            metrics_collector.inc("extraction_requests_total")
            result = _extract_response(result, request.extract)
            if result.get("extraction_error"):
                metrics_collector.inc("extraction_error_total")
            else:
                metrics_collector.inc("extraction_success_total")

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
                # Apply HTML normalisation per item at response time.
                result = _normalize_response(result, item.normalize.model_dump())

                # Apply extraction per item.
                if item.extract.enabled and item.extract.fields:
                    metrics_collector.inc("extraction_requests_total")
                    result = _extract_response(result, item.extract)
                    if result.get("extraction_error"):
                        metrics_collector.inc("extraction_error_total")
                    else:
                        metrics_collector.inc("extraction_success_total")

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
