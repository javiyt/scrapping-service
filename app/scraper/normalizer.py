"""URL normalisation utilities for consistent cache keys."""

import hashlib
import json
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


def make_cache_key(
    url: str,
    cookies: list[dict] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """Return a stable SHA-256 hash for *url* to use as the cache primary key.

    When per-request *cookies* or *extra_headers* are supplied, they are
    folded into the key (order-independent) so that requests carrying
    different credentials never collide on the same cache entry — a request
    with no credentials, and requests with different credentials, each get
    their own cache slot instead of either sharing one entry or bypassing
    the cache altogether. Requests without cookies/headers hash identically
    to the plain URL-only key used before this parameter existed.
    """
    normalized = normalize_url(url)
    parts = [normalized]

    if cookies:
        cookie_pairs = sorted((str(c.get("name")), str(c.get("value"))) for c in cookies)
        parts.append("cookies:" + json.dumps(cookie_pairs, sort_keys=True))

    if extra_headers:
        header_pairs = sorted(extra_headers.items())
        parts.append("headers:" + json.dumps(header_pairs, sort_keys=True))

    key_material = "\n".join(parts)
    return hashlib.sha256(key_material.encode("utf-8")).hexdigest()
