"""Request and response schemas for scraping endpoints."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


# =================================================================== Scroll
class ScrollConfig(BaseModel):
    """Scrolling behaviour for JavaScript-rendered pages."""

    enabled: bool = False
    max_scrolls: int = Field(default=5, ge=1, le=100)
    delay_ms: int = Field(default=1000, ge=100, le=30000)
    stop_when_no_growth: bool = True


# =================================================================== Debug
class DebugConfig(BaseModel):
    """Debug output controls."""

    screenshot: bool = False
    html_dump: bool = False


# ============================================================= ScrapeRequest
class ScrapeRequest(BaseModel):
    """Request body for the ``/v1/scrape`` endpoint."""

    url: str = Field(..., min_length=1, max_length=8192, description="Target URL to scrape")

    mode: str = Field(
        default="auto",
        pattern=r"^(http|browser|auto)$",
        description="Scraping mode: http, browser, or auto",
    )

    cache_ttl_seconds: int | None = Field(
        default=None,
        ge=60,
        le=2592000,
        description="Per-request cache TTL (60 s – 30 d). Overrides domain and global defaults.",
    )

    force_refresh: bool = Field(
        default=False,
        description="If true, bypass cache and fetch fresh content.",
    )

    wait_until: str = Field(
        default="networkidle",
        pattern=r"^(load|domcontentloaded|networkidle|networkidle0)$",
        description="Browser wait strategy (only relevant in browser mode).",
    )

    wait_selector: str | None = Field(
        default=None,
        max_length=500,
        description="CSS selector to wait for before returning (browser mode only).",
    )

    timeout_seconds: int = Field(
        default=45,
        ge=5,
        le=120,
        description="Maximum time to wait for the page to load.",
    )

    scroll: ScrollConfig = Field(default_factory=ScrollConfig)

    debug: DebugConfig = Field(default_factory=DebugConfig)

    @model_validator(mode="after")
    def _check_timeout_vs_mode(self) -> "ScrapeRequest":
        if self.timeout_seconds > 60 and self.mode == "browser":
            raise ValueError(
                "Timeout > 60 s is not recommended for browser mode — risk of resource exhaustion"
            )
        return self


# ========================================================= BatchScrapeRequest
class BatchItem(BaseModel):
    """One item inside a batch scrape request."""

    url: str = Field(..., min_length=1, max_length=8192)
    mode: str = Field(default="auto", pattern=r"^(http|browser|auto)$")
    cache_ttl_seconds: int | None = Field(default=None, ge=60, le=2592000)
    force_refresh: bool = False
    wait_until: str = Field(
        default="networkidle",
        pattern=r"^(load|domcontentloaded|networkidle|networkidle0)$",
    )
    wait_selector: str | None = Field(default=None, max_length=500)
    timeout_seconds: int = Field(default=45, ge=5, le=120)
    scroll: ScrollConfig = Field(default_factory=ScrollConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)


class BatchScrapeRequest(BaseModel):
    """Request body for the ``/v1/scrape/batch`` endpoint."""

    items: list[BatchItem] = Field(..., min_length=1, max_length=50)
    max_concurrency: int = Field(default=3, ge=1, le=10)


# ============================================================ ScrapeResponse
class Metadata(BaseModel):
    """Metadata attached to every scrape response."""

    mode: str
    elapsed_ms: int
    content_length: int
    cache_key: str


class ScrapeResponse(BaseModel):
    """Successful scrape response."""

    url: str
    final_url: str
    status_code: int
    from_cache: bool = False
    stale: bool = False
    fetched_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    expires_at: str | None = None
    html: str
    metadata: Metadata


# ========================================================== CacheStats
class CacheStats(BaseModel):
    """Cache statistics."""

    total_entries: int
    total_size_bytes: int
    expired_entries: int
    cache_path: str


# ============================================================ BatchResponse
class BatchResult(BaseModel):
    """One result within a batch scrape response."""

    url: str
    success: bool
    result: ScrapeResponse | None = None
    error: dict[str, Any] | None = None


class BatchScrapeResponse(BaseModel):
    """Response for ``/v1/scrape/batch``."""

    results: list[BatchResult]
    total: int
    succeeded: int
    failed: int
    elapsed_ms: int


# ============================================================ PurgeResponse
class PurgeResponse(BaseModel):
    """Response after cache purge."""

    purged_entries: int
    message: str


# ================================================================== Health
class HealthResponse(BaseModel):
    """Liveness check response."""

    status: str = "ok"
    version: str = "1.0.0"
    service: str = "scraper-api"


class ReadinessResponse(BaseModel):
    """Readiness check response."""

    status: str = "ok"
    checks: dict[str, Any] = Field(default_factory=dict)
