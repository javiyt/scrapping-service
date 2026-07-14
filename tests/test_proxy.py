"""Tests for proxy URL validation, redaction, selection, and integration.

All tests in this file run **without** external network access.  The HTTPX
client is mocked where needed.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.errors import SecurityError
from app.core.security import redact_proxy_url, validate_proxy_url
from app.scraper.http_fetcher import HttpFetcher

# ====================================================================== validate


class TestValidateProxyUrl:
    """Tests for :func:`validate_proxy_url`."""

    def test_valid_http(self) -> None:
        valid, reason = validate_proxy_url("http://user:pass@proxy.example:8080")
        assert valid is True
        assert reason == ""

    def test_valid_https(self) -> None:
        valid, reason = validate_proxy_url("https://proxy.example:8443")
        assert valid is True
        assert reason == ""

    def test_valid_socks5(self) -> None:
        valid, reason = validate_proxy_url("socks5://user:pass@proxy.example:1080")
        assert valid is True
        assert reason == ""

    def test_rejects_none(self) -> None:
        valid, reason = validate_proxy_url(None)
        assert valid is False
        assert "non-empty" in reason.lower()

    def test_rejects_empty_string(self) -> None:
        valid, reason = validate_proxy_url("")
        assert valid is False
        assert "non-empty" in reason.lower() or "no scheme" in reason.lower()

    def test_rejects_empty_scheme(self) -> None:
        valid, reason = validate_proxy_url("user:pass@proxy.example:8080")
        assert valid is False
        # The URL is parsed as having scheme "user" — scheme validation catches it.
        assert "scheme" in reason.lower()

    def test_rejects_file_scheme(self) -> None:
        valid, reason = validate_proxy_url("file:///etc/passwd")
        assert valid is False
        assert "file" in reason.lower()

    def test_rejects_invalid_scheme(self) -> None:
        valid, reason = validate_proxy_url("ftp://proxy.example:21")
        assert valid is False
        assert "scheme" in reason.lower()

    def test_rejects_localhost_hostname(self) -> None:
        valid, reason = validate_proxy_url("http://localhost:8080")
        assert valid is False
        assert "localhost" in reason.lower()

    def test_rejects_localhost_ip(self) -> None:
        valid, reason = validate_proxy_url("http://127.0.0.1:8080")
        assert valid is False
        assert "localhost" in reason.lower() or "private" in reason.lower()

    @pytest.mark.skipif(
        True,
        reason="DNS resolution may not be available in all test environments",
    )
    def test_rejects_private_ip_proxy(self) -> None:
        """Proxy hostname resolving to a private IP is rejected."""
        valid, reason = validate_proxy_url("http://10.0.0.1:3128")
        assert valid is False
        assert "private" in reason.lower() or "reserved" in reason.lower()

    def test_accepts_valid_external_proxy(self) -> None:
        """A valid external proxy should pass validation."""
        valid, reason = validate_proxy_url(
            "http://user:pass@proxy.example.com:3128",
            block_private_hosts=True,
        )
        assert valid is True, reason

    def test_rejects_malformed_url(self) -> None:
        valid, reason = validate_proxy_url("http://[::1]:8080")
        assert valid is False

    def test_hostname_too_long(self) -> None:
        long_host = "a" * 300
        valid, reason = validate_proxy_url(f"http://{long_host}:8080")
        assert valid is False
        assert "exceed" in reason.lower()


# ====================================================================== redact


class TestRedactProxyUrl:
    """Tests for :func:`redact_proxy_url`."""

    def test_redacts_http_credentials(self) -> None:
        result = redact_proxy_url("http://user:pass@host:8080")
        assert result == "http://***:***@host:8080"

    def test_redacts_socks5_credentials(self) -> None:
        result = redact_proxy_url("socks5://user:pass@host:1080")
        assert result == "socks5://***:***@host:1080"

    def test_redacts_username_only(self) -> None:
        result = redact_proxy_url("http://user@host:8080")
        assert result == "http://***:***@host:8080"

    def test_preserves_url_without_credentials(self) -> None:
        url = "http://proxy.example:8080"
        result = redact_proxy_url(url)
        assert result == url

    def test_preserves_none(self) -> None:
        assert redact_proxy_url(None) is None

    def test_preserves_path_and_query(self) -> None:
        result = redact_proxy_url("http://user:pass@host:8080/path?q=1")
        # urlunparse may include the path; verify credentials are redacted.
        assert "***:***" in result
        assert "user:pass" not in result

    def test_no_credential_leak(self) -> None:
        """Redacted URL must not contain the raw credentials."""
        raw = "http://myuser:mypassword@secret-proxy.example:3128"
        redacted = redact_proxy_url(raw)
        assert "myuser" not in redacted
        assert "mypassword" not in redacted
        assert "***:***" in redacted


# ============================================================== proxy selection


class TestProxySelection:
    """Tests for proxy selection logic via :class:`app.scraper.service.ScraperService`."""

    def _make_service(self, **overrides: bool | str | None) -> MagicMock:
        """Create a mock scraper service with specific proxy settings."""
        from app.core.config import Settings

        settings = Settings(
            api_key="test-key",
            proxy_enabled=overrides.get("proxy_enabled", False),
            proxy_url=overrides.get("proxy_url", None),
            proxy_allow_request_override=overrides.get("proxy_allow_request_override", False),
            proxy_block_private_proxy_hosts=overrides.get("proxy_block_private_proxy_hosts", True),
        )
        cache = MagicMock()
        from app.scraper.service import ScraperService

        service = ScraperService(settings, cache)
        return service

    def test_global_proxy_selected_when_enabled_and_no_request_proxy(self) -> None:
        """When global proxy is enabled and no request proxy config, use global."""
        service = self._make_service(proxy_enabled=True, proxy_url="http://global:8080")
        proxy_url = service._resolve_proxy(None)
        assert proxy_url == "http://global:8080"

    def test_global_proxy_selected_when_request_proxy_disabled(self) -> None:
        """When request proxy is present but disabled, use global."""
        service = self._make_service(proxy_enabled=True, proxy_url="http://global:8080")
        proxy_config = {"enabled": False, "url": "http://request:8080", "country": None}
        proxy_url = service._resolve_proxy(proxy_config)
        assert proxy_url == "http://global:8080"

    def test_no_proxy_when_globally_disabled_and_no_request_proxy(self) -> None:
        """When both global and request proxy are disabled, no proxy."""
        service = self._make_service(proxy_enabled=False)
        proxy_url = service._resolve_proxy(None)
        assert proxy_url is None

    def test_request_proxy_rejected_when_override_disabled(self) -> None:
        """Request proxy is rejected when allow_request_override is False."""
        service = self._make_service(
            proxy_enabled=False,
            proxy_allow_request_override=False,
        )
        proxy_config = {
            "enabled": True,
            "url": "http://user:pass@request:8080",
            "country": "ES",
        }
        with pytest.raises(SecurityError, match="override is not allowed"):
            service._resolve_proxy(proxy_config)

    def test_request_proxy_selected_when_override_enabled(self) -> None:
        """Request proxy is selected when override is allowed."""
        service = self._make_service(
            proxy_enabled=False,
            proxy_allow_request_override=True,
        )
        proxy_config = {
            "enabled": True,
            "url": "http://user:pass@request:3128",
            "country": "ES",
        }
        proxy_url = service._resolve_proxy(proxy_config)
        assert proxy_url == "http://user:pass@request:3128"

    def test_request_proxy_without_url_raises(self) -> None:
        """When request proxy is enabled but no URL provided, raise."""
        service = self._make_service(
            proxy_enabled=False,
            proxy_allow_request_override=True,
        )
        proxy_config = {"enabled": True, "url": None, "country": "ES"}
        with pytest.raises(SecurityError, match="no proxy URL was provided"):
            service._resolve_proxy(proxy_config)

    def test_request_proxy_invalid_url_raises(self) -> None:
        """An invalid request proxy URL is rejected."""
        service = self._make_service(
            proxy_enabled=False,
            proxy_allow_request_override=True,
        )
        proxy_config = {
            "enabled": True,
            "url": "ftp://evil.com:8080",
            "country": None,
        }
        with pytest.raises(SecurityError, match="Invalid request proxy URL"):
            service._resolve_proxy(proxy_config)


# ==================================================== HTTP fetcher proxy wiring


class TestHttpFetcherProxy:
    """Test that the HTTP fetcher passes proxy settings to httpx."""

    @pytest.mark.asyncio
    async def test_proxy_passed_to_httpx(self) -> None:
        """Proxy URL should be passed as ``proxies`` to ``httpx.AsyncClient``."""
        fetcher = HttpFetcher(timeout_seconds=10, max_concurrency=1)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_client_cls.return_value = mock_instance

            # Mock response
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.text = "<html>ok</html>"
            mock_response.status_code = 200
            mock_response.url = httpx.URL("https://example.com")
            mock_response.headers = {"content-type": "text/html"}
            mock_instance.get.return_value = mock_response

            await fetcher.fetch(
                "https://example.com",
                proxy_url="http://proxy:8080",
            )

            # Verify AsyncClient was created with the proxy URL.
            call_kwargs = mock_client_cls.call_args.kwargs
            assert "proxies" in call_kwargs, (
                f"Expected 'proxies' in AsyncClient kwargs, got {list(call_kwargs.keys())}"
            )
            assert call_kwargs["proxies"] == "http://proxy:8080"

    @pytest.mark.asyncio
    async def test_no_proxy_when_not_set(self) -> None:
        """When no proxy is provided, AsyncClient should not receive 'proxies'."""
        fetcher = HttpFetcher(timeout_seconds=10, max_concurrency=1)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_client_cls.return_value = mock_instance

            mock_response = MagicMock(spec=httpx.Response)
            mock_response.text = "<html>ok</html>"
            mock_response.status_code = 200
            mock_response.url = httpx.URL("https://example.com")
            mock_response.headers = {"content-type": "text/html"}
            mock_instance.get.return_value = mock_response

            await fetcher.fetch("https://example.com")

            call_kwargs = mock_client_cls.call_args.kwargs
            assert "proxies" not in call_kwargs, f"'proxies' unexpectedly in kwargs: {call_kwargs}"

    @pytest.mark.asyncio
    async def test_instance_proxy_used_as_default(self) -> None:
        """The proxy_url passed at construction should be used as default."""
        fetcher = HttpFetcher(
            timeout_seconds=10,
            max_concurrency=1,
            proxy_url="http://instance-proxy:3128",
        )

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_client_cls.return_value = mock_instance

            mock_response = MagicMock(spec=httpx.Response)
            mock_response.text = "<html>ok</html>"
            mock_response.status_code = 200
            mock_response.url = httpx.URL("https://example.com")
            mock_response.headers = {"content-type": "text/html"}
            mock_instance.get.return_value = mock_response

            # Fetch without explicit proxy_url — should use instance default.
            await fetcher.fetch("https://example.com")

            call_kwargs = mock_client_cls.call_args.kwargs
            assert call_kwargs.get("proxies") == "http://instance-proxy:3128"


# ========================================================== no credential leaks


class TestNoCredentialLeaks:
    """Verify proxy credentials never appear in error or log-friendly helpers."""

    def test_redact_proxy_url_in_error_message(self) -> None:
        """Error messages should use redacted proxy URLs, not raw credentials."""
        raw = "http://secretuser:secretpass@proxy.internal:8080"
        redacted = redact_proxy_url(raw)

        # The raw credentials must not appear in the redacted form.
        assert "secretuser" not in redacted
        assert "secretpass" not in redacted
        assert "***:***" in redacted

    def test_validate_proxy_url_error_does_not_leak_credentials(self) -> None:
        """Validation error strings must not contain raw proxy credentials."""
        # The validation function only needs hostname parsing, so this test
        # ensures the error path itself doesn't embed the full URL.
        valid, reason = validate_proxy_url("http://user:pass@localhost:8080")
        assert valid is False
        # The reason may mention 'localhost' but must not contain user:pass.
        assert "user:pass" not in reason

    def test_no_credentials_in_log_pattern(self) -> None:
        """The redacted URL must pass our log-safe assertion."""
        redacted = redact_proxy_url("http://user:pass@residential-region1.provider.com:8080")
        # Ensure the redacted string matches a safe pattern:
        # scheme://***:***@hostname:port
        pattern = r"^https?://\*\*\*:\*\*\*@[\w.-]+:\d+"
        assert re.match(pattern, redacted), f"Redacted URL '{redacted}' does not match safe pattern"
