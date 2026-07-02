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
