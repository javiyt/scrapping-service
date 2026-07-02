"""URL validation, SSRF protection, and authentication helpers."""

import ipaddress
import re
import socket
from urllib.parse import urlparse, urlunparse

# -------------------------------------------------------------------- constants

BLOCKED_HOSTS: set = {
    "localhost",
    "127.0.0.1",
    "127.0.1.1",
    "::1",
    "0.0.0.0",
    "[::1]",
    "localhost6",
}

DOCKER_INTERNAL_HOSTS: set = {
    "host.docker.internal",
    "gateway.docker.internal",
    "dockerhost",
}

# Patterns for IP ranges that should never be scraped.
BLOCKED_IP_PATTERNS: list = [
    re.compile(r"^127\.\d+\.\d+\.\d+$"),  # loopback
    re.compile(r"^10\.\d+\.\d+\.\d+$"),  # RFC 1918 (10.0.0.0/8)
    re.compile(r"^172\.(1[6-9]|2\d|3[01])\.\d+\.\d+$"),  # RFC 1918 (172.16/12)
    re.compile(r"^192\.168\.\d+\.\d+$"),  # RFC 1918 (192.168/16)
    re.compile(r"^169\.254\.\d+\.\d+$"),  # link-local
    re.compile(r"^100\.\d+\.\d+\.\d+$"),  # CGNAT (RFC 6598)
    re.compile(r"^198\.18\.\d+\.\d+$"),  # benchmark
]

# Common metadata / internal hostnames.
BLOCKED_HOST_PATTERNS: list = [
    re.compile(r"^metadata\.google\.internal$", re.IGNORECASE),
    re.compile(r"^metadata\.google\.compute$", re.IGNORECASE),
    re.compile(r"^169\.254\.169\.254$"),  # cloud metadata IP literal
]


# ------------------------------------------------------------------ helpers


def is_private_ip(ip_str: str) -> bool:
    """Return ``True`` if *ip_str* is a private, reserved, or link-local address.

    Handles both IPv4 and IPv6.
    """
    try:
        addr = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def is_safe_url(
    url: str,
    block_private_ips: bool = True,
    block_localhost: bool = True,
    allowed_domains: list | None = None,
) -> bool:
    """Quick boolean check — returns ``True`` if the URL is safe to scrape."""
    valid, _ = validate_url(url, block_private_ips, block_localhost, allowed_domains)
    return valid


def validate_url(
    url: str,
    block_private_ips: bool = True,
    block_localhost: bool = True,
    allowed_domains: list | None = None,
) -> tuple[bool, str]:
    """Validate a URL for scraping safety.

    Returns:
        ``(True, "")`` on success, ``(False, "reason")`` on rejection.
    """
    if not url or not isinstance(url, str):
        return False, "URL must be a non-empty string"

    if len(url) > 8192:
        return False, "URL exceeds maximum length of 8192 characters"

    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"Failed to parse URL: {exc}"

    # -- scheme
    if not parsed.scheme:
        return False, "URL has no scheme — use http:// or https://"

    if parsed.scheme not in ("http", "https"):
        return False, f"Invalid scheme '{parsed.scheme}'; only http and https are allowed"

    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no hostname"

    if len(hostname) > 253:
        return False, "Hostname exceeds 253 characters"

    # -- localhost
    if block_localhost:
        if hostname.lower() in BLOCKED_HOSTS:
            return False, f"URL references a blocked localhost address: {hostname}"
        if hostname.lower() in DOCKER_INTERNAL_HOSTS:
            return False, f"URL references Docker internal host: {hostname}"

    # -- blocked hostname patterns
    for pat in BLOCKED_HOST_PATTERNS:
        if pat.match(hostname):
            return False, f"URL references a blocked hostname: {hostname}"

    # -- allowed domains
    if allowed_domains and hostname not in allowed_domains:
        return False, f"Domain '{hostname}' is not in the allowed-domains list"

    # -- DNS resolution + IP checks
    if block_private_ips:
        try:
            addrinfo = socket.getaddrinfo(hostname, 80, family=socket.AF_INET)
        except socket.gaierror:
            return False, f"Cannot resolve hostname: {hostname}"

        for _, _, _, _, sockaddr in addrinfo:
            ip = sockaddr[0]
            if is_private_ip(ip):
                return False, f"Hostname resolves to private/reserved IP: {ip}"

    return True, ""


def compute_domain(url: str) -> str | None:
    """Extract the domain from *url* (strips leading ``www.``)."""
    try:
        hostname = urlparse(url).hostname
        if hostname and hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname
    except Exception:
        return None


# ====================================================================== Proxy


VALID_PROXY_SCHEMES = frozenset({"http", "https", "socks5"})


def validate_proxy_url(
    url: str | None,
    block_private_hosts: bool = True,
) -> tuple[bool, str]:
    """Validate a proxy URL.

    Returns ``(True, "")`` on success or ``(False, "reason")`` on rejection.
    """
    if not url:
        return False, "Proxy URL must be a non-empty string"

    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"Failed to parse proxy URL: {exc}"

    # Scheme checks.
    scheme = parsed.scheme.lower() if parsed.scheme else ""
    if not scheme:
        return False, "Proxy URL has no scheme"
    if scheme == "file":
        return False, "file:// scheme is not allowed for proxy URLs"
    if scheme not in VALID_PROXY_SCHEMES:
        return (
            False,
            f"Invalid proxy scheme '{scheme}'; "
            f"must be one of: {', '.join(sorted(VALID_PROXY_SCHEMES))}",
        )

    hostname = parsed.hostname
    if not hostname:
        return False, "Proxy URL has no hostname"

    if len(hostname) > 253:
        return False, "Proxy hostname exceeds 253 characters"

    # Reject localhost proxy hosts.
    hostname_lower = hostname.lower()
    localhost_aliases: set = {
        "localhost",
        "127.0.0.1",
        "127.0.1.1",
        "::1",
        "0.0.0.0",
        "[::1]",
        "localhost6",
    }
    if hostname_lower in localhost_aliases or hostname_lower.endswith(".localhost"):
        return False, f"Proxy hostname '{hostname}' is a localhost address"

    # Reject private IP proxy hosts when block_private_hosts is True.
    if block_private_hosts:
        try:
            resolved = socket.getaddrinfo(hostname, 80, family=socket.AF_INET)
        except socket.gaierror:
            # Cannot resolve — skip the private-IP check rather than
            # rejecting a potentially valid external proxy hostname.
            pass
        else:
            for _, _, _, _, sockaddr in resolved:
                ip = sockaddr[0]
                if is_private_ip(ip):
                    return False, f"Proxy hostname resolves to private/reserved IP: {ip}"

    return True, ""


def redact_proxy_url(url: str | None) -> str | None:
    """Redact credentials from a proxy URL for safe logging.

    Example::

        >>> redact_proxy_url("http://user:pass@host:8080")
        "http://***:***@host:8080"
    """
    if not url:
        return url

    try:
        parsed = urlparse(url)
    except Exception:
        return url

    if parsed.username is None and parsed.password is None:
        return url

    # Reconstruct with redacted credentials: http://***:***@host:port/path
    hostname = parsed.hostname or ""
    port_str = f":{parsed.port}" if parsed.port is not None else ""
    netloc = f"***:***@{hostname}{port_str}"

    return urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )
