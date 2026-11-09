"""Link extraction primitives for the scraper service.

This module provides standalone functions for extracting and normalizing
links from HTML, with support for allow/deny patterns, domain filtering,
and canonicalization.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse


class LinkExtractor(HTMLParser):
    """Extract all <a href> links from HTML without external dependencies."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for attr, value in attrs:
                if attr == "href" and value is not None:
                    self.links.append(value)


def extract_links(html: str) -> list[str]:
    """Extract all href attributes from <a> tags in HTML.

    Args:
        html: Raw HTML string.

    Returns:
        List of link strings as found in the HTML (unnormalized).
    """
    parser = LinkExtractor()
    parser.feed(html)
    return parser.links


def canonicalize_url(url: str) -> str:
    """Normalize a URL for deduplication and comparison.

    - scheme/host lowercased
    - default ports stripped (http:80 -> http, https:443 -> https)
    - trailing slash on root path kept (https://example.com/)
    - query string order preserved (do not sort)
    - fragment stripped
    - empty path becomes /

    Args:
        url: Input URL string.

    Returns:
        Canonical URL string.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    # Lowercase scheme and host
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    # Strip default ports
    if parsed.port:
        if parsed.scheme == "http" and parsed.port == 80:
            netloc = parsed.hostname
        elif parsed.scheme == "https" and parsed.port == 443:
            netloc = parsed.hostname

    # Ensure path is not empty
    path = parsed.path if parsed.path else "/"

    # Reconstruct without fragment
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def is_same_domain(url: str, base_domain: str) -> bool:
    """Check if a URL belongs to the same domain as base_domain.

    Args:
        url: URL to check.
        base_domain: Base domain (e.g., "example.com").

    Returns:
        True if the URL's hostname matches base_domain (ignoring subdomains).
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        return hostname.lower().endswith(base_domain.lower())
    except Exception:
        return False


def is_subdomain(url: str, base_domain: str) -> bool:
    """Check if a URL belongs to a subdomain of base_domain.

    Args:
        url: URL to check.
        base_domain: Base domain (e.g., "example.com").

    Returns:
        True if the URL's hostname is a subdomain of base_domain.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        hostname_lower = hostname.lower()
        base_lower = base_domain.lower()

        if hostname_lower == base_lower:
            return False
        if not hostname_lower.endswith("." + base_lower):
            return False
        # Must have at least one dot before base domain
        prefix = hostname_lower[: -len(base_lower) - 1]
        return bool(prefix)
    except Exception:
        return False


def _glob_to_re(pattern: str) -> str:
    """Convert a simple glob pattern to a regex.

    Supports:
    - *  matches any sequence of characters (except /)
    - ** matches any sequence (including /)
    - ?  matches any single character
    - [] character classes

    Args:
        pattern: Glob pattern.

    Returns:
        Compiled regex pattern string.
    """
    i = 0
    result = ""
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                result += ".*"
                i += 2
            else:
                result += "[^/]*"
                i += 1
        elif c == "?":
            result += "."
            i += 1
        elif c == "[":
            j = i + 1
            if j < len(pattern) and pattern[j] == "]":
                result += "\\["
                i += 2
                continue
            while j < len(pattern) and pattern[j] != "]":
                j += 1
            if j >= len(pattern):
                result += "\\["
                i += 1
                continue
            result += pattern[i : j + 1]
            i = j + 1
        elif c in ".^$+{}|()\\[]":
            result += "\\" + c
            i += 1
        else:
            result += c
            i += 1
    return result


def _compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    """Compile glob patterns to regex patterns."""
    return [re.compile(_glob_to_re(p)) for p in patterns]


def matches_pattern(url: str, patterns: list[str]) -> bool:
    """Check if URL matches any of the glob patterns.

    Args:
        url: URL to test.
        patterns: List of glob patterns.

    Returns:
        True if URL matches at least one pattern.
    """
    compiled = _compile_patterns(patterns)
    return any(p.search(url) is not None for p in compiled)


def filter_links(
    links: list[str],
    *,
    base_url: str,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    same_domain_only: bool = False,
    include_subdomains: bool = False,
    allowed_extensions: list[str] | None = None,
    css_selector: str | None = None,
    max_links: int | None = None,
) -> list[str]:
    """Filter and deduplicate links based on rules.

    Args:
        links: Raw extracted links.
        base_url: Base URL for resolving relative links.
        allow: Glob patterns for URLs to include. If None, all URLs allowed.
        deny: Glob patterns for URLs to exclude.
        same_domain_only: If True, keep only same-domain URLs (not subdomains).
        include_subdomains: If True, include subdomains (ignored if same_domain_only).
        allowed_extensions: List of file extensions to allow (without dot, e.g. ["html"]).
        css_selector: Optional CSS selector to scope link extraction.
        max_links: Maximum number of links to return.

    Returns:
        List of filtered, deduplicated, canonicalized URLs.
    """
    if css_selector:
        # For now, ignore CSS selector (requires BeautifulSoup)
        # Future: parse HTML with BS4 and apply selector
        pass

    seen: set[str] = set()
    result: list[str] = []

    allow_re = _compile_patterns(allow) if allow else None
    deny_re = _compile_patterns(deny) if deny else None
    ext_re = None
    if allowed_extensions:
        ext_re = re.compile(
            rf"\.({'|'.join(re.escape(e) for e in allowed_extensions)})$", re.IGNORECASE
        )

    for link in links:
        # Skip non-http(s) schemes
        if not link:
            continue
        if link.startswith(("mailto:", "javascript:", "data:", "ftp:", "tel:")):
            continue
        if link.startswith("#"):
            continue  # fragment-only link

        # Resolve relative URLs
        resolved = canonicalize_url(urljoin(base_url, link))

        # Check domain rules
        if same_domain_only:
            try:
                parsed = urlparse(resolved)
                base_parsed = urlparse(base_url)
                link_domain = parsed.hostname or ""
                base_domain = base_parsed.hostname or ""
                if not link_domain or not base_domain:
                    continue
                if link_domain != base_domain:
                    continue
            except Exception:
                continue

        if include_subdomains and not same_domain_only:
            try:
                parsed = urlparse(resolved)
                base_parsed = urlparse(base_url)
                link_domain = parsed.hostname or ""
                base_domain = base_parsed.hostname or ""
                if not link_domain or not base_domain:
                    continue
                if link_domain != base_domain and not link_domain.endswith("." + base_domain):
                    continue
            except Exception:
                continue

        # Apply allow/deny filters
        if allow_re and not matches_pattern(resolved, allow):
            continue
        if deny_re and matches_pattern(resolved, deny):
            continue

        # Extension filter
        if ext_re and not ext_re.search(resolved):
            continue

        # Deduplicate
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(resolved)

        if max_links and len(result) >= max_links:
            break

    return result
