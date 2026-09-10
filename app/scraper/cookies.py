"""Cookie parsing helpers for per-request authenticated scraping."""

from __future__ import annotations

from http.cookies import SimpleCookie
from urllib.parse import urlparse


def parse_cookie_config(cookie_config: dict | None) -> list[dict]:
    """Parse request cookie configuration into browser-friendly cookie dicts."""
    if not cookie_config or not cookie_config.get("enabled", False):
        return []

    header = cookie_config.get("header")
    if header:
        return parse_cookie_header(header)

    netscape = cookie_config.get("netscape")
    if netscape:
        return parse_netscape_cookie_file(netscape)

    return []


def parse_cookie_header(header: str) -> list[dict]:
    """Parse a Cookie header value such as ``a=1; b=2``."""
    parsed = SimpleCookie()
    parsed.load(header)
    return [{"name": key, "value": morsel.value} for key, morsel in parsed.items()]


def parse_netscape_cookie_file(contents: str) -> list[dict]:
    """Parse Netscape cookies.txt content.

    Expected fields are: domain, include-subdomains, path, secure, expires,
    name, value. Comment lines and blank lines are ignored.
    """
    cookies: list[dict] = []
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            parts = line.split(None, 6)
        if len(parts) != 7:
            continue

        domain, include_subdomains, path, secure, expires, name, value = parts
        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "secure": secure.upper() == "TRUE",
            "include_subdomains": include_subdomains.upper() == "TRUE",
        }
        try:
            expiry = int(expires)
        except ValueError:
            expiry = 0
        if expiry > 0:
            cookie["expires"] = expiry
        cookies.append(cookie)

    return cookies


def cookies_for_url(cookies: list[dict], url: str) -> list[dict]:
    """Return only cookies that are applicable to *url*."""
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    if not hostname:
        return []

    filtered: list[dict] = []
    for cookie in cookies:
        domain = cookie.get("domain")
        if domain and not _domain_matches(hostname, str(domain), cookie.get("include_subdomains")):
            continue
        filtered.append(cookie)
    return filtered


def cookie_header_dict(cookies: list[dict]) -> dict[str, str]:
    """Convert cookies to the simple name/value mapping accepted by httpx."""
    return {str(cookie["name"]): str(cookie["value"]) for cookie in cookies}


def browser_cookie_dicts(cookies: list[dict]) -> list[dict]:
    """Return cookie dicts suitable for Botasaurus/CDP."""
    allowed = {"name", "value", "domain", "path", "secure", "expires"}
    return [{k: v for k, v in cookie.items() if k in allowed} for cookie in cookies]


def _domain_matches(hostname: str, cookie_domain: str, include_subdomains: bool | None) -> bool:
    normalized = cookie_domain.lower().lstrip(".").rstrip(".")
    if hostname == normalized:
        return True
    return bool(include_subdomains or cookie_domain.startswith(".")) and hostname.endswith(
        f".{normalized}"
    )
