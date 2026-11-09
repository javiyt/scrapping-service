"""Sitemap parsing utilities."""

from __future__ import annotations

import gzip
from typing import NamedTuple

import httpx

from app.scraper.errors import SitemapError


class URLInfo(NamedTuple):
    """Information about a URL found in a sitemap."""

    loc: str
    lastmod: str | None = None
    changefreq: str | None = None
    priority: float | None = None


class SitemapResult(NamedTuple):
    """Result of sitemap parsing."""

    urls: list[URLInfo]
    subsitemaps: list[str]  # URLs to additional sitemap entries


def _is_gzip(data: bytes) -> bool:
    """Check if data starts with gzip magic bytes."""
    return data.startswith(b"\x1f\x8b")


def _parse_xml_sitemap(data: bytes, base_url: str) -> SitemapResult:
    """Parse a sitemap.xml or sitemap index XML.

    Handles both document types and returns:
    - urls: all <loc> entries with optional lastmod/changefreq/priority
    - subsitemaps: <loc> entries from <sitemap> tags (sitemap index)

    Raises SitemapError on malformed XML.
    """
    try:
        import xml.etree.ElementTree as ET
    except ImportError as exc:
        raise SitemapError("XML parser unavailable") from exc

    # Try gzip decompression first
    if _is_gzip(data):
        try:
            data = gzip.decompress(data)
        except gzip.BadGzipFile as exc:
            raise SitemapError("Invalid gzip sitemap") from exc

    # Handle potential BOM or encoding issues
    try:
        xml_str = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        xml_str = data.decode("latin-1", errors="replace")

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as exc:
        raise SitemapError(f"Malformed XML: {exc}") from exc

    # Strip namespace if present
    def _strip_ns(tag: str) -> str:
        return tag.split("}", 1)[-1] if tag.startswith("{") else tag

    # Detect document type by root element
    root_tag = _strip_ns(root.tag)

    urls: list[URLInfo] = []
    subsitemaps: list[str] = []

    if root_tag == "sitemapindex":
        # Sitemap index: extract <sitemap><loc>
        for child in root:
            child_tag = _strip_ns(child.tag)
            if child_tag == "sitemap":
                loc_elem = child.find("{*}loc")
                if loc_elem is not None and loc_elem.text:
                    subsitemaps.append(loc_elem.text)

    elif root_tag == "urlset":
        # URL set: extract <url><loc> etc.
        for child in root:
            child_tag = _strip_ns(child.tag)
            if child_tag == "url":
                loc_elem = child.find("{*}loc")
                if loc_elem is not None and loc_elem.text:
                    loc = loc_elem.text
                    lastmod = None
                    changefreq = None
                    priority = None

                    for elem in child:
                        elem_tag = _strip_ns(elem.tag)
                        if elem_tag == "lastmod" and elem.text:
                            lastmod = elem.text
                        elif elem_tag == "changefreq" and elem.text:
                            changefreq = elem.text
                        elif elem_tag == "priority" and elem.text:
                            try:
                                priority = float(elem.text)
                            except ValueError:
                                pass

                    urls.append(
                        URLInfo(loc=loc, lastmod=lastmod, changefreq=changefreq, priority=priority)
                    )

    else:
        # Unknown format: treat as URL set if we see <loc>
        for elem in root.iter():
            if _strip_ns(elem.tag) == "loc" and elem.text:
                urls.append(URLInfo(loc=elem.text))

    return SitemapResult(urls=urls, subsitemaps=subsitemaps)


def fetch_sitemap(
    url: str, client: httpx.Client | None = None, *, max_depth: int = 2
) -> SitemapResult:
    """Fetch and parse a sitemap, following index references.

    Args:
        url: Sitemap URL (may be index or URL set).
        client: Optional httpx.Client. If None, creates a new one.
        max_depth: Maximum recursion depth for sitemap index resolution.

    Returns:
        SitemapResult with all discovered URLs and subsitemap URLs.
    """
    if max_depth < 0:
        raise SitemapError("Maximum sitemap recursion depth exceeded")

    if client is None:
        client = httpx.Client(timeout=30.0)

    try:
        response = client.get(url, headers={"User-Agent": "ScraperService/1.0"})
        response.raise_for_status()
        data = response.content
    except httpx.RequestError as exc:
        raise SitemapError(f"Failed to fetch sitemap: {exc}") from exc

    try:
        result = _parse_xml_sitemap(data, url)
    except SitemapError:
        raise

    # Recursively fetch subsitemaps
    all_urls = list(result.urls)
    all_subsitemaps = list(result.subsitemaps)

    for subsitemap_url in result.subsitemaps:
        try:
            sub_result = fetch_sitemap(subsitemap_url, client, max_depth=max_depth - 1)
            all_urls.extend(sub_result.urls)
            all_subsitemaps.extend(sub_result.subsitemaps)
        except SitemapError:
            # Skip individual subsitemap failures
            continue

    # Deduplicate subsitemaps
    all_subsitemaps = list(dict.fromkeys(all_subsitemaps))

    return SitemapResult(urls=all_urls, subsitemaps=all_subsitemaps)


def parse_robots_txt(
    robots_content: str, base_url: str
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Parse robots.txt content.

    Returns:
        Tuple of (disallow_rules, allow_rules) where each is
        {user_agent_pattern: [paths]} and {user_agent_pattern: [paths]}.
    """
    # Minimal robots.txt parser
    # We only care about Disallow and Allow directives
    disallow_rules: dict[str, list[str]] = {}
    allow_rules: dict[str, list[str]] = {}

    current_agent: str | None = None

    lines = robots_content.splitlines()
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue

        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()

            if key == "user-agent":
                current_agent = value
                if current_agent not in disallow_rules:
                    disallow_rules[current_agent] = []
                    allow_rules[current_agent] = []
            elif key == "disallow" and current_agent is not None:
                disallow_rules[current_agent].append(value)
            elif key == "allow" and current_agent is not None:
                allow_rules[current_agent].append(value)
            elif key == "sitemap" and current_agent is None:
                # Global sitemap directive
                pass  # Caller can extract separately
            elif key == "crawl-delay" and current_agent is not None:
                pass  # Caller can extract separately

    return disallow_rules, allow_rules


def extract_sitemap_urls_from_robots(robots_content: str) -> list[str]:
    """Extract sitemap URLs from robots.txt."""
    sitemaps: list[str] = []
    for line in robots_content.splitlines():
        line = line.split("#", 1)[0].strip()
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url:
                sitemaps.append(url)
    return sitemaps
