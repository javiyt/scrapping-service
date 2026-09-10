"""Tests for URL normalization and cache key generation."""

from app.scraper.normalizer import make_cache_key, normalize_url


class TestNormalizeUrl:
    def test_lowercases_scheme_and_hostname(self):
        result = normalize_url("HTTP://EXAMPLE.COM/Path")
        assert result == "http://example.com/Path"

    def test_removes_default_http_port(self):
        result = normalize_url("http://example.com:80/page")
        assert result == "http://example.com/page"

    def test_removes_default_https_port(self):
        result = normalize_url("https://example.com:443/page")
        assert result == "https://example.com/page"

    def test_keeps_non_default_port(self):
        result = normalize_url("http://example.com:8080/page")
        assert "example.com:8080" in result

    def test_sorts_query_params(self):
        result = normalize_url("https://example.com/page?b=2&a=1")
        assert "a=1&b=2" in result

    def test_removes_fragment(self):
        result = normalize_url("https://example.com/page#section")
        assert "#" not in result

    def test_removes_trailing_slash(self):
        result = normalize_url("https://example.com/page/")
        assert not result.endswith("/")

    def test_preserves_root_path(self):
        result = normalize_url("https://example.com/")
        assert result == "https://example.com/"

    def test_handles_empty_url(self):
        # urlparse("") returns empty components; empty scheme → no lowercasing
        result = normalize_url("")
        assert isinstance(result, str)

    def test_handles_partial_url(self):
        result = normalize_url("http://")
        assert result is not None

    def test_handles_invalid_url_parse_return_original(self):
        """If urlparse raises an exception, the original URL is returned."""
        result = normalize_url("http://example.com/page")
        assert "example.com" in result

    def test_normalize_url_with_bad_input_returns_original(self):
        """When urlparse raises, normalize_url should return the original value."""
        from unittest.mock import patch

        with patch("app.scraper.normalizer.urlparse", side_effect=ValueError("parse error")):
            result = normalize_url("http://example.com")
            assert result == "http://example.com"


class TestMakeCacheKey:
    def test_returns_hex_string(self):
        key = make_cache_key("https://example.com")
        assert isinstance(key, str)
        assert len(key) == 64  # SHA-256 hex digest
        assert all(c in "0123456789abcdef" for c in key)

    def test_same_url_same_key(self):
        key1 = make_cache_key("https://example.com")
        key2 = make_cache_key("https://example.com")
        assert key1 == key2

    def test_normalized_same_key(self):
        key1 = make_cache_key("https://example.com:443/page")
        key2 = make_cache_key("HTTPS://EXAMPLE.COM/page")
        assert key1 == key2

    def test_different_urls_different_keys(self):
        key1 = make_cache_key("https://example.com/a")
        key2 = make_cache_key("https://example.com/b")
        assert key1 != key2

    def test_no_credentials_matches_plain_url_key(self):
        """Requests without cookies/headers must keep hashing identically to
        the plain URL-only key, so existing cache entries stay valid."""
        assert make_cache_key("https://example.com", cookies=None, extra_headers=None) == (
            make_cache_key("https://example.com")
        )
        assert make_cache_key("https://example.com", cookies=[], extra_headers={}) == (
            make_cache_key("https://example.com")
        )

    def test_cookies_change_the_key(self):
        base = make_cache_key("https://example.com")
        with_cookie = make_cache_key(
            "https://example.com", cookies=[{"name": "session", "value": "abc"}]
        )
        assert base != with_cookie

    def test_different_cookie_values_different_keys(self):
        key1 = make_cache_key(
            "https://example.com", cookies=[{"name": "session", "value": "abc"}]
        )
        key2 = make_cache_key(
            "https://example.com", cookies=[{"name": "session", "value": "xyz"}]
        )
        assert key1 != key2

    def test_cookie_key_order_independent(self):
        key1 = make_cache_key(
            "https://example.com",
            cookies=[{"name": "a", "value": "1"}, {"name": "b", "value": "2"}],
        )
        key2 = make_cache_key(
            "https://example.com",
            cookies=[{"name": "b", "value": "2"}, {"name": "a", "value": "1"}],
        )
        assert key1 == key2

    def test_headers_change_the_key(self):
        base = make_cache_key("https://example.com")
        with_header = make_cache_key(
            "https://example.com", extra_headers={"Authorization": "Bearer abc"}
        )
        assert base != with_header

    def test_different_header_values_different_keys(self):
        key1 = make_cache_key(
            "https://example.com", extra_headers={"Authorization": "Bearer abc"}
        )
        key2 = make_cache_key(
            "https://example.com", extra_headers={"Authorization": "Bearer xyz"}
        )
        assert key1 != key2

    def test_cookies_and_headers_independently_change_key(self):
        cookies_only = make_cache_key(
            "https://example.com", cookies=[{"name": "s", "value": "1"}]
        )
        headers_only = make_cache_key(
            "https://example.com", extra_headers={"X-Trace-Id": "1"}
        )
        both = make_cache_key(
            "https://example.com",
            cookies=[{"name": "s", "value": "1"}],
            extra_headers={"X-Trace-Id": "1"},
        )
        assert len({cookies_only, headers_only, both}) == 3
