"""Crawl API schemas."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class CrawlRequest(BaseModel):
    """Request body for the ``/v1/crawl`` endpoint.

    Supports both start_urls (URL list) and sitemap_url (single URL)
    as entry points. At least one must be provided.
    """

    # Entry point: either start_urls or sitemap_url must be provided
    start_urls: list[str] | None = Field(
        default=None,
        description="List of starting URLs for the crawl.",
    )
    sitemap_url: str | None = Field(
        default=None,
        description="URL of a sitemap.xml or sitemap index to start from.",
    )

    # Crawl limits
    max_pages: int = Field(default=100, ge=1, description="Maximum pages to fetch.")
    max_depth: int = Field(default=5, ge=0, description="Maximum crawl depth.")

    # URL filtering
    allow: list[str] | None = Field(
        default=None,
        description="Glob patterns for URLs to include.",
    )
    deny: list[str] | None = Field(
        default=None,
        description="Glob patterns for URLs to exclude.",
    )
    same_domain_only: bool = Field(
        default=False,
        description="If true, keep only same-domain URLs (not subdomains).",
    )
    include_subdomains: bool = Field(
        default=False,
        description="If true, include subdomains (ignored if same_domain_only).",
    )
    allowed_extensions: list[str] | None = Field(
        default=None,
        description="List of file extensions to allow (without dot, e.g. ['html']).",
    )

    # Robots.txt compliance
    obey_robots_txt: bool = Field(
        default=True,
        description="If true, honor Disallow and Crawl-delay from robots.txt.",
    )

    # Per-page scrape options
    mode: str = Field(default="http", pattern=r"^(http|browser|auto)$")
    normalize: dict[str, Any] | None = None
    extract: dict[str, Any] | None = None
    output_format: Literal["html", "markdown", "text"] = Field(default="html")
    timeout_seconds: int = Field(default=60, ge=5, le=300)
    cookies: dict[str, Any] | None = None
    headers: dict[str, Any] | None = None
    proxy: dict[str, Any] | None = None
    impersonate: str | None = None

    @model_validator(mode="after")
    def _check_entry_points(self) -> CrawlRequest:
        if self.start_urls is None and self.sitemap_url is None:
            raise ValueError("At least one of start_urls or sitemap_url must be provided")
        if self.start_urls is not None and len(self.start_urls) == 0:
            raise ValueError("start_urls must not be an empty list")
        if self.sitemap_url is not None and self.start_urls is not None:
            # Allow both for flexibility, but prefer one or the other
            pass
        return self


class CrawlSummary(BaseModel):
    """Summary of crawl progress and results."""

    fetched: int = 0
    skipped_robots: int = 0
    skipped_filter: int = 0
    failed: int = 0
    total_urls: int = 0


class CrawlJobResponse(BaseModel):
    """Response for crawl job status."""

    job_id: str
    status: str  # queued, running, succeeded, failed, cancelled
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    started_at: str | None = None
    finished_at: str | None = None
    summary: CrawlSummary = Field(default_factory=CrawlSummary)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class CrawlPage(BaseModel):
    """One page result from a crawl."""

    url: str
    status_code: int
    success: bool
    html: str | None = None
    markdown: str | None = None
    error: str | None = None
    links_found: int = 0
    links_enqueued: int = 0
    metadata: dict[str, Any] | None = None


class CrawlPageListResponse(BaseModel):
    """Paginated page results from a crawl."""

    pages: list[CrawlPage]
    total: int
    offset: int
    limit: int


# ============================================================ Cancel/Resume


class CrawlCancelResponse(BaseModel):
    """Response for crawl cancel request."""

    job_id: str
    status: str


class CrawlResumeRequest(BaseModel):
    """Request body for crawl resume."""


class CrawlResumeResponse(BaseModel):
    """Response for crawl resume request."""

    job_id: str
    status: str
    resumed_from_state: bool
