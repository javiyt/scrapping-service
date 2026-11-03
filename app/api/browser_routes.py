"""FastAPI routes for the interactive agent browser API.

These routes expose the stateful browser operations agents need (create a
session, open tabs, snapshot, click, type, navigate, evaluate, close).  They
are the *only* public surface: all security/policy (ownership, SSRF, domain
rules, evaluate policy, limits, TTL) is enforced by
:class:`app.browser.service.AgentBrowserService`, and all backend mechanics
are hidden behind the provider — Camofox identifiers never appear here.

Every route requires the standard bearer API key (the router inherits the
auth dependency) and is tagged ``Browser``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.dependencies import get_auth_context, get_settings, verify_api_key
from app.auth.models import AuthContext
from app.browser import AgentBrowserService as BrowserService
from app.browser.models import (
    BrowserCapabilitiesResponse,
    BrowserClickRequest,
    BrowserEvaluateRequest,
    BrowserNavigateRequest,
    BrowserSession,
    BrowserSessionCreatedResponse,
    BrowserSessionDeletedResponse,
    BrowserTabCreatedResponse,
    BrowserTabDeletedResponse,
    BrowserTypeRequest,
    SnapshotRequest,
    SnapshotResponse,
)
from app.browser.providers.base import ProviderError
from app.core.config import Settings
from app.core.errors import (
    BrowserDisabledError,
    BrowserProviderError,
    BrowserSessionError,
    BrowserSessionExpiredError,
    BrowserSessionLimitError,
    BrowserTabError,
    BrowserTabLimitError,
    EvaluateDisabledError,
    SecurityError,
    ValidationError,
)

__all__ = ["browser_router", "get_agent_browser_service"]

logger = logging.getLogger("scraper-api.routes.browser")

browser_router = APIRouter(
    prefix="/v1/browser", tags=["Browser"], dependencies=[Depends(verify_api_key)]
)


# ------------------------------------------------------------------ deps


def get_agent_browser_service(
    request: Request,
    settings: Settings = Depends(get_settings),
    auth_context: AuthContext | None = Depends(get_auth_context),
) -> BrowserService:
    """Return the app-wide agent browser service, or ``None`` if unavailable.

    The service is built in the lifespan and stored on ``app.state``.  When
    the agent browser is disabled or no provider is ready the state holds
    ``None`` and the routes return a clean 503.
    """
    return getattr(request.app.state, "agent_browser_service", None)


def _profile(auth_context: AuthContext | None) -> str | None:
    return auth_context.profile_name if auth_context is not None else None


def _require_service(service: BrowserService | None) -> BrowserService:
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "browser_disabled",
                    "message": "The interactive browser API is not enabled or no provider is ready",
                    "details": {},
                }
            },
        )
    return service


# ------------------------------------------------------------------ sessions


@browser_router.post(
    "/sessions",
    response_model=BrowserSessionCreatedResponse,
    status_code=201,
)
async def create_session(
    request: Request,
    service: BrowserService | None = Depends(get_agent_browser_service),
    auth_context: AuthContext | None = Depends(get_auth_context),
):
    """Create a new interactive browser session owned by this profile."""
    svc = _require_service(service)
    profile = _profile(auth_context)
    try:
        session = await svc.create_session(profile)
    except BrowserSessionLimitError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    except BrowserDisabledError as exc:
        raise HTTPException(status_code=503, detail=exc.to_dict()) from exc
    except (BrowserProviderError, ProviderError) as exc:
        raise HTTPException(status_code=502, detail=_provider_error_dict(exc)) from exc
    return BrowserSessionCreatedResponse(session=session)


@browser_router.get("/sessions/{session_id}", response_model=BrowserSession)
async def get_session(
    session_id: str,
    service: BrowserService | None = Depends(get_agent_browser_service),
    auth_context: AuthContext | None = Depends(get_auth_context),
):
    """Return the current state of one of this profile's sessions."""
    svc = _require_service(service)
    profile = _profile(auth_context)
    try:
        return await svc.get_session(profile, session_id)
    except BrowserSessionError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except BrowserSessionExpiredError as exc:
        raise HTTPException(status_code=410, detail=exc.to_dict()) from exc


@browser_router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    service: BrowserService | None = Depends(get_agent_browser_service),
    auth_context: AuthContext | None = Depends(get_auth_context),
):
    """Close a session (and all its tabs) and release the browser."""
    svc = _require_service(service)
    profile = _profile(auth_context)
    try:
        sid = await svc.delete_session(profile, session_id)
    except BrowserSessionError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except BrowserSessionExpiredError as exc:
        raise HTTPException(status_code=410, detail=exc.to_dict()) from exc
    return BrowserSessionDeletedResponse(session_id=sid, message="session closed")


# ------------------------------------------------------------------ tabs


@browser_router.post(
    "/sessions/{session_id}/tabs",
    response_model=BrowserTabCreatedResponse,
    status_code=201,
)
async def create_tab(
    session_id: str,
    url: str | None = None,
    service: BrowserService | None = Depends(get_agent_browser_service),
    auth_context: AuthContext | None = Depends(get_auth_context),
):
    """Create a tab in a session, optionally opening ``url``.

    The URL (when given) is validated against the profile's SSRF and
    domain-allowlist rules.
    """
    svc = _require_service(service)
    profile = _profile(auth_context)
    try:
        tab = await svc.create_tab(profile, session_id, url)
    except BrowserSessionError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except BrowserSessionExpiredError as exc:
        raise HTTPException(status_code=410, detail=exc.to_dict()) from exc
    except BrowserTabLimitError as exc:
        raise HTTPException(status_code=409, detail=exc.to_dict()) from exc
    except SecurityError as exc:
        raise HTTPException(status_code=403, detail=exc.to_dict()) from exc
    except (BrowserProviderError, ProviderError) as exc:
        raise HTTPException(status_code=502, detail=_provider_error_dict(exc)) from exc
    return BrowserTabCreatedResponse(session_id=session_id, tab=tab)


@browser_router.delete("/sessions/{session_id}/tabs/{tab_id}")
async def delete_tab(
    session_id: str,
    tab_id: str,
    service: BrowserService | None = Depends(get_agent_browser_service),
    auth_context: AuthContext | None = Depends(get_auth_context),
):
    """Close a single tab in a session."""
    svc = _require_service(service)
    profile = _profile(auth_context)
    try:
        tid = await svc.delete_tab(profile, session_id, tab_id)
    except BrowserSessionError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except BrowserSessionExpiredError as exc:
        raise HTTPException(status_code=410, detail=exc.to_dict()) from exc
    except BrowserTabError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except (BrowserProviderError, ProviderError) as exc:
        raise HTTPException(status_code=502, detail=_provider_error_dict(exc)) from exc
    return BrowserTabDeletedResponse(session_id=session_id, tab_id=tid, message="tab closed")


# ------------------------------------------------------------------ snapshot


@browser_router.get(
    "/sessions/{session_id}/tabs/{tab_id}/snapshot",
    response_model=SnapshotResponse,
)
async def snapshot(
    session_id: str,
    tab_id: str,
    service: BrowserService | None = Depends(get_agent_browser_service),
    auth_context: AuthContext | None = Depends(get_auth_context),
    include_links: bool = Query(False),
    links_limit: int = Query(50, ge=1, le=500),
):
    """Return a normalized page snapshot (accessible tree with element refs)."""
    svc = _require_service(service)
    profile = _profile(auth_context)
    try:
        return await svc.snapshot(
            profile,
            session_id,
            tab_id,
            SnapshotRequest(include_links=include_links, links_limit=links_limit),
        )
    except BrowserSessionError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except BrowserSessionExpiredError as exc:
        raise HTTPException(status_code=410, detail=exc.to_dict()) from exc
    except BrowserTabError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except (BrowserProviderError, ProviderError) as exc:
        raise HTTPException(status_code=502, detail=_provider_error_dict(exc)) from exc


# ------------------------------------------------------------------ actions


@browser_router.post("/sessions/{session_id}/tabs/{tab_id}/navigate")
async def navigate(
    session_id: str,
    tab_id: str,
    body: BrowserNavigateRequest,
    service: BrowserService | None = Depends(get_agent_browser_service),
    auth_context: AuthContext | None = Depends(get_auth_context),
):
    """Navigate a tab to a URL (validated by the profile's security rules)."""
    svc = _require_service(service)
    profile = _profile(auth_context)
    try:
        result = await svc.navigate(
            profile,
            session_id,
            tab_id,
            body.url,
            timeout_seconds=body.timeout_seconds,
        )
    except BrowserSessionError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except BrowserSessionExpiredError as exc:
        raise HTTPException(status_code=410, detail=exc.to_dict()) from exc
    except BrowserTabError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except SecurityError as exc:
        raise HTTPException(status_code=403, detail=exc.to_dict()) from exc
    except (BrowserProviderError, ProviderError) as exc:
        raise HTTPException(status_code=502, detail=_provider_error_dict(exc)) from exc
    return {
        "session_id": session_id,
        "tab_id": tab_id,
        "ok": result.ok,
        "url": result.url,
        "title": result.title,
        "provider": {"provider": svc.provider.name},
    }


@browser_router.post("/sessions/{session_id}/tabs/{tab_id}/click")
async def click(
    session_id: str,
    tab_id: str,
    body: BrowserClickRequest,
    service: BrowserService | None = Depends(get_agent_browser_service),
    auth_context: AuthContext | None = Depends(get_auth_context),
):
    """Click an element by snapshot ``ref`` or CSS selector."""
    svc = _require_service(service)
    profile = _profile(auth_context)
    try:
        result = await svc.click(profile, session_id, tab_id, ref=body.ref, selector=body.selector)
    except BrowserSessionError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except BrowserSessionExpiredError as exc:
        raise HTTPException(status_code=410, detail=exc.to_dict()) from exc
    except BrowserTabError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except (BrowserProviderError, ProviderError) as exc:
        raise HTTPException(status_code=502, detail=_provider_error_dict(exc)) from exc
    return {
        "session_id": session_id,
        "tab_id": tab_id,
        "ok": result.ok,
        "url": result.url,
        "provider": {"provider": svc.provider.name},
    }


@browser_router.post("/sessions/{session_id}/tabs/{tab_id}/type")
async def type_text(
    session_id: str,
    tab_id: str,
    body: BrowserTypeRequest,
    service: BrowserService | None = Depends(get_agent_browser_service),
    auth_context: AuthContext | None = Depends(get_auth_context),
):
    """Type text into the active or addressed element."""
    svc = _require_service(service)
    profile = _profile(auth_context)
    try:
        result = await svc.type_text(
            profile, session_id, tab_id, body.text, ref=body.ref, selector=body.selector
        )
    except BrowserSessionError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except BrowserSessionExpiredError as exc:
        raise HTTPException(status_code=410, detail=exc.to_dict()) from exc
    except BrowserTabError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except (BrowserProviderError, ProviderError) as exc:
        raise HTTPException(status_code=502, detail=_provider_error_dict(exc)) from exc
    return {
        "session_id": session_id,
        "tab_id": tab_id,
        "ok": result.ok,
        "url": result.url,
        "provider": {"provider": svc.provider.name},
    }


@browser_router.post("/sessions/{session_id}/tabs/{tab_id}/evaluate")
async def evaluate(
    session_id: str,
    tab_id: str,
    body: BrowserEvaluateRequest,
    service: BrowserService | None = Depends(get_agent_browser_service),
    auth_context: AuthContext | None = Depends(get_auth_context),
):
    """Evaluate JavaScript in the page context (only when enabled for the profile)."""
    svc = _require_service(service)
    profile = _profile(auth_context)
    try:
        result = await svc.evaluate(
            profile, session_id, tab_id, body.expression, timeout_ms=body.timeout_ms
        )
    except BrowserSessionError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except BrowserSessionExpiredError as exc:
        raise HTTPException(status_code=410, detail=exc.to_dict()) from exc
    except BrowserTabError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc
    except EvaluateDisabledError as exc:
        raise HTTPException(status_code=403, detail=exc.to_dict()) from exc
    except (BrowserProviderError, ProviderError) as exc:
        raise HTTPException(status_code=502, detail=_provider_error_dict(exc)) from exc
    return {
        "session_id": session_id,
        "tab_id": tab_id,
        "ok": result.ok,
        "result": result.result,
        "result_type": result.result_type,
        "truncated": result.truncated,
        "error": result.error,
        "error_type": result.error_type,
        "provider": {"provider": svc.provider.name},
    }


# ------------------------------------------------------------------ capabilities


@browser_router.get("/capabilities", response_model=BrowserCapabilitiesResponse)
async def capabilities(
    request: Request,
    settings: Settings = Depends(get_settings),
    auth_context: AuthContext | None = Depends(get_auth_context),
):
    """Report the interactive browser capability/limits for this profile."""
    profile = _profile(auth_context)
    effective = settings
    if profile:
        try:
            from app.auth.resolver import get_profile_resolver

            effective = get_profile_resolver().effective_settings_for(profile)
        except Exception:  # noqa: BLE001
            effective = settings
    service: BrowserService | None = getattr(request.app.state, "agent_browser_service", None)
    return BrowserCapabilitiesResponse(
        enabled=service is not None and service._base_config.enabled,
        provider=service.provider.name if service is not None else None,
        evaluate_enabled=effective.agent_browser_allow_evaluate,
        max_sessions=effective.agent_browser_max_sessions,
        max_tabs_per_session=effective.agent_browser_max_tabs_per_session,
        session_ttl_seconds=effective.agent_browser_session_ttl_seconds,
    )


# ------------------------------------------------------------------ helpers


def _provider_error_dict(exc: Exception) -> dict[str, Any]:
    message = getattr(exc, "message", None) or str(exc)
    details = getattr(exc, "details", None) or {}
    return {"error": {"type": "browser_provider_error", "message": message, "details": details}}
