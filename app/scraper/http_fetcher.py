"""HTTP(S) fetcher using the ``httpx`` library.

This is the simplest fetch strategy and works for any publicly-accessible
URL that does not require JavaScript rendering.
"""

import asyncio
import logging

import httpx

from app.scraper.domain_policy import DomainRateLimiter

logger = logging.getLogger("scraper-api.fetcher.http")


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
        timeout_seconds: int = 45,
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
        timeout = timeout_seconds or self._timeout
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }

        import time

        client_kwargs: dict = {
            "timeout": httpx.Timeout(timeout),
            "follow_redirects": True,
            "max_redirects": 10,
        }
        if proxy_url:
            client_kwargs["proxies"] = proxy_url

        start = time.monotonic()
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(url, headers=headers)

        elapsed = int((time.monotonic() - start) * 1000)
        proxy_info = " via proxy" if proxy_url else ""
        logger.info("HTTP fetch %s → %d (%d ms)%s", url, response.status_code, elapsed, proxy_info)

        return FetchResult(
            html=response.text,
            status_code=response.status_code,
            final_url=str(response.url),
            headers=dict(response.headers),
            elapsed_ms=elapsed,
        )
