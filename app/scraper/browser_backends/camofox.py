"""Camofox-backed browser fetcher — an optional fallback browser backend.

Camofox (``camofox-browser``) is an external long-running browser service
consumed purely over HTTP through :class:`app.camofox.client.CamofoxClient`.
Nothing is launched here.

This module adapts the Camofox REST client to the
:class:`app.scraper.browser_backend.BrowserBackend` contract so the scraper
service can use it as a fallback after the primary backend (Botasaurus)
fails.  It is a thin adapter: all Camofox REST semantics (auth scopes, error
mapping, session lifecycle) live in :mod:`app.camofox`; only the rendered-
document fetch flow lives here.

Fetch lifecycle (one tab, always cleaned up):

1. create a browser tab (the upstream browser starts lazily on first use),
2. optionally import cookies scoped to the tab,
3. navigate the tab to the target URL,
4. wait for the document to be in a "complete" state (retried briefly),
5. extract the rendered document via ``document.documentElement.outerHTML``
   (never an accessibility snapshot),
6. capture the final URL,
7. close the tab (in a ``finally`` — tabs are never leaked).

Cookies use the client's *sensitive* route, so a Camofox session can only
accept per-request cookies when the backend was built with a configured
``api_key``; otherwise cookie imports are skipped with a warning (mirroring
how the Botasaurus backend treats per-request proxy overrides it cannot
honor).  The rendered document is the *HTML document*, never an
accessibility snapshot, per the ticket.

The backend reuses the app-wide shared ``CamofoxClient`` when one is provided
(one HTTP client per process, shared with the Camofox search provider); it
builds its own when none is.  :meth:`close` only releases its own resources
and is a no-op for a shared client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.camofox.client import CamofoxClient
from app.camofox.config import CamofoxConfig
from app.camofox.session import CamofoxSession
from app.camofox.types import CamofoxCookie
from app.core.config import Settings
from app.core.errors import BrowserError, CamofoxError, TimeoutError
from app.scraper.http_fetcher import FetchResult

logger = logging.getLogger("scraper-api.fetcher.camofox")

NAME = "camofox"
CAMOFOX_NAME = NAME

# Rendered-document extraction.  ``documentElement.outerHTML`` is the full
# document (including ``<head>``); it is the HTML *document*, never an
# accessibility snapshot.
_HTML_JS = "document.readyState === 'complete' ? document.documentElement.outerHTML : null"


def _camofox_cookies(cookies: list[dict] | None, final_url: str) -> list[CamofoxCookie]:
    """Map request cookies to :class:`CamofoxCookie` for the target origin.

    Cookie dicts (from :func:`app.scraper.cookies.cookies_for_url`) may or may
    not carry a ``domain``.  When absent, the target URL's host is used so
    the cookies are scoped to the page they were matched for.  Values are
    never logged.
    """
    if not cookies:
        return []
    try:
        from urllib.parse import urlparse

        host = urlparse(final_url).hostname or ""
    except Exception:  # noqa: BLE001 - never fail the fetch over cookie mapping
        host = ""
    result: list[CamofoxCookie] = []
    for cookie in cookies:
        domain = cookie.get("domain") or host
        if not domain:
            continue
        try:
            result.append(CamofoxCookie(name=cookie["name"], value=cookie["value"], domain=domain))
        except Exception:  # noqa: BLE001 - skip malformed cookies
            logger.debug("Skipping malformed cookie for Camofox import")
    return result


class CamofoxBrowserBackend:
    """A :class:`BrowserBackend` backed by the external Camofox service."""

    def __init__(
        self,
        config: CamofoxConfig,
        client: CamofoxClient | None = None,
        *,
        timeout_seconds: float | None = None,
        user_id_prefix: str | None = None,
    ) -> None:
        self._config = config
        if client is None:
            if not config.enabled or not config.base_url:
                raise ValueError("Camofox backend requires an enabled camofox client config")
            client = CamofoxClient(config)
        self._owns_client = client is not None and client is not None
        self._client = client
        self._timeout = timeout_seconds or config.timeout_seconds
        self._user_id_prefix = user_id_prefix or config.user_id_prefix
        # Whether the shared/owned client has a sensitive key for cookie import.
        self._can_import_cookies = bool(self._client.config.api_key)

    # ------------------------------------------------------------------ contract

    @property
    def name(self) -> str:
        return NAME

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
        """Fetch *url* through Camofox and return the rendered HTML document.

        The ``wait_selector``/``scroll_config``/``screenshot_path`` arguments
        are accepted for contract compatibility; Camofox has no direct
        equivalents, so they are intentionally no-ops here (the render wait
        uses the document readyState instead).  Per-request proxy /
        extra-headers overrides are not supported by the Camofox service and
        are ignored with a warning, mirroring the Botasaurus backend.
        """
        if proxy_url:
            logger.warning(
                "Per-request proxy override is not supported by the Camofox backend; ignoring."
            )
        if extra_headers:
            logger.warning(
                "Per-request extra headers are not supported by the Camofox backend; ignoring."
            )

        session: CamofoxSession | None = None
        tab = None
        start = time.monotonic()
        try:
            session = self._client.create_session()
            tab = await session.create_tab()
            if cookies:
                self._import_cookies(session, tab, cookies, url)
            await session.navigate(tab, url)
            html = await self._wait_for_html(session, tab, timeout_seconds=timeout_seconds)
            final_url = (tab.url if tab.url else None) or url
            return FetchResult(
                html=html,
                status_code=200,
                final_url=final_url,
                headers={},
                elapsed_ms=int((time.monotonic() - start) * 1000),
                backend_name=self.name,
                backend_metadata={"via": "camofox"},
            )
        except (CamofoxError, TimeoutError) as exc:
            raise BrowserError(
                "Camofox browser fetch failed",
                details={"backend": self.name, "url": url, "error": exc.message},
            ) from exc
        finally:
            if session is not None and tab is not None:
                await self._close_tab_quietly(session, tab)

    async def health(self) -> bool:
        """Cheap probe: a single unauthenticated ``GET /health`` (no tab)."""
        if self._client is None:
            return False
        status = await self._client.probe_health()
        return bool(status.available)

    def close(self) -> None:
        """Release owned resources.  Shared client is closed by the app."""
        # The client is async-closed by the application lifecycle when shared.
        # For an owned client we rely on the app to await close(); there is no
        # sync path here because closing an httpx client is async.  Nothing to
        # release synchronously (sessions/tabs are per-fetch).

    # ------------------------------------------------------------------ internals

    def _import_cookies(
        self, session: CamofoxSession, tab: Any, cookies: list[dict], url: str
    ) -> None:
        if not self._can_import_cookies:
            logger.warning(
                "Camofox backend has no api_key; skipping per-request cookie import for %s",
                url,
            )
            return
        camo_cookies = _camofox_cookies(cookies, url)
        if not camo_cookies:
            return
        try:
            count = self._client.import_cookies(session, camo_cookies, tab=tab)
            logger.debug("Imported %d cookies into Camofox tab", count)
        except CamofoxError as exc:
            # Cookie import is best-effort: a failure should not abort a fetch
            # that might otherwise succeed (mirrors Botasaurus' permissive
            # cookie handling).
            logger.warning(
                "Camofox cookie import failed; continuing without cookies: %s", exc.message
            )

    async def _wait_for_html(
        self, session: CamofoxSession, tab: Any, timeout_seconds: int | None = None
    ) -> str:
        """Wait for the document to complete, then extract the rendered HTML.

        Retries the readyState check briefly (a render budget) and raises
        :class:`BrowserError` if the document never reports complete within
        that budget.
        """
        budget = timeout_seconds or self._timeout
        deadline = time.monotonic() + budget
        while True:
            try:
                result = await session.evaluate(tab, _HTML_JS)
            except (CamofoxError, TimeoutError) as exc:
                # A transient evaluate failure (timeout, transient transport
                # error) is retried within the render budget; a persistent
                # failure that exhausts the budget becomes a BrowserError.
                if time.monotonic() >= deadline:
                    raise BrowserError(
                        "Camofox page did not reach a complete render state",
                        details={"backend": self.name, "error": exc.message},
                    ) from exc
                await asyncio.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
                continue
            if result.ok and isinstance(result.result, str) and result.result:
                return result.result
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        raise BrowserError(
            "Camofox page did not reach a complete render state",
            details={"backend": self.name},
        )

    async def _close_tab_quietly(self, session: CamofoxSession, tab: Any) -> None:
        try:
            await session.close_tab(tab)
        except CamofoxError:
            # The tab may already be gone upstream; the desired end state.
            logger.debug("Camofox tab close failed (likely already closed)")
        except Exception:  # noqa: BLE001 - cleanup must never raise
            logger.debug("Camofox tab close raised during cleanup")


def build_camofox_backend(
    settings: Settings, client: CamofoxClient | None = None
) -> CamofoxBrowserBackend | None:
    """Build the Camofox backend from settings, or ``None`` when not configured.

    Returns ``None`` when no client is provided *and* the top-level ``camofox``
    section is absent/disabled (the backend is optional).  When a shared client
    is provided it is reused (the backend is constructed unconditionally — the
    client is the source of truth for availability).
    """
    if client is None:
        config = CamofoxConfig.from_settings(settings)
        if not config.enabled:
            return None
        if not config.base_url:
            logger.warning("Camofox backend configured but base_url missing; disabling it")
            return None
        return CamofoxBrowserBackend(config)
    return CamofoxBrowserBackend(CamofoxConfig.from_settings(settings), client=client)


__all__ = ["CamofoxBrowserBackend", "build_camofox_backend", "NAME", "CAMOFOX_NAME"]
