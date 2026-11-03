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


class SearchProviderUnavailableError(ScraperError):
    """A search provider is unreachable or reports itself unavailable."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("search_provider_unavailable", message, details, 503)


class SearchProviderTimeoutError(ScraperError):
    """A search provider did not answer within the allowed time."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("search_provider_timeout", message, details, 504)


class UnsupportedSearchCapabilityError(ScraperError):
    """The request needs a capability no available provider supports."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("unsupported_search_capability", message, details, 400)


class InvalidProviderResponseError(ScraperError):
    """A search provider returned a payload that cannot be normalized."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("invalid_provider_response", message, details, 502)


class NoProviderAvailableError(ScraperError):
    """No configured search provider can serve the request."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("no_provider_available", message, details, 503)


class CamofoxError(ScraperError):
    """Base exception for the Camofox browser REST client.

    All Camofox-specific failures are translated into this family so that
    application code never handles raw upstream exceptions or REST payloads.
    Public messages are safe to render: they never contain the configured
    credentials, the service URL, or upstream response text (those are
    logged instead).
    """

    _error_type: str = "camofox_error"
    _status_code: int = 502

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(self._error_type, message, details, self._status_code)


class CamofoxUnavailableError(CamofoxError):
    """The Camofox service is unreachable or returned a server error."""

    _error_type = "camofox_unavailable"
    _status_code = 503


class CamofoxTimeoutError(CamofoxError):
    """A Camofox request did not complete within the allowed time."""

    _error_type = "camofox_timeout"
    _status_code = 504


class CamofoxAuthError(CamofoxError):
    """Camofox rejected the configured credentials (HTTP 401/403)."""

    _error_type = "camofox_auth_failed"


class CamofoxInvalidResponseError(CamofoxError):
    """Camofox returned a payload that cannot be parsed into project types."""

    _error_type = "camofox_invalid_response"


class CamofoxSessionExpiredError(CamofoxError):
    """The Camofox session no longer exists (idle cleanup or explicit close)."""

    _error_type = "camofox_session_expired"


class CamofoxTabNotFoundError(CamofoxError):
    """The requested tab does not exist for the session's user."""

    _error_type = "camofox_tab_not_found"


class CamofoxNavigationError(CamofoxError):
    """Navigation (URL or search macro) failed on the Camofox side."""

    _error_type = "camofox_navigation_failed"


class BrowserSessionError(ScraperError):
    """The requested interactive browser session does not exist.

    Raised for unknown session IDs, sessions owned by another profile, and
    sessions that have expired or were already closed.  The message never
    reveals whether an ID exists under a different profile.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("browser_session_error", message, details, 404)


class BrowserSessionExpiredError(ScraperError):
    """The interactive browser session has expired (idle TTL)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("browser_session_expired", message, details, 410)


class BrowserTabError(ScraperError):
    """The requested browser tab does not exist in the session."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("browser_tab_error", message, details, 404)


class BrowserSessionLimitError(ScraperError):
    """Too many concurrent interactive browser sessions for this client."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("browser_session_limit", message, details, 409)


class BrowserTabLimitError(ScraperError):
    """Too many open tabs in one interactive browser session."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("browser_tab_limit", message, details, 409)


class BrowserDisabledError(ScraperError):
    """The interactive browser API is not enabled or no provider is ready."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("browser_disabled", message, details, 503)


class EvaluateDisabledError(ScraperError):
    """JavaScript evaluation is not enabled for this client's profile."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("browser_evaluate_disabled", message, details, 403)


class BrowserProviderError(ScraperError):
    """The interactive browser backend provider failed."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("browser_provider_error", message, details, 502)


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
    SearchProviderUnavailableError: 503,
    SearchProviderTimeoutError: 504,
    UnsupportedSearchCapabilityError: 400,
    InvalidProviderResponseError: 502,
    NoProviderAvailableError: 503,
    CamofoxError: 502,
    CamofoxUnavailableError: 503,
    CamofoxTimeoutError: 504,
    CamofoxAuthError: 502,
    CamofoxInvalidResponseError: 502,
    CamofoxSessionExpiredError: 502,
    CamofoxTabNotFoundError: 502,
    CamofoxNavigationError: 502,
    BrowserSessionError: 404,
    BrowserSessionExpiredError: 410,
    BrowserTabError: 404,
    BrowserSessionLimitError: 409,
    BrowserTabLimitError: 409,
    BrowserDisabledError: 503,
    EvaluateDisabledError: 403,
    BrowserProviderError: 502,
    InternalError: 500,
}
