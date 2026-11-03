"""Camofox implementation of the agent browser provider contract.

This is the first concrete :class:`AgentBrowserProvider`.  It wraps the
existing reusable :class:`app.camofox.client.CamofoxClient` /
:class:`app.camofox.session.CamofoxSession` and translates between the
provider-agnostic contract and the Camofox-specific types.

Identifier mapping (the Camofox IDs never escape this class):

* ``provider_session_id``  →  the Camofox ``sessionKey`` of a logical
  :class:`CamofoxSession` (which owns its own ``userId`` + ``sessionKey`` +
  tracked tabs).  Kept in an in-memory dict for the session's lifetime.
* ``provider_tab_id``      →  a Camofox ``tabId``; resolved back to the
  :class:`CamofoxTab` the session tracks.

The service layer (:class:`app.browser.service.BrowserService`) keeps its own
session/tab registry with service-owned IDs and only hands this provider the
opaque provider IDs above.
"""

from __future__ import annotations

import logging

from app.browser.providers.base import (
    ProviderActionResult,
    ProviderError,
    ProviderEvaluateResult,
    ProviderHealth,
    ProviderLink,
    ProviderLinks,
    ProviderNavigateResult,
    ProviderSession,
    ProviderSnapshot,
    ProviderTab,
)
from app.camofox.client import CamofoxClient
from app.camofox.session import CamofoxSession
from app.camofox.types import CamofoxTab
from app.core.errors import CamofoxError

__all__ = ["CamofoxAgentBrowserProvider", "build_camofox_provider"]

logger = logging.getLogger("scraper-api.browser.camofox")


def _translate_camofox_error(exc: CamofoxError) -> ProviderError:
    """Map a Camofox client error onto the provider-facing error type.

    The Camofox client already renders public messages without credentials or
    upstream blobs, so reusing the message here is safe.
    """
    return ProviderError(exc.message, details=dict(exc.details or {}))


class CamofoxAgentBrowserProvider:
    """Agent browser provider backed by the external Camofox REST service.

    One instance is shared for the app lifetime (built in the lifespan when a
    Camofox client is available).  It owns the mapping from
    provider-internal session IDs to live :class:`CamofoxSession` objects.
    """

    name = "camofox"
    supports_evaluate = True

    def __init__(self, client: CamofoxClient) -> None:
        if client is None:
            raise ValueError("CamofoxAgentBrowserProvider requires a CamofoxClient")
        self._client = client
        # provider_session_id (Camofox sessionKey) -> CamofoxSession
        self._sessions: dict[str, CamofoxSession] = {}

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def create_session(self) -> ProviderSession:
        """Create a fresh Camofox session.

        Camofox materialises the browser lazily on first tab use, so this
        performs no upstream I/O — it only builds the logical identity.
        """
        session = self._client.create_session()
        self._sessions[session.session_key] = session
        return ProviderSession(
            provider_session_id=session.session_key,
            camofox_user_id=session.user_id,
        )

    async def close_session(self, provider_session_id: str) -> None:
        """Close the session upstream and drop the local mapping.

        Idempotent: an unknown or already-released provider session is a no-op.
        """
        session = self._sessions.pop(provider_session_id, None)
        if session is None:
            return
        try:
            await session.close()
        except CamofoxError as exc:
            # Closing a session that already expired server-side is fine.
            logger.debug("Camofox session close reported: %s", exc.message)

    def _require_session(self, provider_session_id: str) -> CamofoxSession:
        session = self._sessions.get(provider_session_id)
        if session is None:
            raise ProviderError(
                "Browser session is not active (it may have expired)",
                details={"provider_session_id": provider_session_id},
            )
        return session

    def _require_tab(self, session: CamofoxSession, provider_tab_id: str) -> CamofoxTab:
        for tab in session.tabs:
            if tab.tab_id == provider_tab_id:
                return tab
        raise ProviderError(
            "Tab does not exist in the session",
            details={"provider_tab_id": provider_tab_id},
        )

    # ------------------------------------------------------------------
    # Tabs
    # ------------------------------------------------------------------

    async def create_tab(self, provider_session_id: str, url: str | None = None) -> ProviderTab:
        session = self._require_session(provider_session_id)
        try:
            camofox_tab = await session.create_tab(url)
        except CamofoxError as exc:
            # A session that died upstream makes the provider session stale.
            self._sessions.pop(provider_session_id, None)
            raise _translate_camofox_error(exc) from exc
        except ValueError as exc:
            self._sessions.pop(provider_session_id, None)
            raise ProviderError(str(exc)) from exc
        return ProviderTab(
            provider_tab_id=camofox_tab.tab_id, url=camofox_tab.url, title=camofox_tab.title
        )

    async def close_tab(self, provider_session_id: str, provider_tab_id: str) -> None:
        session = self._require_session(provider_session_id)
        camofox_tab = self._require_tab(session, provider_tab_id)
        try:
            await session.close_tab(camofox_tab)
        except CamofoxError as exc:
            if "TabNotFound" in type(exc).__name__:
                return  # already gone upstream — the desired end state
            raise _translate_camofox_error(exc) from exc

    # ------------------------------------------------------------------
    # Navigation / page data
    # ------------------------------------------------------------------

    async def navigate(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        url: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderNavigateResult:
        session = self._require_session(provider_session_id)
        camofox_tab = self._require_tab(session, provider_tab_id)
        try:
            result = await session.navigate(camofox_tab, url)
        except CamofoxError as exc:
            self._drop_stale_session(provider_session_id, exc)
            raise _translate_camofox_error(exc) from exc
        return ProviderNavigateResult(ok=result.ok, url=result.url, title=result.title)

    async def snapshot(
        self, provider_session_id: str, provider_tab_id: str, *, offset: int = 0
    ) -> ProviderSnapshot:
        session = self._require_session(provider_session_id)
        camofox_tab = self._require_tab(session, provider_tab_id)
        try:
            snap = await session.snapshot(camofox_tab, offset=offset)
        except CamofoxError as exc:
            self._drop_stale_session(provider_session_id, exc)
            raise _translate_camofox_error(exc) from exc
        return ProviderSnapshot(
            url=snap.url or camofox_tab.url,
            title=camofox_tab.title,
            snapshot=snap.snapshot,
            refs_count=snap.refs_count,
            truncated=snap.truncated,
            has_more=snap.has_more,
            next_offset=snap.next_offset,
        )

    async def get_links(
        self, provider_session_id: str, provider_tab_id: str, *, limit: int = 50
    ) -> ProviderLinks:
        session = self._require_session(provider_session_id)
        camofox_tab = self._require_tab(session, provider_tab_id)
        try:
            links = await session.get_links(camofox_tab, limit=limit)
        except CamofoxError as exc:
            self._drop_stale_session(provider_session_id, exc)
            raise _translate_camofox_error(exc) from exc
        return ProviderLinks(
            links=[ProviderLink(url=item.url, text=item.text) for item in links.links],
            total=links.total,
            has_more=links.has_more,
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def click(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        *,
        ref: str | None = None,
        selector: str | None = None,
    ) -> ProviderActionResult:
        session = self._require_session(provider_session_id)
        camofox_tab = self._require_tab(session, provider_tab_id)
        if ref is None and selector is None:
            raise ProviderError("click requires a ref or a selector")
        try:
            result = await session.click(camofox_tab, ref=ref, selector=selector)
        except CamofoxError as exc:
            self._drop_stale_session(provider_session_id, exc)
            raise _translate_camofox_error(exc) from exc
        return ProviderActionResult(ok=result.ok, url=result.url)

    async def type_text(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        text: str,
        *,
        ref: str | None = None,
        selector: str | None = None,
    ) -> ProviderActionResult:
        session = self._require_session(provider_session_id)
        camofox_tab = self._require_tab(session, provider_tab_id)
        try:
            result = await session.type_text(camofox_tab, text, ref=ref, selector=selector)
        except CamofoxError as exc:
            self._drop_stale_session(provider_session_id, exc)
            raise _translate_camofox_error(exc) from exc
        return ProviderActionResult(ok=result.ok, url=result.url)

    async def evaluate(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        expression: str,
        *,
        timeout_ms: int | None = None,
    ) -> ProviderEvaluateResult:
        session = self._require_session(provider_session_id)
        camofox_tab = self._require_tab(session, provider_tab_id)
        try:
            result = await session.evaluate(camofox_tab, expression, timeout_ms=timeout_ms)
        except CamofoxError as exc:
            self._drop_stale_session(provider_session_id, exc)
            raise _translate_camofox_error(exc) from exc
        return ProviderEvaluateResult(
            ok=result.ok,
            result=result.result,
            result_type=result.result_type,
            truncated=result.truncated,
            error=result.error,
            error_type=result.error_type,
        )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> ProviderHealth:
        """Cheap unauthenticated probe of the Camofox service."""
        status = await self._client.probe_health()
        return ProviderHealth(
            available=status.available,
            reason=status.reason,
            version=status.version,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _drop_stale_session(self, provider_session_id: str, exc: CamofoxError) -> None:
        """Drop the provider session when the server reports it gone.

        Keeping a dead session would only produce repeated failures for the
        life of the service.  Only session-expiry signals qualify.
        """
        if "SessionExpired" in type(exc).__name__:
            self._sessions.pop(provider_session_id, None)

    def release(self, provider_session_id: str) -> bool:
        """Remove the local mapping without an upstream call.

        Used by the service's idle-cleanup path after it has already closed
        the session through the provider; returns whether a mapping existed.
        """
        session = self._sessions.pop(provider_session_id, None)
        if session is not None:
            session.forget_all_tabs()
        return session is not None


def build_camofox_provider(client: CamofoxClient | None) -> CamofoxAgentBrowserProvider | None:
    """Build the Camofox agent browser provider from a Camofox client.

    Returns ``None`` when no client is available (Camofox disabled or
    unconfigured), so "no provider" cleanly means "agent browser unavailable".
    """
    if client is None:
        return None
    return CamofoxAgentBrowserProvider(client)
