"""HTTP fetcher using ``curl_cffi`` to impersonate real browsers.

This fetcher uses curl_cffi to mimic browser TLS/JA3/HTTP2 fingerprints,
allowing many sites that block plain httpx to be fetched without launching
a full browser.
"""

import asyncio
import logging
import time
from typing import Any

try:
    import curl_cffi
    from curl_cffi.requests import AsyncSession, BrowserType

    Impersonate = BrowserType  # type: ignore[assignment]
except ImportError:
    curl_cffi = None  # type: ignore[assignment]
    AsyncSession = None  # type: ignore[assignment]
    BrowserType = None  # type: ignore[assignment]
    Impersonate = None  # type: ignore[assignment]
    _CURL_CFFI_AVAILABLE = False
else:
    _CURL_CFFI_AVAILABLE = True

from app.core.errors import HttpError, SecurityError, TimeoutError
from app.scraper.cookies import cookie_header_dict
from app.scraper.domain_policy import DomainRateLimiter

logger = logging.getLogger("scraper-api.fetcher.impersonating")

# Mapping from user-facing profile names to curl_cffi browser types
_PROFILE_MAP: dict[str, str] = {
    "chrome": "chrome124",
    "chrome124": "chrome124",
    "chrome123": "chrome123",
    "chrome122": "chrome122",
    "chrome121": "chrome121",
    "chrome120": "chrome120",
    "chrome119": "chrome119",
    "chrome118": "chrome118",
    "chrome117": "chrome117",
    "chrome116": "chrome116",
    "chrome115": "chrome115",
    "firefox": "firefox128",
    "firefox128": "firefox128",
    "firefox127": "firefox127",
    "firefox126": "firefox126",
    "firefox125": "firefox125",
    "safari": "safari17_5",
    "safari17_5": "safari17_5",
    "safari17_2": "safari17_2",
    "safari17_0": "safari17_0",
    "safari16_5": "safari16_5",
    "safari16_4": "safari16_4",
    "edge": "edge124",
    "edge124": "edge124",
    "edge123": "edge123",
    "edge122": "edge122",
}


def _resolve_profile(profile: str | None) -> str | None:
    """Resolve a user-facing profile name to a curl_cffi impersonation value.

    Returns the resolved impersonation value, or None if profile is None
    (caller should fall back to plain httpx).
    Raises SecurityError if profile name is unknown.
    """
    if profile is None:
        return None

    lowered = profile.lower().strip()
    resolved = _PROFILE_MAP.get(lowered)

    if resolved is None:
        # Offer a helpful hint for common misspellings
        candidates = sorted(_PROFILE_MAP.keys())
        raise SecurityError(
            f"Unknown impersonation profile {profile!r}. Valid values: {', '.join(candidates)}"
        )

    return resolved


class FetchResult:
    """Result from the impersonating fetcher.

    Mirrors :class:`app.scraper.http_fetcher.FetchResult` structure
    so the scraper service can treat both fetchers identically.
    """

    def __init__(
        self,
        html: str,
        status_code: int,
        final_url: str,
        headers: dict[str, str] | None = None,
        elapsed_ms: int = 0,
        backend_name: str | None = None,
        backend_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.html = html
        self.status_code = status_code
        self.final_url = final_url
        self.headers = headers or {}
        self.elapsed_ms = elapsed_ms
        self.backend_name = backend_name
        self.backend_metadata = backend_metadata or {}


class ImpersonatingFetcher:
    """HTTP fetcher using curl_cffi with browser impersonation.

    Uses curl_cffi's impersonate feature to mimic real browser fingerprints,
    making it harder for sites to detect and block automated access.

    When ``impersonate`` is ``None``, use this only when curl_cffi is installed
    and the profile is known. Otherwise the caller should fall back to plain
    ``HttpFetcher``.
    """

    def __init__(
        self,
        timeout_seconds: int = 90,
        max_concurrency: int = 1,
        proxy_url: str | None = None,
    ) -> None:
        """Initialize the impersonating fetcher.

        Args:
            timeout_seconds: Default timeout for requests.
            max_concurrency: Maximum concurrent requests.
            proxy_url: Optional proxy URL (e.g. ``http://user:pass@host:port``).
        """
        self._timeout = timeout_seconds
        self._max_concurrency = max_concurrency
        self._proxy_url = proxy_url
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._initialized = False
        self._session: AsyncSession | None = None

        if not _CURL_CFFI_AVAILABLE:
            logger.warning(
                "curl_cffi not installed; impersonating fetcher will raise ImportError on fetch"
            )

    async def _ensure_session(self) -> AsyncSession:
        """Create and return an AsyncSession, lazy-init on first use."""
        if not self._session:
            self._session = AsyncSession(timeout=self._timeout)
            if self._proxy_url:
                self._session.proxies = {"all": self._proxy_url}
        return self._session

    async def fetch(
        self,
        url: str,
        timeout_seconds: int | None = None,
        domain_limiter: DomainRateLimiter | None = None,
        domain: str | None = None,
        proxy_url: str | None = None,
        cookies: list[dict] | None = None,
        extra_headers: dict[str, str] | None = None,
        impersonate: str | None = None,
    ) -> FetchResult:
        """Perform a fetch with browser fingerprint impersonation.

        Args:
            url: The URL to fetch.
            timeout_seconds: Override the default timeout.
            domain_limiter: Optional rate limiter to check before sending.
            domain: Domain for rate-limiting purposes.
            proxy_url: Per-request proxy override.
            cookies: List of cookie dicts (same format as HttpFetcher).
            extra_headers: Allowlisted custom headers to merge.
            impersonate: Browser profile to impersonate (e.g. ``"chrome"``,
                ``"firefox"``, ``"safari"``, or specific versions like ``"chrome124"``).
                If ``None``, the caller should fall back to plain ``HttpFetcher``.

        Returns:
            FetchResult with the response HTML, status code, final URL, and headers.

        Raises:
            SecurityError: Unknown impersonation profile.
            ImportError: curl_cffi not available.
            TimeoutError: Request exceeded the timeout.
            HttpError: Non-2xx / connection error.
        """
        if not _CURL_CFFI_AVAILABLE:
            raise ImportError("curl_cffi is not installed. Install it with: pip install curl_cffi")

        # Resolve the impersonation profile
        resolved_profile = _resolve_profile(impersonate)

        if domain_limiter and domain:
            await domain_limiter.wait_if_needed(domain)
            domain_limiter.acquire(domain)

        try:
            async with self._semaphore:
                return await self._do_fetch(
                    url,
                    timeout_seconds,
                    proxy_url or self._proxy_url,
                    cookies=cookies,
                    extra_headers=extra_headers,
                    impersonate=resolved_profile,
                )
        finally:
            if domain_limiter and domain:
                domain_limiter.release(domain)

    async def _do_fetch(
        self,
        url: str,
        timeout_seconds: int | None = None,
        proxy_url: str | None = None,
        cookies: list[dict] | None = None,
        extra_headers: dict[str, str] | None = None,
        impersonate: str | None = None,
    ) -> FetchResult:
        """Internal fetch implementation."""
        raw_timeout = timeout_seconds or self._timeout
        session = await self._ensure_session()

        # Apply per-request proxy if provided
        if proxy_url:
            session.proxies = {"all": proxy_url}

        # Build headers
        # NOTE: When impersonating, curl_cffi automatically sets User-Agent and
        # sec-ch-ua to match the impersonated browser. We must NOT override
        # these headers with our own values.
        headers: dict[str, str] = {}

        if extra_headers:
            for name, value in extra_headers.items():
                # Safety check: prevent overriding security-critical headers
                lower_name = name.lower()
                if lower_name in ("user-agent", "sec-ch-ua", "sec-ch-ua-platform"):
                    logger.warning(
                        "Skipping override of security-critical header %r when impersonating",
                        name,
                    )
                    continue
                headers[name] = value

        # Build cookies dict for curl_cffi
        cookies_dict: dict[str, str] | None = None
        if cookies:
            cookies_dict = cookie_header_dict(cookies)

        start = time.monotonic()
        logger.debug(
            "Impersonating GET %s (timeout: %ds, profile: %s)",
            url,
            raw_timeout,
            impersonate or "none",
        )

        try:
            response = await session.get(
                url,
                headers=headers,
                cookies=cookies_dict,
                impersonate=impersonate,
            )
        except curl_cffi.requests.exceptions.RequestException as exc:
            elapsed = int((time.monotonic() - start) * 1000)

            # Map curl_cffi exceptions to our error types
            if isinstance(exc, (curl_cffi.requests.exceptions.Timeout,)):
                logger.error(
                    "Impersonating timeout fetching %s (%d ms): %s",
                    url,
                    elapsed,
                    exc,
                )
                raise TimeoutError(
                    f"Request timed out after {raw_timeout}s fetching {url}",
                    details={
                        "url": url,
                        "elapsed_ms": elapsed,
                        "timeout_seconds": raw_timeout,
                        "strategy": "impersonate",
                    },
                ) from exc

            # Other curl_cffi exceptions → HttpError
            logger.error(
                "Impersonating error fetching %s (%d ms): %s",
                url,
                elapsed,
                exc,
            )
            raise HttpError(
                f"HTTP request failed: {exc}",
                details={
                    "url": url,
                    "elapsed_ms": elapsed,
                    "error": str(exc),
                    "strategy": "impersonate",
                },
            ) from exc
        except Exception as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            logger.error(
                "Unexpected error fetching %s (%d ms): %s",
                url,
                elapsed,
                exc,
            )
            raise HttpError(
                f"Unexpected HTTP error: {exc}",
                details={
                    "url": url,
                    "elapsed_ms": elapsed,
                    "error": str(exc),
                    "strategy": "impersonate",
                },
            ) from exc

        elapsed = int((time.monotonic() - start) * 1000)

        logger.info(
            "Impersonating fetch %s → %d (%d bytes, %d ms, profile: %s)",
            url,
            response.status_code,
            len(response.content),
            elapsed,
            impersonate or "none",
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
            backend_name="impersonate",
            backend_metadata={
                "strategy": "impersonate",
                "profile": impersonate,
            },
        )

    async def health(self) -> bool:
        """Return True if the curl_cffi library is available."""
        return _CURL_CFFI_AVAILABLE

    def close(self) -> None:
        """Release resources. Safe to call multiple times."""
        if self._session:
            # AsyncSession.close() is async, but this is called from sync code
            # during service shutdown. We schedule it in the event loop.
            try:
                asyncio.get_running_loop().run_until_complete(self._session.close())
            except RuntimeError:
                # No running loop — nothing to do
                pass
            self._session = None
