"""Project-owned response types for the Camofox REST client.

These models are the only Camofox shapes that application code may use.
All upstream JSON parsing lives in :mod:`app.camofox.client`; everything
Camofox-specific (field names, endpoint shapes) stays inside the adapter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CamofoxActionResult",
    "CamofoxCookie",
    "CamofoxHealth",
    "CamofoxHealthStatus",
    "CamofoxLink",
    "CamofoxLinks",
    "CamofoxNavigateResult",
    "CamofoxEvaluateResult",
    "CamofoxSnapshot",
    "CamofoxTab",
    "CamofoxTabInfo",
    "CamofoxTranscript",
]


class _CamofoxModel(BaseModel):
    """Base for parsed Camofox payloads — unknown upstream fields are ignored."""

    model_config = ConfigDict(extra="ignore")


class CamofoxTab(_CamofoxModel):
    """A tab tracked by a :class:`app.camofox.session.CamofoxSession`."""

    tab_id: str
    url: str | None = None
    title: str | None = None


class CamofoxTabInfo(_CamofoxModel):
    """A tab as reported by the server's tab listing (``GET /tabs``)."""

    tab_id: str
    url: str | None = None
    title: str | None = None
    session_key: str | None = Field(
        default=None,
        description="Upstream ``listItemId``: the session-key group the tab belongs to.",
    )


class CamofoxHealth(_CamofoxModel):
    """Parsed payload of the server-level health endpoint (``GET /health``)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    ok: bool = False
    running: bool | None = None
    engine: str | None = None
    version: str | None = None
    browser_connected: bool = Field(default=False, alias="browserConnected")
    pool_size: int | None = Field(default=None, alias="poolSize")
    active_user_count: int = Field(default=0, alias="activeUserCount")


class CamofoxHealthStatus(BaseModel):
    """Result of the cheap capability/health probe.

    The probe is a single unauthenticated ``GET /health`` — it never creates
    a browser tab.  Failures are returned (``available=False`` + ``reason``),
    never raised, so it can be integrated into ``/ready`` / deep health.
    """

    available: bool = False
    reason: str | None = None
    version: str | None = None
    browser_connected: bool | None = None
    pool_size: int | None = None
    active_user_count: int | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CamofoxNavigateResult(_CamofoxModel):
    """Result of a tab navigation (URL or search macro)."""

    ok: bool = False
    url: str | None = None
    title: str | None = None


class CamofoxSnapshot(_CamofoxModel):
    """Accessibility-tree snapshot of a tab, with element refs."""

    url: str | None = None
    snapshot: str = ""
    refs_count: int = 0
    offset: int = 0
    truncated: bool = False
    total_chars: int = 0
    has_more: bool = False
    next_offset: int | None = None


class CamofoxLink(_CamofoxModel):
    """One link extracted from a page."""

    url: str
    text: str = ""


class CamofoxLinks(_CamofoxModel):
    """Page links extracted by the server (``GET /tabs/:id/links``)."""

    links: list[CamofoxLink] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 0
    has_more: bool = False


class CamofoxActionResult(_CamofoxModel):
    """Result of a click or type action."""

    ok: bool = False
    url: str | None = None


class CamofoxEvaluateResult(_CamofoxModel):
    """Result of a JavaScript evaluation in the page context.

    Handled script failures (``ok=False``) are returned, not raised, except
    timeouts (``error_type == "timeout"``), which surface as
    :class:`app.core.errors.CamofoxTimeoutError`.
    """

    ok: bool = False
    result: Any = None
    result_type: str | None = None
    truncated: bool = False
    error: str | None = None
    error_type: str | None = None


class CamofoxCookie(BaseModel):
    """Project-owned cookie model for import (Playwright-compatible fields).

    Unknown extra fields are dropped when the cookie is serialized for the
    wire, mirroring the sanitisation the Camofox server performs.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    value: str
    domain: str
    path: str | None = None
    expires: float | None = None
    http_only: bool | None = None
    secure: bool | None = None
    same_site: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Serialize to the exact field names the Camofox server accepts.

        The server keeps only ``name``/``value``/``domain``/``path``/
        ``expires``/``httpOnly``/``secure``/``sameSite``; ``None`` values are
        omitted.  ``value`` is never logged anywhere in this package.
        """
        wire: dict[str, Any] = {"name": self.name, "value": self.value, "domain": self.domain}
        if self.path is not None:
            wire["path"] = self.path
        if self.expires is not None:
            wire["expires"] = self.expires
        if self.http_only is not None:
            wire["httpOnly"] = self.http_only
        if self.secure is not None:
            wire["secure"] = self.secure
        if self.same_site is not None:
            wire["sameSite"] = self.same_site
        return wire


class CamofoxTranscript(_CamofoxModel):
    """Parsed result of ``POST /youtube/transcript``.

    A successful result (``ok=True``) carries the ``transcript`` text — a
    plain text with ``[mm:ss]`` / ``[hh:mm:ss]`` line prefixes — plus the
    video title, caption language and word count when the server reports
    them.  Upstream *error* results (``ok=False``; e.g. no captions for the
    video) carry the server's ``code`` / ``message``.  The transcript text
    is never logged; only its length feeds diagnostics.
    """

    ok: bool = False
    status: str | None = None
    transcript: str | None = None
    code: int | None = None
    message: str | None = None
    video_id: str | None = None
    video_title: str | None = None
    language: str | None = None
    total_words: int | None = None

    @property
    def transcript_length(self) -> int:
        """Length of the transcript text (``0`` for error results)."""
        return len(self.transcript) if self.transcript is not None else 0
