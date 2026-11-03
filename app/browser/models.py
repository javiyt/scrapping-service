"""Project-owned request/response models for the interactive agent browser API.

These are the stable public contract of ``/v1/browser/...``.  They carry
only scrapping-service-owned identifiers (``session_id`` / ``tab_id``) —
never upstream provider identifiers such as Camofox ``userId``/``sessionKey``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "BrowserProviderMetadata",
    "BrowserSession",
    "BrowserTab",
    "SnapshotRequest",
    "SnapshotResponse",
    "BrowserNavigateRequest",
    "BrowserNavigateResponse",
    "BrowserClickRequest",
    "BrowserClickResponse",
    "BrowserTypeRequest",
    "BrowserTypeResponse",
    "BrowserEvaluateRequest",
    "BrowserEvaluateResponse",
    "BrowserLink",
    "BrowserSessionCreatedResponse",
    "BrowserSessionDeletedResponse",
    "BrowserTabCreatedResponse",
    "BrowserTabDeletedResponse",
    "BrowserCapabilitiesResponse",
]


class BrowserProviderMetadata(BaseModel):
    """Which backend provider is serving a session (never raw upstream IDs)."""

    provider: str


class BrowserTab(BaseModel):
    """A service-owned tab inside an agent browser session."""

    tab_id: str
    url: str | None = None
    title: str | None = None
    created_at: str | None = None


class BrowserSession(BaseModel):
    """A service-owned interactive browser session (public view)."""

    session_id: str
    provider: str
    tabs: list[BrowserTab] = Field(default_factory=list)
    created_at: str | None = None
    last_activity_at: str | None = None
    expires_at: str | None = None


class SnapshotRequest(BaseModel):
    """Request body for a page snapshot (optional parameters)."""

    include_links: bool = False
    links_limit: int = Field(default=50, ge=1, le=500)


class BrowserLink(BaseModel):
    """A single link extracted from a page."""

    url: str
    text: str = ""


class SnapshotResponse(BaseModel):
    """Normalized page snapshot for agents.

    Carries the accessible text/tree with stable element refs, the current
    URL and title, and provider metadata.  Never exposes raw upstream blobs.
    """

    session_id: str
    tab_id: str
    url: str | None = None
    title: str | None = None
    snapshot: str = ""
    refs_count: int = 0
    truncated: bool = False
    has_more: bool = False
    next_offset: int | None = None
    links: list[BrowserLink] = Field(default_factory=list)
    provider: BrowserProviderMetadata


class BrowserNavigateRequest(BaseModel):
    """Navigate a tab to a URL."""

    url: str
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)


class BrowserNavigateResponse(BaseModel):
    """Result of a navigation."""

    session_id: str
    tab_id: str
    ok: bool
    url: str | None = None
    title: str | None = None
    provider: BrowserProviderMetadata


class BrowserClickRequest(BaseModel):
    """Click an element by its snapshot ``ref`` or a CSS selector."""

    ref: str | None = None
    selector: str | None = None


class BrowserClickResponse(BaseModel):
    """Result of a click."""

    session_id: str
    tab_id: str
    ok: bool
    url: str | None = None
    provider: BrowserProviderMetadata


class BrowserTypeRequest(BaseModel):
    """Type text into the active element (or one addressed by ref/selector)."""

    text: str
    ref: str | None = None
    selector: str | None = None


class BrowserTypeResponse(BaseModel):
    """Result of a type action."""

    session_id: str
    tab_id: str
    ok: bool
    url: str | None = None
    provider: BrowserProviderMetadata


class BrowserEvaluateRequest(BaseModel):
    """Evaluate JavaScript in the page context (only when enabled)."""

    expression: str
    timeout_ms: int | None = Field(default=None, gt=0, le=120000)


class BrowserEvaluateResponse(BaseModel):
    """Result of a JavaScript evaluation."""

    session_id: str
    tab_id: str
    ok: bool
    result: object | None = None
    result_type: str | None = None
    truncated: bool = False
    error: str | None = None
    error_type: str | None = None
    provider: BrowserProviderMetadata


class BrowserSessionCreatedResponse(BaseModel):
    """Response for creating a session."""

    session: BrowserSession


class BrowserSessionDeletedResponse(BaseModel):
    """Response for deleting a session."""

    session_id: str
    message: str


class BrowserTabCreatedResponse(BaseModel):
    """Response for creating a tab (optionally opening a URL)."""

    session_id: str
    tab: BrowserTab


class BrowserTabDeletedResponse(BaseModel):
    """Response for deleting a tab."""

    session_id: str
    tab_id: str
    message: str


class BrowserCapabilitiesResponse(BaseModel):
    """Service-level capabilities for the interactive browser API."""

    enabled: bool
    provider: str | None = None
    evaluate_enabled: bool = False
    max_sessions: int
    max_tabs_per_session: int
    session_ttl_seconds: int
