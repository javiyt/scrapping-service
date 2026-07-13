"""HTTP(S) fetcher using the ``httpx`` library.

This is the simplest fetch strategy and works for any publicly-accessible
URL that does not require JavaScript rendering.
"""

import asyncio
import logging
import time

import httpx

from app.core.errors import HttpError, TimeoutError
from app.scraper.domain_policy import DomainRateLimiter

logger = logging.getLogger("scraper-api.fetcher.http")

# httpx timeout values we treat as definite timeouts (vs generic errors).
_HTTPX_TIMEOUT_EXCEPTIONS = (
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.TimeoutException,
)


class FetchResult:
    """Result from any fetcher implementation."""

    def __init__(
        self,
        html: str,
        status_code: int,
        final_url: str,
        headers: dict[str, str] | None = None,
        elapsed_ms: int = 0,
    ) -> None:
        self.html = html
        self.status_code = status_code
        self.final_url = final_url
        self.headers = headers or {}
        self.elapsed_ms = elapsed_ms


class HttpFetcher:
    """Simple HTTP fetcher using ``httpx``.

    Does **not** execute JavaScript.  Use :class:`BrowserFetcher` for JS-heavy
    pages.
    """

    def __init__(
        self,
        timeout_seconds: int = 90,
        max_concurrency: int = 1,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        proxy_url: str | None = None,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_concurrency = max_concurrency
        self._user_agent = user_agent
        self._proxy_url = proxy_url
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def fetch(
        self,
        url: str,
        timeout_seconds: int | None = None,
        domain_limiter: DomainRateLimiter | None = None,
        domain: str | None = None,
        proxy_url: str | None = None,
    ) -> FetchResult:
        """Perform an HTTP GET and return the response content.

        Args:
            url: The URL to fetch.
            timeout_seconds: Override the default timeout.
            domain_limiter: Optional rate limiter to check before sending.
            domain: Domain for rate-limiting purposes.

        Raises:
            TimeoutError: Request exceeded the timeout.
            HttpError: Non-2xx / connection error.
        """
        if domain_limiter and domain:
            await domain_limiter.wait_if_needed(domain)
            domain_limiter.acquire(domain)

        try:
            async with self._semaphore:
                return await self._do_fetch(url, timeout_seconds, proxy_url or self._proxy_url)
        finally:
            if domain_limiter and domain:
                domain_limiter.release(domain)

    async def _do_fetch(
        self,
        url: str,
        timeout_seconds: int | None = None,
        proxy_url: str | None = None,
    ) -> FetchResult:
        raw_timeout = timeout_seconds or self._timeout
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        # Use separate timeouts so a slow page download doesn't eat into
        # the connect or pool budget.
        timeout = httpx.Timeout(
            connect=min(15.0, raw_timeout * 0.2),
            read=raw_timeout * 0.7,  # bulk of budget goes to reading the body
            write=10.0,
            pool=10.0,
        )

        client_kwargs: dict = {
            "timeout": timeout,
            "follow_redirects": True,
            "max_redirects": 10,
        }
        if proxy_url:
            client_kwargs["proxies"] = proxy_url

        start = time.monotonic()
        logger.debug(
            "HTTP GET %s (timeout: connect=%.1f read=%.1f)",
            url,
            timeout.connect,
            timeout.read,
        )

        try:
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.get(url, headers=headers)
        except _HTTPX_TIMEOUT_EXCEPTIONS as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            proxy_info = " via proxy" if proxy_url else ""
            logger.error(
                "HTTP timeout fetching %s (%d ms)%s: %s",
                url,
                elapsed,
                proxy_info,
                exc,
            )
            raise TimeoutError(
                f"Request timed out after {raw_timeout}s fetching {url}",
                details={"url": url, "elapsed_ms": elapsed, "timeout_seconds": raw_timeout},
            ) from exc
        except httpx.HTTPError as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            proxy_info = " via proxy" if proxy_url else ""
            logger.error(
                "HTTP error fetching %s (%d ms)%s: %s",
                url,
                elapsed,
                proxy_info,
                exc,
            )
            raise HttpError(
                f"HTTP request failed: {exc}",
                details={"url": url, "elapsed_ms": elapsed, "error": str(exc)},
            ) from exc

        elapsed = int((time.monotonic() - start) * 1000)
        proxy_info = " via proxy" if proxy_url else ""
        logger.info(
            "HTTP fetch %s → %d (%d bytes, %d ms)%s",
            url,
            response.status_code,
            len(response.content),
            elapsed,
            proxy_info,
        )

        # Log non-2xx as warnings.
        if response.status_code >= 400:
            logger.warning(
                "Non-2xx response for %s: %d — body preview: %s",
                url,
                response.status_code,
                response.text[:500],
            )

        return FetchResult(
            html=response.text,
            status_code=response.status_code,
            final_url=str(response.url),
            headers=dict(response.headers),
            elapsed_ms=elapsed,
        )
