"""Concrete backend-selection engine with a bounded fallback policy.

The :class:`BrowserBackendOrchestrator` is the thing the scraper service
actually calls.  It satisfies the
:class:`app.scraper.browser_backend.BrowserBackend` contract while adding the
*selection* logic the service would otherwise have to own:

1. Try the **primary** backend (Botasaurus).
2. If that fails in a way the fallback policy considers *appropriate*, try
   each configured **fallback** backend **exactly once**, in order.
3. If nothing succeeds, raise the *primary* backend's error (the
   orchestrator never retries a backend, so there is no retry loop).

"Appropriate" failures (see :func:`is_fallback_appropriate`) are browser-
level problems — startup/import errors, navigation timeouts, upstream browser
errors, and obviously-blocked/challenge results.  Extraction-level failures
(application rules) and security/invalid-input errors never trigger a
fallback.

Bounded metrics (``browser_backend_*_total`` with a ``backend`` label from the
bounded, operator-configured set of backend names; URLs never appear in
labels) are recorded for every attempt.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.errors import BrowserError, SecurityError, TimeoutError
from app.metrics.prometheus import MetricsCollector, get_metrics
from app.scraper.browser_backend import BrowserBackend
from app.scraper.http_fetcher import FetchResult

logger = logging.getLogger("scraper-api.fetcher.backends")

# Marker strings that indicate a *block/challenge* page rather than real
# content.  These are specific enough to avoid false positives on ordinary
# pages; they mirror the signals the Botasaurus driver already waits on.
_BLOCK_SIGNALS = (
    "access denied",
    "just a moment",
    "checking your browser",
    "attention required",
    "cf-challenge",
    "customdeny",
)

# Minimum rendered length below which a browser result is "clearly unusable".
MIN_USABLE_HTML_LENGTH = 500


def is_fallback_appropriate(exc: BaseException) -> bool:
    """Return ``True`` if *exc* is an *appropriate* reason to fall back.

    Appropriate: browser startup/import failures, timeouts, and the
    :class:`BrowserError` / :class:`CamofoxError` family (which already
    encodes navigation/upstream failures).

    Not appropriate: security/invalid-input errors (a bad URL or blocked
    proxy is not a browser problem) and application extraction errors.
    """
    if isinstance(exc, SecurityError):
        return False
    if isinstance(exc, (ImportError, TimeoutError)):
        return True
    if isinstance(exc, BrowserError):
        return True
    # The Camofox client raises the CamofoxError family; treat the whole
    # family as appropriate so an upstream Camofox problem is not re-raised
    # as a "browser failed" when a *later* backend could still work.
    from app.core.errors import CamofoxError

    if isinstance(exc, CamofoxError):
        return True
    # ValidationError and other non-browser errors → not appropriate.
    return False


def _result_is_usable(result: FetchResult) -> bool:
    """A rendered document is "clearly unusable" when it is empty or a
    block/challenge page (a browser returned something, but not content)."""
    if result.status_code >= 400:
        return False
    html = result.html
    if len(html.strip()) < MIN_USABLE_HTML_LENGTH:
        return False
    lowered = html[:4000].lower()  # scan a bounded prefix only
    if any(signal in lowered for signal in _BLOCK_SIGNALS):
        return False
    return True


class BrowserBackendOrchestrator:
    """Implements :class:`BrowserBackend` with primary → fallback selection."""

    def __init__(
        self,
        backends: list[BrowserBackend],
        *,
        metrics: MetricsCollector | None = None,
    ) -> None:
        if not backends:
            raise ValueError("BrowserBackendOrchestrator requires at least one backend")
        self._backends = list(backends)
        self._metrics = metrics or get_metrics()

    @property
    def backends(self) -> list[BrowserBackend]:
        return list(self._backends)

    @property
    def name(self) -> str:
        """The *primary* backend's name (used for the fallback label)."""
        return self._backends[0].name

    @property
    def primary(self) -> BrowserBackend:
        return self._backends[0]

    # --------------------------------------------------------------- contract

    async def fetch(
        self,
        url: str,
        timeout_seconds: int | None = None,
        wait_until: str = "networkidle",
        wait_selector: str | None = None,
        scroll_config: dict[str, Any] | None = None,
        screenshot_path: str | None = None,
        proxy_url: str | None = None,
        cookies: list[dict] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> FetchResult:
        """Try the primary backend, then each fallback exactly once.

        Raises the *primary* backend's error when no backend produces a usable
        result (preserves the historical "browser failed" semantics and the
        original root cause for the client).
        """
        primary_exc: BaseException | None = None
        last_exc: BaseException | None = None

        for index, backend in enumerate(self._backends):
            backend_name = backend.name
            self._metrics.inc("browser_backend_attempt_total", backend=backend_name)
            try:
                result = await backend.fetch(
                    url,
                    timeout_seconds=timeout_seconds,
                    wait_until=wait_until,
                    wait_selector=wait_selector,
                    scroll_config=scroll_config,
                    screenshot_path=screenshot_path,
                    proxy_url=proxy_url,
                    cookies=cookies,
                    extra_headers=extra_headers,
                )
            except SecurityError:
                # A security/invalid-input failure is *not* a browser problem:
                # record it and re-raise immediately — no fallback for it.
                self._metrics.inc("browser_backend_error_total", backend=backend_name)
                raise
            except BaseException as exc:  # noqa: BLE001 - capture any backend failure
                self._metrics.inc("browser_backend_error_total", backend=backend_name)
                if index == 0:
                    primary_exc = exc
                last_exc = exc
                if not is_fallback_appropriate(exc):
                    logger.info(
                        "Backend %s failed with a non-fallbackable error for %s: %s",
                        backend_name,
                        url,
                        _safe_message(exc),
                    )
                    raise
                logger.info(
                    "Backend %s failed for %s — %s",
                    backend_name,
                    url,
                    _safe_message(exc),
                )
                continue

            if _result_is_usable(result):
                self._metrics.inc("browser_backend_success_total", backend=backend_name)
                if index > 0:
                    self._metrics.inc("browser_backend_fallback_total", backend=backend_name)
                return result

            # The backend returned a result, but it is unusable (empty or a
            # block/challenge page).  Treat as a failure for fallback purposes
            # (still "appropriate"), but do *not* return it.
            self._metrics.inc("browser_backend_error_total", backend=backend_name)
            logger.info(
                "Backend %s returned an unusable result for %s "
                "(status=%d, len=%d) — falling through",
                backend_name,
                url,
                result.status_code,
                len(result.html),
            )
            if index == 0:
                primary_exc = BrowserError(
                    "Botasaurus returned an unusable (blocked/empty) result",
                    details={"backend": backend_name, "url": url},
                )
            last_exc = last_exc or BrowserError(
                f"{backend_name} returned an unusable result",
                details={"backend": backend_name, "url": url},
            )

        # No backend produced a usable result → raise the primary's error.
        raise primary_exc if primary_exc is not None else last_exc

    async def health(self) -> bool:
        """Usable if *any* configured backend is healthy."""
        for backend in self._backends:
            try:
                if await backend.health():
                    return True
            except Exception:  # noqa: BLE001 - a broken backend just reports False
                logger.debug("Backend health probe raised", exc_info=True)
        return False

    def close(self) -> None:
        """Close every backend.  Idempotent; individual failures are logged."""
        for backend in self._backends:
            try:
                backend.close()
            except Exception:  # noqa: BLE001 - cleanup must never raise
                logger.debug("Error closing backend %s", backend.name, exc_info=True)


def _safe_message(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    return message or exc.__class__.__name__


__all__ = ["BrowserBackendOrchestrator", "is_fallback_appropriate", "_result_is_usable"]
