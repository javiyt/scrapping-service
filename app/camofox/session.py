"""Logical Camofox sessions.

Camofox isolates browser state per ``userId`` and groups tabs per
``sessionKey``.  Callers should never construct those identifiers by hand:
a :class:`CamofoxSession` owns one ``userId`` (derived from the configured
``user_id_prefix``) plus a unique ``sessionKey``, and tracks the tabs it
created.  Because every request is scoped to the session's ``userId``,
concurrent sessions can never share tabs, cookies, or browser profiles.

The session object is a *lightweight logical* handle: creating it performs
no I/O.  The upstream browser materialises lazily on first use and is
released by :meth:`close` (or by the server's idle cleanup, which surfaces
as :class:`app.core.errors.CamofoxSessionExpiredError`).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.camofox.types import (
    CamofoxActionResult,
    CamofoxCookie,
    CamofoxEvaluateResult,
    CamofoxLinks,
    CamofoxNavigateResult,
    CamofoxSnapshot,
    CamofoxTab,
    CamofoxTabInfo,
)
from app.core.errors import ValidationError

if TYPE_CHECKING:
    from app.camofox.client import CamofoxClient

__all__ = ["CamofoxSession"]


class CamofoxSession:
    """A logical Camofox session: one isolated browser identity.

    The tab registry (``self._tabs``) is a plain dict mutated with atomic
    operations — it never guards shared mutable state, only bookkeeping, so
    concurrent use of a single session is safe without a lock.
    """

    def __init__(
        self,
        client: CamofoxClient,
        name: str | None = None,
        *,
        user_id: str | None = None,
        session_key: str | None = None,
    ) -> None:
        self._client = client
        self.user_id = user_id or client.make_user_id(name)
        self.session_key = session_key or uuid.uuid4().hex
        self._tabs: dict[str, CamofoxTab] = {}
        self._closed_tabs: set[str] = set()

    # ------------------------------------------------------------------
    # Tab registry (bookkeeping)
    # ------------------------------------------------------------------

    @property
    def tabs(self) -> tuple[CamofoxTab, ...]:
        """Tabs currently tracked by this session."""
        return tuple(self._tabs.values())

    def add_tab(self, tab: CamofoxTab) -> None:
        self._tabs[tab.tab_id] = tab
        self._closed_tabs.discard(tab.tab_id)

    def upsert_tabs(self, tabs: list[CamofoxTabInfo]) -> None:
        """Track tabs discovered via listing (keep locally enriched versions)."""
        for info in tabs:
            existing = self._tabs.get(info.tab_id)
            if existing is None:
                self._tabs[info.tab_id] = CamofoxTab(
                    tab_id=info.tab_id, url=info.url, title=info.title
                )
            else:
                existing.url = info.url or existing.url
                existing.title = info.title or existing.title

    def forget_tab(self, tab_id: str) -> None:
        self._tabs.pop(tab_id, None)
        self._closed_tabs.add(tab_id)

    def forget_all_tabs(self) -> None:
        self._tabs.clear()

    def assert_owns(self, tab: CamofoxTab) -> None:
        """Ensure the tab belongs to this session (client-side misuse guard).

        Tabs this session created (or already closed) are its own; anything
        else — e.g. a tab from a different session — is refused.
        """
        if tab.tab_id not in self._tabs and tab.tab_id not in self._closed_tabs:
            raise ValidationError(
                f"tab {tab.tab_id!r} does not belong to this session",
                details={"user_id": self.user_id},
            )

    # ------------------------------------------------------------------
    # Operations (delegate to the client with this session's identity)
    # ------------------------------------------------------------------

    async def create_tab(self, url: str | None = None) -> CamofoxTab:
        """Create a new tab in this session (the browser starts lazily)."""
        return await self._client.create_tab(self, url)

    async def list_tabs(self) -> list[CamofoxTabInfo]:
        """List the session's tabs (tracks them locally)."""
        return await self._client.list_tabs(self)

    async def navigate(
        self,
        tab: CamofoxTab,
        url: str | None = None,
        *,
        macro: str | None = None,
        engine: str | None = None,
        query: str | None = None,
    ) -> CamofoxNavigateResult:
        """Navigate a tab to a URL, or run a search macro (``engine`` shorthand)."""
        return await self._client.navigate(self, tab, url, macro=macro, engine=engine, query=query)

    async def snapshot(self, tab: CamofoxTab, offset: int = 0) -> CamofoxSnapshot:
        """Fetch the accessibility-tree snapshot (windowed, with element refs)."""
        return await self._client.snapshot(self, tab, offset=offset)

    async def get_links(self, tab: CamofoxTab, *, limit: int = 50, offset: int = 0) -> CamofoxLinks:
        """Extract page links (with pagination)."""
        return await self._client.get_links(self, tab, limit=limit, offset=offset)

    async def click(
        self,
        tab: CamofoxTab,
        *,
        ref: str | None = None,
        selector: str | None = None,
    ) -> CamofoxActionResult:
        """Click an element by snapshot ``ref`` or CSS selector."""
        return await self._client.click(self, tab, ref=ref, selector=selector)

    async def type_text(
        self,
        tab: CamofoxTab,
        text: str,
        *,
        ref: str | None = None,
        selector: str | None = None,
    ) -> CamofoxActionResult:
        """Type text into the active element (or one addressed by ref/selector)."""
        return await self._client.type_text(self, tab, text, ref=ref, selector=selector)

    async def evaluate(
        self,
        tab: CamofoxTab,
        expression: str,
        *,
        timeout_ms: int | None = None,
    ) -> CamofoxEvaluateResult:
        """Evaluate JavaScript in the page context."""
        return await self._client.evaluate(self, tab, expression, timeout_ms=timeout_ms)

    async def close_tab(self, tab: CamofoxTab) -> None:
        """Close a tab and stop tracking it."""
        await self._client.close_tab(self, tab)

    async def import_cookies(
        self, cookies: list[CamofoxCookie], *, tab: CamofoxTab | None = None
    ) -> int:
        """Import cookies into the session (optionally scoped to a tab).

        Returns the number of cookies imported as reported by the server.
        """
        return await self._client.import_cookies(self, cookies, tab=tab)

    async def close(self) -> None:
        """Close the session upstream (all its tabs) and forget local state.

        Idempotent: a session that already expired server-side is released
        locally without error.
        """
        await self._client.close_session(self)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"CamofoxSession(user_id={self.user_id!r}, tabs={len(self._tabs)})"
