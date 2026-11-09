"""Scrape schemas for the scraper service."""

from app.schemas.crawl import (
    CrawlCancelResponse,
    CrawlJobResponse,
    CrawlPage,
    CrawlPageListResponse,
    CrawlRequest,
    CrawlResumeRequest,
    CrawlResumeResponse,
    CrawlSummary,
)
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
from app.schemas.search import SearchApiResponse, SearchCapabilitiesResponse, SearchRequest
from app.schemas.transcript import TranscriptApiResponse, TranscriptQueryParams

__all__ = [
    # Crawl
    "CrawlRequest",
    "CrawlJobResponse",
    "CrawlSummary",
    "CrawlPage",
    "CrawlPageListResponse",
    "CrawlCancelResponse",
    "CrawlResumeRequest",
    "CrawlResumeResponse",
    # Scrape
    "ScrapeRequest",
    "ScrapeResponse",
    "V2ScrapeRequest",
    "V2ScrapeResponse",
    "BatchScrapeRequest",
    "BatchScrapeResponse",
    "BatchItem",
    "CacheStats",
    "CacheCleanupRequest",
    "PurgeResponse",
    # Search
    "SearchRequest",
    "SearchApiResponse",
    "SearchCapabilitiesResponse",
    # Transcript
    "TranscriptQueryParams",
    "TranscriptApiResponse",
    # Health
    "HealthResponse",
    "ReadinessResponse",
]
