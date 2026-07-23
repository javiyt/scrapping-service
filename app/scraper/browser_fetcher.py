"""Browser-based fetcher using Botasaurus Driver.

Requires ``botasaurus`` (or ``botasaurus-driver``) and a compatible Chrome /
Chromium binary to be installed.

In test environments without a browser, this class can be fully mocked at the
application layer.
"""

import logging
import threading
import time
from typing import Any

from app.scraper.http_fetcher import FetchResult

logger = logging.getLogger("scraper-api.fetcher.browser")

# Challenge / block page detection markers — if any appear in the title or
# body after load, we wait longer for a possible JS challenge to resolve.
_BLOCK_SIGNALS = [
    "access denied",
    "just a moment",
    "checking your browser",
    "attention required",
    "cf-challenge",
    "customdeny",
]


class BrowserFetcher:
    """Fetcher that drives a real (headless) Chrome via Botasaurus Driver.

    The driver is created **lazily** on the first fetch and kept alive
    for subsequent requests.
    """

    def __init__(
        self,
        headless: bool = True,
        arguments: list[str] | None = None,
        timeout_seconds: int = 45,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        window_size: tuple[int, int] = (1366, 768),
        proxy_url: str | None = None,
    ) -> None:
        self._headless = headless
        self._arguments = arguments or []
        self._timeout = timeout_seconds
        self._user_agent = user_agent
        self._window_size = window_size
        self._proxy_url = proxy_url
        self._driver: Any = None
        self._lock = threading.RLock()
        self._botasaurus_available = False

        # Probe for botasaurus-driver at init time.
        try:
            from botasaurus_driver import Driver  # noqa: F401

            self._botasaurus_available = True
        except ImportError:
            logger.warning(
                "botasaurus-driver is not installed — browser fetcher will raise "
                "ImportError on fetch.  Install with: pip install botasaurus"
            )

    # --------------------------------------------------------------- driver

    @property
    def driver(self) -> Any:
        """Lazy-initialised Botasaurus Driver."""
        if self._driver is None:
            from botasaurus_driver import Driver

            kwargs: dict = {
                "headless": self._headless,
                "arguments": self._arguments,
                "user_agent": self._user_agent,
                "window_size": self._window_size,
                "block_images": True,
                "wait_for_complete_page_load": True,
            }
            if self._proxy_url:
                # Note: Botasaurus Driver proxy support may vary by version.
                # The 'proxy' parameter expects a string like
                # "http://user:pass@host:port" or a dict mapping scheme to URL.
                kwargs["proxy"] = self._proxy_url

            self._driver = Driver(**kwargs)
        return self._driver

    # ------------------------------------------------------------------ fetch

    async def fetch(
        self,
        url: str,
        timeout_seconds: int | None = None,
        wait_until: str = "networkidle",
        wait_selector: str | None = None,
        scroll_config: dict[str, Any] | None = None,
        screenshot_path: str | None = None,
        proxy_url: str | None = None,
    ) -> FetchResult:
        """Fetch a URL with browser rendering.

        Args:
            url: Target URL.
            timeout_seconds: Page-load timeout (defaults to instance default).
            wait_until: When to consider the page loaded.
            wait_selector: Optional CSS selector to wait for.
            scroll_config: Scrolling behaviour (``enabled``, ``max_scrolls``, …).
            screenshot_path: If set, save a screenshot to this path.
            proxy_url: Proxy URL override.  Only supported when set at
                construction time — per-request proxy overrides are **not**
                supported for the browser fetcher and will be silently ignored.

        Returns:
            A :class:`FetchResult` with the full rendered HTML.

        Raises:
            ImportError: Botasaurus is not installed.
        """
        if not self._botasaurus_available:
            raise ImportError(
                "Botasaurus driver is required for browser scraping. "
                "Install it with: pip install botasaurus"
            )

        timeout = timeout_seconds or self._timeout

        # Per-request proxy override is NOT supported for the browser fetcher.
        # Botasaurus Driver only accepts proxy at construction time.  Log a
        # warning if a different proxy is requested.
        if proxy_url and proxy_url != self._proxy_url:
            logger.warning(
                "Per-request proxy override is not supported for browser fetcher; "
                "using the globally configured proxy (or none)."
            )

        # Selenium calls are synchronous, so offload to a thread-pool.
        import asyncio

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            self._sync_fetch,
            url,
            timeout,
            wait_until,
            wait_selector,
            scroll_config or {},
            screenshot_path,
        )
        return result

    # ------------------------------------------------------------ sync fetch

    def _sync_fetch(
        self,
        url: str,
        timeout: int,
        wait_until: str,
        wait_selector: str | None,
        scroll_config: dict[str, Any],
        screenshot_path: str | None,
    ) -> FetchResult:
        """Synchronous browser-fetch (runs in thread-pool)."""
        with self._lock:
            return self._sync_fetch_locked(
                url,
                timeout,
                wait_until,
                wait_selector,
                scroll_config,
                screenshot_path,
            )

    def _sync_fetch_locked(
        self,
        url: str,
        timeout: int,
        wait_until: str,
        wait_selector: str | None,
        scroll_config: dict[str, Any],
        screenshot_path: str | None,
    ) -> FetchResult:
        """Synchronous browser-fetch with exclusive driver access."""
        start = time.monotonic()
        driver = self.driver

        # Navigate to the URL.
        driver.get(url)

        # ---- Wait strategy
        if wait_until in ("networkidle", "networkidle0"):
            for _ in range(int(timeout / 0.5)):
                ready: str = driver.run_js("return document.readyState")
                if ready == "complete":
                    break
                time.sleep(0.5)

        # Wait for a specific CSS selector if provided.
        if wait_selector:
            driver.wait_for_element(wait_selector, timeout=timeout)

        # ---- Wait for JavaScript challenges to resolve
        # Some CDNs / WAFs (Akamai, Cloudflare, etc.) serve a challenge page
        # that self-resolves after a few seconds.  If we detect one, wait and
        # then refresh the page HTML.
        page_text_lower: str = driver.run_js("return document.title").lower()  # type: ignore[no-untyped-call]
        if not any(signal in page_text_lower for signal in _BLOCK_SIGNALS):
            page_text_lower = driver.page_html.lower()

        if any(signal in page_text_lower for signal in _BLOCK_SIGNALS):
            logger.info("Block/challenge page detected — waiting up to 10 s for resolution")
            for _ in range(20):  # 20 × 0.5 s = 10 s
                time.sleep(0.5)
                current: str = driver.page_html.lower()
                if not any(signal in current for signal in _BLOCK_SIGNALS):
                    logger.info("Challenge resolved after %.1f s", (time.monotonic() - start))
                    break
            else:
                logger.warning("Challenge did not resolve within 10 s — returning current HTML")

        # ---- Scrolling
        if scroll_config.get("enabled", False):
            self._sync_scroll(
                driver,
                max_scrolls=scroll_config.get("max_scrolls", 5),
                delay_ms=scroll_config.get("delay_ms", 1000),
                stop_when_no_growth=scroll_config.get("stop_when_no_growth", True),
            )

        # ---- Screenshot
        if screenshot_path:
            driver.save_screenshot(screenshot_path)

        html: str = driver.page_html
        current_url: str = driver.current_url
        elapsed = int((time.monotonic() - start) * 1000)

        return FetchResult(
            html=html,
            status_code=200,
            final_url=current_url,
            headers={},
            elapsed_ms=elapsed,
        )

    # --------------------------------------------------------------- scroll

    @staticmethod
    def _sync_scroll(
        driver: Any, max_scrolls: int, delay_ms: int, stop_when_no_growth: bool
    ) -> None:
        """Scroll to the bottom of the page in steps."""
        last_height: float = driver.run_js("return document.body.scrollHeight")
        for i in range(max_scrolls):
            driver.run_js("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(delay_ms / 1000.0)

            if stop_when_no_growth:
                new_height: float = driver.run_js("return document.body.scrollHeight")
                if new_height == last_height:
                    break
                last_height = new_height

    # -------------------------------------------------------------- cleanup

    def close(self) -> None:
        """Release browser resources.  Call on application shutdown."""
        with self._lock:
            driver = self._driver
            self._driver = None

        if driver is not None:
            try:
                for method_name in ("close", "quit", "stop"):
                    method = getattr(driver, method_name, None)
                    if callable(method):
                        method()
                        break
            except Exception:
                logger.exception("Error while closing browser driver")
