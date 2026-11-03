"""Provider-agnostic agent browser contract.

This module defines the backend-side protocol every :class:`AgentBrowserProvider`
must implement, plus a single provider-facing error type.

The contract is deliberately coarse: providers implement *provider-internal*
session/tab identifiers and map them to whatever their upstream service uses
(Camofox ``userId``/``sessionKey``/``tabId`` today).  The scrapping-service
layer (:class:`app.browser.service.BrowserService`) owns the *public*
session/tab IDs and the security/policy rules, so a provider never sees
profiles or the service's session registry.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AgentBrowserProvider",
    "ProviderError",
    "ProviderSession",
    "ProviderTab",
    "ProviderNavigateResult",
    "ProviderSnapshot",
    "ProviderActionResult",
    "ProviderEvaluateResult",
    "ProviderLink",
    "ProviderLinks",
    "ProviderHealth",
]


class ProviderError(Exception):
    """Provider-facing error for interactive browser backend failures.

    Providers raise this (carrying a *safe* message and optional details)
    for any upstream failure.  The service layer calls :meth:`to_public` to
    translate it into the API-layer error type so it renders with the
    standard error envelope.  Messages must never contain secrets or raw
    upstream payloads.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_public(self) -> Any:
        """Translate into the public :class:`BrowserProviderError` (HTTP 502).

        Importing the public type here (lazily) avoids an import cycle:
        ``app.core.errors`` does not import from ``app.browser``.
        """
        from app.core.errors import BrowserProviderError

        return BrowserProviderError(self.message, details=dict(self.details))


class ProviderTab:
    """A provider-internal tab handle (holds the upstream tab identifier)."""

    def __init__(
        self, provider_tab_id: str, url: str | None = None, title: str | None = None
    ) -> None:
        self.provider_tab_id = provider_tab_id
        self.url = url
        self.title = title


class ProviderSession:
    """A provider-internal session handle.

    Holds the provider-internal session identifier and any opaque context the
    provider needs to address the upstream browser.  Service-layer session
    metadata (ownership, TTL, tabs) is kept by the service, not here.
    """

    def __init__(self, provider_session_id: str, **context: Any) -> None:
        self.provider_session_id = provider_session_id
        self.context: dict[str, Any] = context


class ProviderNavigateResult:
    def __init__(self, ok: bool, url: str | None = None, title: str | None = None) -> None:
        self.ok = ok
        self.url = url
        self.title = title


class ProviderSnapshot:
    """A normalized accessibility-tree snapshot with element refs."""

    def __init__(
        self,
        url: str | None,
        title: str | None,
        snapshot: str,
        refs_count: int = 0,
        truncated: bool = False,
        has_more: bool = False,
        next_offset: int | None = None,
    ) -> None:
        self.url = url
        self.title = title
        self.snapshot = snapshot
        self.refs_count = refs_count
        self.truncated = truncated
        self.has_more = has_more
        self.next_offset = next_offset


class ProviderActionResult:
    def __init__(self, ok: bool, url: str | None = None) -> None:
        self.ok = ok
        self.url = url


class ProviderEvaluateResult:
    def __init__(
        self,
        ok: bool,
        result: Any = None,
        result_type: str | None = None,
        truncated: bool = False,
        error: str | None = None,
        error_type: str | None = None,
    ) -> None:
        self.ok = ok
        self.result = result
        self.result_type = result_type
        self.truncated = truncated
        self.error = error
        self.error_type = error_type


class ProviderLink:
    def __init__(self, url: str, text: str = "") -> None:
        self.url = url
        self.text = text


class ProviderLinks:
    def __init__(
        self,
        links: list[ProviderLink],
        total: int = 0,
        has_more: bool = False,
    ) -> None:
        self.links = links
        self.total = total if total else len(links)
        self.has_more = has_more


class ProviderHealth:
    """Provider availability (mirrors the Camofox health status shape)."""

    def __init__(
        self,
        available: bool,
        reason: str | None = None,
        version: str | None = None,
    ) -> None:
        self.available = available
        self.reason = reason
        self.version = version


@runtime_checkable
class AgentBrowserProvider(Protocol):
    """Provider-agnostic interactive browser backend contract.

    Implementations wrap a concrete upstream (Camofox is first).  All methods
    are async.  ``provider_session_id`` / ``provider_tab_id`` are
    provider-internal identifiers returned by ``create_session`` /
    ``create_tab`` and passed back on subsequent calls.

    Implementations should raise :class:`BrowserProviderError` (or a subclass)
    for upstream failures so the service can map them to public responses.
    """

    name: str
    supports_evaluate: bool

    async def create_session(self) -> ProviderSession:
        """Create a fresh provider session (browser context)."""
        ...

    async def close_session(self, provider_session_id: str) -> None:
        """Close a provider session and release its resources."""
        ...

    async def create_tab(self, provider_session_id: str, url: str | None = None) -> ProviderTab:
        """Create a tab in a session, optionally navigating to ``url``."""
        ...

    async def close_tab(self, provider_session_id: str, provider_tab_id: str) -> None:
        """Close a tab in a session."""
        ...

    async def navigate(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        url: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderNavigateResult:
        """Navigate a tab to ``url``."""
        ...

    async def snapshot(
        self, provider_session_id: str, provider_tab_id: str, *, offset: int = 0
    ) -> ProviderSnapshot:
        """Fetch the accessibility-tree snapshot (with element refs)."""
        ...

    async def get_links(
        self, provider_session_id: str, provider_tab_id: str, *, limit: int = 50
    ) -> ProviderLinks:
        """Extract page links."""
        ...

    async def click(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        *,
        ref: str | None = None,
        selector: str | None = None,
    ) -> ProviderActionResult:
        """Click an element by snapshot ``ref`` or CSS selector."""
        ...

    async def type_text(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        text: str,
        *,
        ref: str | None = None,
        selector: str | None = None,
    ) -> ProviderActionResult:
        """Type text into the active or addressed element."""
        ...

    async def evaluate(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        expression: str,
        *,
        timeout_ms: int | None = None,
    ) -> ProviderEvaluateResult:
        """Evaluate JavaScript in the page context."""
        ...

    async def health(self) -> ProviderHealth:
        """Cheap availability probe (never raises; returns ``available=False``)."""
        ...
