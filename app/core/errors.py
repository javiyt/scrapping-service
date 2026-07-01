"""Standard error types and structured JSON error responses.

Every API error follows the same shape::

    {
      "error": {
        "type": "validation_error",
        "message": "Human-readable description",
        "details": {}
      }
    }
"""

from typing import Any


class ScraperError(Exception):
    """Base exception for the scraper service."""

    def __init__(
        self,
        error_type: str,
        message: str,
        details: dict[str, Any] | None = None,
        status_code: int = 500,
    ) -> None:
        self.error_type = error_type
        self.message = message
        self.details = details or {}
        self.status_code = status_code
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "type": self.error_type,
                "message": self.message,
                "details": self.details,
            }
        }


class ValidationError(ScraperError):
    """Invalid request parameters."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("validation_error", message, details, 400)


class SecurityError(ScraperError):
    """URL rejected by SSRF or domain policies."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("security_error", message, details, 403)


class TimeoutError(ScraperError):
    """Request exceeded the configured timeout."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("timeout_error", message, details, 504)


class BlockedError(ScraperError):
    """Response appears blocked or empty."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("blocked_error", message, details, 403)


class HttpError(ScraperError):
    """Upstream HTTP request failed."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("http_error", message, details, 502)


class BrowserError(ScraperError):
    """Browser-based scraping failed."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("browser_error", message, details, 502)


class CacheError(ScraperError):
    """Cache backend error."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("cache_error", message, details, 500)


class InternalError(ScraperError):
    """Unexpected internal error."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("internal_error", message, details, 500)


# Mapping from exception class to the generic HTTP handler.
EXCEPTION_STATUS_MAP: dict[type, int] = {
    ValidationError: 400,
    SecurityError: 403,
    TimeoutError: 504,
    BlockedError: 403,
    HttpError: 502,
    BrowserError: 502,
    CacheError: 500,
    InternalError: 500,
}
