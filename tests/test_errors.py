"""Tests for structured error types."""

from app.core.errors import (
    BlockedError,
    BrowserError,
    CacheError,
    HttpError,
    InternalError,
    InvalidProviderResponseError,
    NoProviderAvailableError,
    ScraperError,
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
    SecurityError,
    TimeoutError,
    UnsupportedSearchCapabilityError,
    ValidationError,
)


class TestScraperError:
    def test_base_error(self):
        exc = ScraperError("test_error", "Something went wrong", {"key": "val"}, 400)
        assert exc.error_type == "test_error"
        assert exc.message == "Something went wrong"
        assert exc.details == {"key": "val"}
        assert exc.status_code == 400

    def test_to_dict(self):
        exc = ScraperError("test_error", "msg", {"key": "val"}, 400)
        d = exc.to_dict()
        assert d == {
            "error": {
                "type": "test_error",
                "message": "msg",
                "details": {"key": "val"},
            }
        }


class TestErrorSubclasses:
    def test_validation_error(self):
        exc = ValidationError("invalid param")
        assert exc.error_type == "validation_error"
        assert exc.status_code == 400

    def test_security_error(self):
        exc = SecurityError("blocked")
        assert exc.error_type == "security_error"
        assert exc.status_code == 403

    def test_timeout_error(self):
        exc = TimeoutError("timed out")
        assert exc.error_type == "timeout_error"
        assert exc.status_code == 504

    def test_blocked_error(self):
        exc = BlockedError("blocked")
        assert exc.error_type == "blocked_error"
        assert exc.status_code == 403

    def test_http_error(self):
        exc = HttpError("upstream 500")
        assert exc.error_type == "http_error"
        assert exc.status_code == 502

    def test_browser_error(self):
        exc = BrowserError("browser crash")
        assert exc.error_type == "browser_error"
        assert exc.status_code == 502

    def test_cache_error(self):
        exc = CacheError("cache full")
        assert exc.error_type == "cache_error"
        assert exc.status_code == 500

    def test_internal_error(self):
        exc = InternalError("unexpected")
        assert exc.error_type == "internal_error"
        assert exc.status_code == 500

    def test_validation_error_with_details(self):
        exc = ValidationError("bad field", {"field": "name"})
        assert exc.details == {"field": "name"}


class TestSearchErrors:
    def test_provider_unavailable(self):
        exc = SearchProviderUnavailableError("search backend down")
        assert exc.error_type == "search_provider_unavailable"
        assert exc.status_code == 503

    def test_provider_timeout(self):
        exc = SearchProviderTimeoutError("search timed out")
        assert exc.error_type == "search_provider_timeout"
        assert exc.status_code == 504

    def test_unsupported_search_capability(self):
        exc = UnsupportedSearchCapabilityError("no provider supports maps")
        assert exc.error_type == "unsupported_search_capability"
        assert exc.status_code == 400

    def test_invalid_provider_response(self):
        exc = InvalidProviderResponseError("unparseable provider payload")
        assert exc.error_type == "invalid_provider_response"
        assert exc.status_code == 502

    def test_no_provider_available(self):
        exc = NoProviderAvailableError("no search providers configured")
        assert exc.error_type == "no_provider_available"
        assert exc.status_code == 503

    def test_search_error_details_and_dict(self):
        exc = SearchProviderUnavailableError("down", {"provider": "example"})
        assert exc.details == {"provider": "example"}
        assert exc.to_dict() == {
            "error": {
                "type": "search_provider_unavailable",
                "message": "down",
                "details": {"provider": "example"},
            }
        }
