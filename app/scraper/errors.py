"""Crawl-specific error types and structured responses."""

from typing import Any

from app.core.errors import ScraperError


class CrawlError(ScraperError):
    """Base exception for crawl-related errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("crawl_error", message, details, 400)


class SitemapError(CrawlError):
    """Sitemap parsing or fetching failed."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("sitemap_error", message, details, 400)


class RobotsError(CrawlError):
    """Robots.txt parsing or compliance failed."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("robots_error", message, details, 403)


class CrawlLimitError(CrawlError):
    """Crawl aborted due to hard limits (pages, depth, duration)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("crawl_limit_exceeded", message, details, 400)


class CrawlCancelledError(CrawlError):
    """Crawl was cancelled by the user."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("crawl_cancelled", message, details, 408)


class CrawlResumeError(CrawlError):
    """Crawl cannot be resumed (state missing or invalid)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("crawl_resume_failed", message, details, 404)


class CrawlNotYetCompletedError(CrawlError):
    """Requested operation requires a completed crawl."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("crawl_not_completed", message, details, 409)


class ExtractionError(ScraperError):
    """Raised when a required field cannot be extracted.

    The exception is caught by the API route and converted to a structured
    ``extraction_error`` dict in the response body.
    """

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(
            "extraction_error",
            f"Extraction failed for '{field_name}': {message}",
            {"field": field_name, "message": message},
            400,
        )
        self.field_name = field_name
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        """Return the error as a serialisable dict."""
        return self.details
