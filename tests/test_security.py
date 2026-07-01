"""Tests for URL validation and SSRF protection."""

from app.core.security import (
    compute_domain,
    is_private_ip,
    is_safe_url,
    validate_url,
)


class TestPrivateIpDetection:
    def test_private_ipv4(self):
        assert is_private_ip("10.0.0.1")
        assert is_private_ip("172.16.0.1")
        assert is_private_ip("192.168.1.1")
        assert is_private_ip("127.0.0.1")

    def test_public_ipv4(self):
        assert not is_private_ip("8.8.8.8")
        assert not is_private_ip("93.184.216.34")
        assert not is_private_ip("1.1.1.1")

    def test_link_local(self):
        assert is_private_ip("169.254.1.1")
        assert is_private_ip("169.254.254.254")

    def test_multicast(self):
        assert is_private_ip("224.0.0.1")
        assert is_private_ip("239.255.255.255")

    def test_ipv6_loopback(self):
        assert is_private_ip("::1")

    def test_invalid_input(self):
        assert not is_private_ip("not-an-ip")
        assert not is_private_ip("")


class TestUrlValidation:
    def test_accept_https_url(self):
        valid, reason = validate_url("https://example.com")
        assert valid, reason

    def test_accept_http_url(self):
        valid, reason = validate_url("http://example.com")
        assert valid, reason

    def test_reject_file_scheme(self):
        valid, reason = validate_url("file:///etc/passwd")
        assert not valid
        assert "scheme" in reason.lower()

    def test_reject_ftp_scheme(self):
        valid, reason = validate_url("ftp://example.com")
        assert not valid
        assert "scheme" in reason.lower()

    def test_reject_localhost(self):
        valid, reason = validate_url("http://localhost:8080/admin")
        assert not valid
        assert "localhost" in reason.lower()

    def test_reject_loopback_127(self):
        valid, reason = validate_url("http://127.0.0.1:8080/")
        assert not valid

    def test_reject_loopback_0(self):
        valid, reason = validate_url("http://0.0.0.0/")
        assert not valid

    def test_reject_docker_host(self):
        valid, reason = validate_url("http://host.docker.internal:8080/")
        assert not valid
        assert "docker" in reason.lower()

    def test_empty_url(self):
        valid, reason = validate_url("")
        assert not valid

    def test_too_long(self):
        valid, reason = validate_url("https://x.com/" + "a" * 9000)
        assert not valid
        assert "8192" in reason

    def test_no_hostname(self):
        valid, reason = validate_url("http:///path")
        assert not valid

    def test_no_scheme(self):
        valid, reason = validate_url("example.com")
        assert not valid
        assert "scheme" in reason.lower()

    def test_allowed_domains(self):
        valid, reason = validate_url(
            "https://allowed.com/page",
            allowed_domains=["allowed.com"],
        )
        assert valid, reason

    def test_reject_not_allowed_domain(self):
        valid, reason = validate_url(
            "https://evil.com/page",
            allowed_domains=["allowed.com"],
        )
        assert not valid

    def test_quick_check(self):
        assert is_safe_url("https://example.com")
        assert not is_safe_url("http://localhost:8080/")
        assert not is_safe_url("file:///etc/passwd")


class TestComputeDomain:
    def test_extracts_domain(self):
        assert compute_domain("https://example.com/path") == "example.com"

    def test_strips_www(self):
        assert compute_domain("https://www.example.com/path") == "example.com"

    def test_handles_port(self):
        assert compute_domain("https://example.com:8080/path") == "example.com"

    def test_returns_none_for_invalid(self):
        assert compute_domain("") is None
