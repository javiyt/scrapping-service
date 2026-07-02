"""Core scraper service — orchestrates caching, fetching, and domain policies."""

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.cache.models import CacheEntry
from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import (
    BrowserError,
    HttpError,
    InternalError,
    ScraperError,
    SecurityError,
    TimeoutError,
)
from app.core.security import (
    compute_domain,
    validate_proxy_url,
    validate_url,
)
from app.metrics.prometheus import get_metrics
from app.scraper.browser_fetcher import BrowserFetcher
from app.scraper.domain_policy import DomainRateLimiter
from app.scraper.http_fetcher import FetchResult, HttpFetcher
from app.scraper.normalizer import make_cache_key

logger = logging.getLogger("scraper-api.service")

# Minimum HTML body length (bytes) below which we consider the response
# blocked / empty and fall back to browser mode in ``auto``.
MIN_VALID_HTML_LENGTH = 500

# HTTP status codes that look like a block page rather than real content.
BLOCKED_STATUS_CODES = {403, 429, 503, 444}


class ScraperService:
    """Orchestrates URL scraping with caching, domain policies, and mode fallback.

    Typical flow:

    1. Validate and normalise the URL.
    2. Apply domain policy (rate limiting).
    3. Check cache (unless ``force_refresh``).
    4. Fetch via HTTP or browser, with auto-fallback.
    5. Store result in cache.
    6. Return structured response.
    """

    def __init__(self, settings: Settings, cache: SqliteCache) -> None:
        self.settings = settings
        self.cache = cache

        # Resolve global proxy URL.
        self._proxy_url: str | None = None
        if settings.proxy_enabled and settings.proxy_url:
            self._proxy_url = settings.proxy_url

        # HTTP fetcher
        self.http_fetcher = HttpFetcher(
            timeout_seconds=settings.scraper_timeout_seconds,
            max_concurrency=settings.scraper_max_concurrency,
            proxy_url=self._proxy_url,
        )

        # Browser fetcher (lazy — only instantiated if used)
        self._browser_fetcher: BrowserFetcher | None = None
        # Extract window size from arguments if present.
        window_size = (1366, 768)
        filtered_args = list(settings.browser_arguments)
        for a in list(filtered_args):
            if a.startswith("--window-size="):
                ws = a.split("=", 1)[1]
                window_size = tuple(int(x) for x in ws.split(","))
                filtered_args.remove(a)
        # Determine user agent based on the configured profile.
        ua_profile = settings.scraper_user_agent_profile
        if ua_profile == "mobile_es":
            user_agent = (
                "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Mobile Safari/537.36"
            )
        else:  # desktop_es (default)
            user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        self._browser_fetcher_args = {
            "headless": settings.scraper_headless,
            "arguments": filtered_args,
            "timeout_seconds": settings.scraper_timeout_seconds,
            "user_agent": user_agent,
            "window_size": window_size,
            "proxy_url": self._proxy_url,
        }

        # Domain rate limiter
        self.rate_limiter = DomainRateLimiter(settings)

        # Debug directory (optional — fail gracefully if unwritable)
        self.debug_dir = Path(settings.debug_dir)
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(
                "Cannot create debug directory %s — debug features disabled",
                self.debug_dir,
            )
            self.debug_dir = Path("/tmp/scraper-debug")
            self.debug_dir.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------- browser

    @property
    def browser_fetcher(self) -> BrowserFetcher:
        """Lazy-init browser fetcher (only created when first needed)."""
        if self._browser_fetcher is None:
            self._browser_fetcher = BrowserFetcher(**self._browser_fetcher_args)
        return self._browser_fetcher

    # --------------------------------------------------------------- scrape

    def _resolve_proxy(self, proxy_config: dict[str, Any] | None) -> str | None:
        """Resolve the effective proxy URL for a request.

        Priority:
        1. If request proxy is enabled and override is allowed → request proxy.
        2. If global proxy is enabled → global proxy.
        3. Otherwise → ``None`` (no proxy).

        Raises:
            SecurityError: Request proxy is enabled but override is not allowed.
            SecurityError: Request proxy URL is invalid.
        """
        request_enabled = proxy_config and proxy_config.get("enabled", False)
        request_url = proxy_config.get("url") if proxy_config else None

        # If no request proxy and global proxy is enabled, use the global one.
        if not request_enabled:
            return self._proxy_url

        # Request proxy is enabled — check override permission.
        if not self.settings.proxy_allow_request_override:
            raise SecurityError("Per-request proxy override is not allowed by server configuration")

        if not request_url:
            raise SecurityError("Proxy is enabled in the request but no proxy URL was provided")

        # Validate the request proxy URL.
        block_private = self.settings.proxy_block_private_proxy_hosts
        valid, reason = validate_proxy_url(request_url, block_private_hosts=block_private)
        if not valid:
            raise SecurityError(f"Invalid request proxy URL: {reason}")

        return request_url

    async def scrape(
        self,
        url: str,
        mode: str = "auto",
        cache_ttl_seconds: int | None = None,
        force_refresh: bool = False,
        wait_until: str = "networkidle",
        wait_selector: str | None = None,
        timeout_seconds: int | None = None,
        scroll_config: dict[str, Any] | None = None,
        debug_config: dict[str, Any] | None = None,
        proxy_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Scrape a single URL and return the result dictionary.

        Args:
            url: Target URL.
            mode: ``http``, ``browser``, or ``auto``.
            cache_ttl_seconds: Per-request cache TTL override.
            force_refresh: Skip cache and fetch fresh content.
            wait_until: Browser wait strategy.
            wait_selector: CSS selector to wait for (browser mode).
            timeout_seconds: Request timeout override.
            scroll_config: Scrolling configuration.
            debug_config: Debug output controls (``screenshot``, ``html_dump``).
            proxy_config: Per-request proxy configuration.

        Returns:
            Dict ready for JSON serialisation.
        """
        # ---- 0. Resolve proxy
        effective_proxy = self._resolve_proxy(proxy_config)
        metrics = get_metrics()

        # ---- 1. Validate URL
        block_private = self.settings.security_block_private_ips
        block_local = self.settings.security_block_localhost
        allowed_domains = self.settings.all_domain_names() or None

        valid, reason = validate_url(url, block_private, block_local, allowed_domains)
        if not valid:
            raise SecurityError(reason)

        # ---- 2. Domain
        domain = compute_domain(url) or ""
        domain_ttl = self.settings.get_domain_ttl(domain)

        # ---- 3. Cache check
        cache_key = make_cache_key(url)
        cached: Any = None

        if not force_refresh:
            cached = self.cache.get(cache_key)
            if cached is not None:
                expired = cached.is_expired
                # If stale-if-error is enabled and entry is not expired, return it.
                if not expired or self.settings.cache_stale_if_error:
                    # If it is expired but stale_if_error is on, we still try to
                    # fetch fresh; but if that fails we return the stale version.
                    if not expired:
                        logger.info("Cache HIT for %s", url)
                        return self._entry_to_response(cached, from_cache=True, stale=False)

        # ---- 4. Fetch
        result: FetchResult | None = None
        error: ScraperError | None = None
        used_mode = mode
        start = time.monotonic()

        if effective_proxy:
            metrics.inc("proxy_requests_total")

        try:
            if mode == "http":
                result = await self._fetch_http(
                    url,
                    timeout_seconds,
                    domain,
                    proxy_url=effective_proxy,
                )
            elif mode == "browser":
                result = await self._fetch_browser(
                    url,
                    timeout_seconds,
                    wait_until,
                    wait_selector,
                    scroll_config or {},
                    debug_config or {},
                    proxy_url=effective_proxy,
                )
            else:  # auto
                # Try HTTP first.
                try:
                    result = await self._fetch_http(
                        url, timeout_seconds, domain, proxy_url=effective_proxy
                    )
                    if self._looks_blocked(result):
                        logger.info(
                            "HTTP result looks blocked for %s — falling back to browser", url
                        )
                        result = await self._fetch_browser(
                            url,
                            timeout_seconds,
                            wait_until,
                            wait_selector,
                            scroll_config or {},
                            debug_config or {},
                            proxy_url=effective_proxy,
                        )
                        used_mode = "browser"
                    else:
                        used_mode = "http"
                except (HttpError, TimeoutError, ScraperError) as exc:
                    logger.info("HTTP fetch failed for %s — trying browser: %s", url, exc)
                    try:
                        result = await self._fetch_browser(
                            url,
                            timeout_seconds,
                            wait_until,
                            wait_selector,
                            scroll_config or {},
                            debug_config or {},
                            proxy_url=effective_proxy,
                        )
                        used_mode = "browser"
                    except (BrowserError, ImportError) as browser_exc:
                        # Both failed — propagate the original HTTP error.
                        raise exc from browser_exc
        except SecurityError as exc:
            error = exc
            metrics.inc("proxy_errors_total")
        except ScraperError as exc:
            error = exc
        except Exception as exc:
            error = InternalError(f"Unexpected error scraping {url}: {exc}")

        elapsed = int((time.monotonic() - start) * 1000)

        # ---- 5. Stale fallback
        if error is not None:
            if self.settings.cache_stale_if_error and cached is not None:
                logger.warning("Fetch failed, returning stale cache for %s", url)
                return self._entry_to_response(cached, from_cache=True, stale=True)
            raise error

        # ---- 6. Validate fetched content
        if result is None:
            raise InternalError(f"No result obtained for {url}")

        if self._looks_blocked(result) and used_mode != "browser":
            logger.warning(
                "Fetched result for %s looks blocked (status=%d, length=%d)",
                url,
                result.status_code,
                len(result.html),
            )

        # ---- 7. Cache the result
        ttl = cache_ttl_seconds or domain_ttl or self.settings.cache_default_ttl_seconds
        max_size = self.settings.cache_max_html_size_mb * 1024 * 1024

        html = result.html
        if len(html) > max_size:
            logger.warning("Truncating HTML for %s (%d bytes > %d limit)", url, len(html), max_size)
            html = html[:max_size]

        expires_at = datetime.now(UTC) + timedelta(seconds=ttl) if ttl > 0 else None
        entry = CacheEntry(
            cache_key=cache_key,
            url=url,
            final_url=result.final_url,
            status_code=result.status_code,
            html=html,
            fetched_at=datetime.now(UTC),
            expires_at=expires_at,
            mode=used_mode,
            content_length=len(html),
            headers=json.dumps(result.headers) if result.headers else None,
            error_metadata=None,
        )
        self.cache.set(entry)

        # ---- 8. Debug output
        self._maybe_write_debug(debug_config or {}, url, html, result)

        # ---- 9. Build response
        response = self._entry_to_response(entry, from_cache=False, stale=False)
        response["metadata"]["elapsed_ms"] = elapsed
        return response

    # --------------------------------------------------------------- helpers

    async def _fetch_http(
        self,
        url: str,
        timeout: int | None,
        domain: str,
        proxy_url: str | None = None,
    ) -> FetchResult:
        return await self.http_fetcher.fetch(
            url=url,
            timeout_seconds=timeout,
            domain_limiter=self.rate_limiter,
            domain=domain or None,
            proxy_url=proxy_url,
        )

    async def _fetch_browser(
        self,
        url: str,
        timeout: int | None,
        wait_until: str,
        wait_selector: str | None,
        scroll_config: dict[str, Any],
        debug_config: dict[str, Any],
        proxy_url: str | None = None,
    ) -> FetchResult:
        screenshot_path: str | None = None
        if debug_config.get("screenshot", False):
            ss_dir = self.debug_dir / "screenshots"
            ss_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = str(ss_dir / f"{int(time.time())}.png")

        return await self.browser_fetcher.fetch(
            url=url,
            timeout_seconds=timeout,
            wait_until=wait_until,
            wait_selector=wait_selector,
            scroll_config=scroll_config,
            screenshot_path=screenshot_path,
            proxy_url=proxy_url,
        )

    def _looks_blocked(self, result: FetchResult) -> bool:
        """Return ``True`` if the result looks like a block page or empty response."""
        if result.status_code in BLOCKED_STATUS_CODES:
            return True
        if len(result.html.strip()) < MIN_VALID_HTML_LENGTH:
            return True
        # Check for common block-page markers.
        lower = result.html.lower()
        block_signals = [
            "access denied",
            "captcha",
            "unusual traffic",
            "please complete the security check",
            "blocked",
            "cf-browser-verification",
            "challenge-platform",
        ]
        if any(signal in lower for signal in block_signals):
            return True
        return False

    def _maybe_write_debug(
        self,
        debug_config: dict[str, Any],
        url: str,
        html: str,
        result: FetchResult,
    ) -> None:
        if debug_config.get("html_dump", False):
            dump_dir = self.debug_dir / "html"
            dump_dir.mkdir(parents=True, exist_ok=True)
            safe_name = url.replace("://", "_").replace("/", "_").replace("?", "_")[:100]
            dump_path = dump_dir / f"{safe_name}.html"
            dump_path.write_text(html, encoding="utf-8")
            logger.info("HTML dump written to %s", dump_path)

    @staticmethod
    def _entry_to_response(entry: CacheEntry, from_cache: bool, stale: bool) -> dict[str, Any]:
        return {
            "url": entry.url,
            "final_url": entry.final_url,
            "status_code": entry.status_code,
            "from_cache": from_cache,
            "stale": stale,
            "fetched_at": entry.fetched_at.isoformat(),
            "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
            "html": entry.html,
            "metadata": {
                "mode": entry.mode,
                "elapsed_ms": 0,  # caller fills in
                "content_length": entry.content_length,
                "cache_key": entry.cache_key,
            },
        }
