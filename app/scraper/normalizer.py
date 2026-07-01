"""URL normalisation utilities for consistent cache keys."""

import hashlib
from urllib.parse import urlparse, urlunparse


def normalize_url(url: str) -> str:
    """Normalise a URL for cache-key generation.

    - Lowercases scheme and hostname.
    - Removes default ports (80 for http, 443 for https).
    - Sorts query-string parameters.
    - Removes fragment.
    - Strips trailing slash on path (except for root).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return url

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower() if parsed.hostname else ""

    # Remove default ports.
    port = parsed.port
    if port is not None:
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            port = None
    netloc = hostname if port is None else f"{hostname}:{port}"

    # Sort query parameters.
    query = parsed.query
    if query:
        params = sorted(query.split("&"))
        query = "&".join(params)

    # Remove fragment.
    fragment = ""

    # Normalise path (remove trailing slash unless it's just "/").
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunparse((scheme, netloc, path, parsed.params, query, fragment))


def make_cache_key(url: str) -> str:
    """Return a stable SHA-256 hash for *url* to use as the cache primary key."""
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
