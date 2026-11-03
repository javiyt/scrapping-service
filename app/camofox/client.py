"""Typed async client for the external Camofox browser REST service.

Camofox (``camofox-browser``) is a long-running external service; this module
is the *single* place in the codebase that knows its REST contract.  The
service is never embedded in or launched by this application — everything
goes through plain HTTP.

Design (mirrors ``app.search.providers.searxng``):

* All upstream JSON is parsed into the project-owned models in
  :mod:`app.camofox.types`.  Unknown fields are ignored.
* **Auth**: routes are called with a *scope*.  General routes use
  ``access_key`` (falling back to ``api_key``); sensitive routes (cookie
  import) prefer ``api_key`` (falling back to ``access_key``); the health
  probe sends no credentials.  Credentials are never logged and never appear
  in raised errors.
* **Error mapping**: upstream failures are translated into the project-owned
  Camofox error types from :mod:`app.core.errors`:

  =======================  ===============================================
  Upstream situation       Error type
  =======================  ===============================================
  connection failure / 5xx :class:`CamofoxUnavailableError` (503)
  client-side timeout,
  or server timeout 408/500 :class:`CamofoxTimeoutError` (504)
  HTTP 401 / 403           :class:`CamofoxAuthError` (502)
  unparseable / wrong-shape
  JSON                     :class:`CamofoxInvalidResponseError` (502)
  HTTP 404 "Tab not found" :class:`CamofoxTabNotFoundError` (502)
  session gone (HTTP 409,
  no active session)       :class:`CamofoxSessionExpiredError` (502)
  navigation failed        :class:`CamofoxNavigationError` (502)
  anything else            :class:`CamofoxError` (502)
  =======================  ===============================================

  Public error messages carry only the endpoint and HTTP status; upstream
  response text is logged (server-side) instead, so the exceptions are safe
  to render into HTTP responses.
* **Health**: :meth:`CamofoxClient.probe_health` is a single unauthenticated
  ``GET /health`` — cheap, and it never creates a browser tab.  Failures are
  returned as ``available=False`` with a reason, never raised.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from app.camofox.config import CamofoxConfig
from app.camofox.session import CamofoxSession
from app.camofox.types import (
    CamofoxActionResult,
    CamofoxCookie,
    CamofoxEvaluateResult,
    CamofoxHealth,
    CamofoxHealthStatus,
    CamofoxLink,
    CamofoxLinks,
    CamofoxNavigateResult,
    CamofoxSnapshot,
    CamofoxTab,
    CamofoxTabInfo,
)
from app.core.errors import (
    CamofoxAuthError,
    CamofoxError,
    CamofoxInvalidResponseError,
    CamofoxNavigationError,
    CamofoxSessionExpiredError,
    CamofoxTabNotFoundError,
    CamofoxTimeoutError,
    CamofoxUnavailableError,
    ValidationError,
)

__all__ = ["CamofoxClient"]

logger = logging.getLogger("scraper-api.camofox")

# Upstream error markers (matched case-insensitively against the server's
# ``error`` strings).
_TIMEOUT_MARKERS = ("timed out", "timeout")
_SESSION_GONE_MARKERS = ("no active session", "no canonical profile", "session closed")

_NAME_SLUG_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")


def _first_str(*values: Any) -> str | None:
    """Return the first value that is a non-empty string, else None."""
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _first_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _looks_like_timeout(upstream_error: str | None) -> bool:
    if not upstream_error:
        return False
    lowered = upstream_error.lower()
    return any(marker in lowered for marker in _TIMEOUT_MARKERS)


def _is_tab_not_found(upstream_error: str | None) -> bool:
    return bool(upstream_error) and "tab not found" in upstream_error.lower()


def _is_session_gone(upstream_error: str | None) -> bool:
    if not upstream_error:
        return False
    lowered = upstream_error.lower()
    return any(marker in lowered for marker in _SESSION_GONE_MARKERS)


def _extract_upstream_error(response: httpx.Response) -> str | None:
    """Extract the upstream ``error`` string for logging. Never raised."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    return error if isinstance(error, str) and error else None


def _parse_snapshot(payload: dict[str, Any]) -> CamofoxSnapshot:
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, str):
        raise CamofoxInvalidResponseError(
            "Camofox snapshot response is missing the snapshot text",
            details={"endpoint": "snapshot"},
        )
    total_chars = _first_int(payload.get("totalChars"))
    has_more = bool(payload.get("hasMore"))
    next_offset = _first_int(payload.get("nextOffset"))
    return CamofoxSnapshot(
        url=_first_str(payload.get("url")),
        snapshot=snapshot,
        refs_count=_first_int(payload.get("refsCount")) or 0,
        offset=_first_int(payload.get("offset")) or 0,
        truncated=bool(payload.get("truncated")),
        total_chars=total_chars if total_chars is not None else len(snapshot),
        has_more=has_more,
        next_offset=next_offset if next_offset is not None and has_more else None,
    )


def _parse_links(payload: Any) -> CamofoxLinks:
    """Parse the links payload — the server returns an object; be lenient."""
    if isinstance(payload, dict):
        raw_links = payload.get("links")
        pagination = payload.get("pagination")
    elif isinstance(payload, list):
        raw_links = payload
        pagination = None
    else:
        raw_links = None
        pagination = None

    if not isinstance(raw_links, list):
        raise CamofoxInvalidResponseError(
            "Camofox links response has no links array",
            details={"endpoint": "links"},
        )

    links: list[CamofoxLink] = []
    for entry in raw_links:
        if isinstance(entry, str):
            links.append(CamofoxLink(url=entry))
        elif isinstance(entry, dict):
            url = _first_str(entry.get("url"), entry.get("href"))
            if url:
                links.append(
                    CamofoxLink(
                        url=url,
                        text=_first_str(entry.get("text")) or "",
                    )
                )

    total = _first_int(pagination.get("total")) if isinstance(pagination, dict) else None
    return CamofoxLinks(
        links=links,
        total=total if total is not None else len(links),
        offset=_first_int(pagination.get("offset")) or 0 if isinstance(pagination, dict) else 0,
        limit=_first_int(pagination.get("limit")) or 0 if isinstance(pagination, dict) else 0,
        has_more=bool(pagination.get("hasMore")) if isinstance(pagination, dict) else False,
    )


class CamofoxClient:
    """Typed async client for the external Camofox browser REST service.

    The client owns the ``httpx.AsyncClient`` (when constructed without an
    injected one) and must be closed on application shutdown.  For testability
    (and for connection pooling across consumers) an existing
    ``httpx.AsyncClient`` may be injected; it is *not* closed in that case.

    Every operation takes the :class:`CamofoxSession` it belongs to, so
    callers never hand out raw ``userId``/``sessionKey``/``tabId`` strings.
    """

    def __init__(self, config: CamofoxConfig, client: httpx.AsyncClient | None = None) -> None:
        if not config.base_url:
            raise ValueError("Camofox base_url must be configured")
        self._config = config
        self._owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                base_url=config.base_url,
                timeout=httpx.Timeout(config.timeout_seconds),
                headers={"Accept": "application/json"},
                follow_redirects=False,
            )
        self._client = client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def config(self) -> CamofoxConfig:
        return self._config

    @property
    def base_url(self) -> str:
        return self._config.base_url or ""

    async def close(self) -> None:
        """Close the owned HTTP client.  Safe to call multiple times."""
        if self._owns_client:
            await self._client.aclose()
            self._owns_client = False

    async def __aenter__(self) -> CamofoxClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def make_user_id(self, name: str | None = None) -> str:
        """Build a unique ``userId`` from the configured prefix.

        Named sessions get a stable slug suffix (``scrapping-service-melissa``);
        anonymous ones get a random suffix so concurrent sessions never collide.
        """
        if name:
            slug = _NAME_SLUG_PATTERN.sub("-", name.strip()).strip("-")[:48]
            if not slug:
                slug = uuid.uuid4().hex[:12]
        else:
            slug = uuid.uuid4().hex[:12]
        return f"{self._config.user_id_prefix}-{slug}"

    def create_session(
        self,
        name: str | None = None,
        *,
        user_id: str | None = None,
        session_key: str | None = None,
    ) -> CamofoxSession:
        """Create a logical session (no HTTP involved).

        The session is *not* created upstream until its first operation:
        Camofox materialises the browser on first use (``POST /tabs``).
        """
        return CamofoxSession(self, name=name, user_id=user_id, session_key=session_key)

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def probe_health(self) -> CamofoxHealthStatus:
        """Cheap capability probe: a single unauthenticated ``GET /health``.

        No browser tab is created and no credentials are sent, so this is
        safe for readiness/deep-health integration.  Failures (connection
        refused, timeouts, server errors, malformed payloads) are returned as
        ``available=False`` with a non-secret ``reason`` — never raised.
        """
        try:
            payload = await self._request("GET", "/health", auth=None)
        except CamofoxError as exc:
            return CamofoxHealthStatus(available=False, reason=exc.message)
        try:
            health = CamofoxHealth.model_validate(payload)
        except Exception:  # noqa: BLE001 - malformed payload must not raise
            return CamofoxHealthStatus(available=False, reason="invalid health payload")
        available = bool(health.ok and health.running)
        return CamofoxHealthStatus(
            available=available,
            reason=None if available else "server reported not running",
            version=health.version,
            browser_connected=health.browser_connected,
            pool_size=health.pool_size,
            active_user_count=health.active_user_count,
        )

    # ------------------------------------------------------------------
    # Tabs
    # ------------------------------------------------------------------

    async def create_tab(self, session: CamofoxSession, url: str | None = None) -> CamofoxTab:
        """Create a new tab in the session (the browser starts lazily)."""
        body: dict[str, Any] = {
            "userId": session.user_id,
            "sessionKey": session.session_key,
        }
        if url:
            body["url"] = url
        payload = await self._request("POST", "/tabs", auth="general", json_body=body)

        tab_id = _first_str(payload.get("tabId"))
        if not tab_id:
            raise CamofoxInvalidResponseError(
                "Camofox tab creation returned no tabId",
                details={"endpoint": "/tabs"},
            )
        tab = CamofoxTab(
            tab_id=tab_id,
            url=_first_str(payload.get("url"), body.get("url")),
            title=_first_str(payload.get("title")),
        )
        session.add_tab(tab)
        return tab

    async def list_tabs(self, session: CamofoxSession) -> list[CamofoxTabInfo]:
        """List the session user's tabs (grouped by the session key upstream)."""
        payload = await self._request(
            "GET", "/tabs", auth="general", params={"userId": session.user_id}
        )
        raw_tabs = payload.get("tabs")
        if not isinstance(raw_tabs, list):
            raise CamofoxInvalidResponseError(
                "Camofox tab listing returned no tabs array",
                details={"endpoint": "/tabs"},
            )
        tabs: list[CamofoxTabInfo] = []
        for entry in raw_tabs:
            if isinstance(entry, dict):
                tab_id = _first_str(entry.get("tabId"), entry.get("targetId"))
                if tab_id:
                    tabs.append(
                        CamofoxTabInfo(
                            tab_id=tab_id,
                            url=_first_str(entry.get("url")),
                            title=_first_str(entry.get("title")),
                            session_key=_first_str(entry.get("listItemId")),
                        )
                    )
            elif isinstance(entry, str):
                tabs.append(CamofoxTabInfo(tab_id=entry))
        session.upsert_tabs(tabs)
        return tabs

    async def close_tab(self, session: CamofoxSession, tab: CamofoxTab) -> None:
        """Close a tab (idempotent: closing an already-closed tab is fine)."""
        session.assert_owns(tab)
        try:
            await self._request(
                "DELETE",
                f"/tabs/{quote(tab.tab_id, safe='')}",
                auth="general",
                json_body={"userId": session.user_id},
            )
        except CamofoxTabNotFoundError:
            pass  # already gone upstream — the desired end state
        session.forget_tab(tab.tab_id)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(
        self,
        session: CamofoxSession,
        tab: CamofoxTab,
        url: str | None = None,
        *,
        macro: str | None = None,
        engine: str | None = None,
        query: str | None = None,
    ) -> CamofoxNavigateResult:
        """Navigate a tab to a URL, or run a search macro.

        ``engine`` is shorthand for ``macro="@<engine>_search"`` (CamelCase
        macro names such as ``@google_search`` / ``@youtube_search`` may be
        passed via ``macro`` directly).  ``query`` is the search term.
        """
        session.assert_owns(tab)
        if macro is None and engine is not None:
            macro = f"@{engine}_search"
        if url is None and macro is None:
            raise ValidationError("navigate requires a url, a macro, or an engine")

        body: dict[str, Any] = {"userId": session.user_id}
        if url is not None:
            body["url"] = url
        if macro is not None:
            body["macro"] = macro
            if query is not None:
                body["query"] = query

        try:
            payload = await self._request(
                "POST",
                f"/tabs/{quote(tab.tab_id, safe='')}/navigate",
                auth="general",
                json_body=body,
            )
        except CamofoxError as exc:
            if isinstance(
                exc,
                (
                    CamofoxTabNotFoundError,
                    CamofoxSessionExpiredError,
                    CamofoxAuthError,
                    CamofoxTimeoutError,
                ),
            ):
                self._maybe_invalidate_tab(session, exc)
                raise
            # Any other failure of the navigation request (e.g. a 5xx such as
            # "Blocked request to private IP") → navigation failed.
            self._maybe_invalidate_tab(session, exc)
            raise CamofoxNavigationError(
                "Camofox navigation failed",
                details={**(exc.details or {}), "url": url, "macro": macro},
            ) from exc

        result = CamofoxNavigateResult(
            ok=bool(payload.get("ok", True)),
            url=_first_str(payload.get("url")),
            title=_first_str(payload.get("title")),
        )
        if result.url:
            tab.url = result.url
        return result

    # ------------------------------------------------------------------
    # Page data
    # ------------------------------------------------------------------

    async def snapshot(
        self, session: CamofoxSession, tab: CamofoxTab, offset: int = 0
    ) -> CamofoxSnapshot:
        """Fetch the accessibility-tree snapshot (windowed with element refs)."""
        session.assert_owns(tab)
        params: dict[str, Any] = {"userId": session.user_id}
        if offset:
            params["offset"] = offset
        try:
            payload = await self._request(
                "GET",
                f"/tabs/{quote(tab.tab_id, safe='')}/snapshot",
                auth="general",
                params=params,
            )
        except CamofoxError as exc:
            self._maybe_invalidate_tab(session, exc)
            raise
        return _parse_snapshot(payload)

    async def get_links(
        self, session: CamofoxSession, tab: CamofoxTab, *, limit: int = 50, offset: int = 0
    ) -> CamofoxLinks:
        """Extract page links (with pagination)."""
        session.assert_owns(tab)
        params: dict[str, Any] = {
            "userId": session.user_id,
            "limit": limit,
            "offset": offset,
        }
        try:
            payload = await self._request(
                "GET",
                f"/tabs/{quote(tab.tab_id, safe='')}/links",
                auth="general",
                params=params,
                accept_list=True,
            )
        except CamofoxError as exc:
            self._maybe_invalidate_tab(session, exc)
            raise
        return _parse_links(payload)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def click(
        self,
        session: CamofoxSession,
        tab: CamofoxTab,
        *,
        ref: str | None = None,
        selector: str | None = None,
    ) -> CamofoxActionResult:
        """Click an element by its snapshot ``ref`` or a CSS selector."""
        session.assert_owns(tab)
        if ref is None and selector is None:
            raise ValidationError("click requires a ref or a selector")
        body: dict[str, Any] = {"userId": session.user_id}
        if ref is not None:
            body["ref"] = ref
        if selector is not None:
            body["selector"] = selector
        try:
            payload = await self._request(
                "POST",
                f"/tabs/{quote(tab.tab_id, safe='')}/click",
                auth="general",
                json_body=body,
            )
        except CamofoxError as exc:
            self._maybe_invalidate_tab(session, exc)
            raise
        result = CamofoxActionResult(
            ok=bool(payload.get("ok", True)), url=_first_str(payload.get("url"))
        )
        if result.url:
            tab.url = result.url
        return result

    async def type_text(
        self,
        session: CamofoxSession,
        tab: CamofoxTab,
        text: str,
        *,
        ref: str | None = None,
        selector: str | None = None,
    ) -> CamofoxActionResult:
        """Type text into the active element or an element addressed by ref/selector."""
        session.assert_owns(tab)
        body: dict[str, Any] = {"userId": session.user_id, "text": text}
        if ref is not None:
            body["ref"] = ref
        if selector is not None:
            body["selector"] = selector
        try:
            payload = await self._request(
                "POST",
                f"/tabs/{quote(tab.tab_id, safe='')}/type",
                auth="general",
                json_body=body,
            )
        except CamofoxError as exc:
            self._maybe_invalidate_tab(session, exc)
            raise
        return CamofoxActionResult(
            ok=bool(payload.get("ok", True)), url=_first_str(payload.get("url"))
        )

    async def evaluate(
        self,
        session: CamofoxSession,
        tab: CamofoxTab,
        expression: str,
        *,
        timeout_ms: int | None = None,
    ) -> CamofoxEvaluateResult:
        """Evaluate JavaScript in the page context.

        Handled script failures are returned as ``ok=False`` results; only
        evaluation timeouts are raised (:class:`CamofoxTimeoutError`).
        """
        session.assert_owns(tab)
        body: dict[str, Any] = {"userId": session.user_id, "expression": expression}
        if timeout_ms is not None:
            body["timeout"] = timeout_ms
        try:
            payload = await self._request(
                "POST",
                f"/tabs/{quote(tab.tab_id, safe='')}/evaluate",
                auth="general",
                json_body=body,
            )
        except CamofoxError as exc:
            self._maybe_invalidate_tab(session, exc)
            raise
        result = CamofoxEvaluateResult(
            ok=bool(payload.get("ok", True)),
            result=payload.get("result"),
            result_type=_first_str(payload.get("resultType")),
            truncated=bool(payload.get("truncated")),
            error=_first_str(payload.get("error")),
            error_type=_first_str(payload.get("errorType")),
        )
        if not result.ok and result.error_type == "timeout":
            raise CamofoxTimeoutError(
                f"Camofox evaluate on tab {tab.tab_id} timed out",
                details={"endpoint": "evaluate", "tab_id": tab.tab_id},
            )
        return result

    # ------------------------------------------------------------------
    # Cookies / session teardown
    # ------------------------------------------------------------------

    async def import_cookies(
        self,
        session: CamofoxSession,
        cookies: list[CamofoxCookie],
        *,
        tab: CamofoxTab | None = None,
    ) -> int:
        """Import cookies into the session's context (sensitive route).

        Optionally scoped to a single tab.  Returns the number of cookies
        imported as reported by the server.
        """
        if not cookies:
            return 0
        body: dict[str, Any] = {"cookies": [cookie.to_wire() for cookie in cookies]}
        if tab is not None:
            session.assert_owns(tab)
            body["tabId"] = tab.tab_id
        payload = await self._request(
            "POST",
            f"/sessions/{quote(session.user_id, safe='')}/cookies",
            auth="sensitive",
            json_body=body,
        )
        count = _first_int(payload.get("count"))
        return count if count is not None else len(cookies)

    async def close_session(self, session: CamofoxSession) -> None:
        """Close the session upstream (all its tabs) and forget local tabs."""
        await self._request(
            "DELETE",
            f"/sessions/{quote(session.user_id, safe='')}",
            auth="general",
        )
        session.forget_all_tabs()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _auth_headers(self, scope: str | None) -> dict[str, str]:
        """Build the Authorization header for a route scope.

        Secrets are used only to build the header; they are never logged and
        cannot reach an exception from here.
        """
        if scope is None:
            return {}
        access_key, api_key = self._config.access_key, self._config.api_key
        key = api_key or access_key if scope == "sensitive" else access_key or api_key
        if not key:
            return {}
        return {"Authorization": f"Bearer {key}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: str | None = "general",
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        accept_list: bool = False,
    ) -> dict[str, Any] | list[Any]:
        """Perform a request and return the parsed JSON object payload.

        Raises the mapped Camofox error types (see module docstring) — never
        a raw httpx exception or an upstream payload.
        """
        headers = self._auth_headers(auth) or None
        try:
            response = await self._client.request(
                method, path, params=params, json=json_body, headers=headers
            )
        except httpx.TimeoutException as exc:
            logger.warning(
                "Camofox %s %s timed out after %.1fs", method, path, self._config.timeout_seconds
            )
            raise CamofoxTimeoutError(
                f"Camofox {path} request timed out after {self._config.timeout_seconds}s",
                details={"endpoint": path},
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "Camofox service is unreachable (%s %s): %s", method, path, type(exc).__name__
            )
            raise CamofoxUnavailableError(
                "Camofox service is unreachable", details={"endpoint": path}
            ) from exc

        status = response.status_code
        if status >= 400:
            self._raise_for_status(method, path, status, response)
        return self._parse_json(method, path, status, response, accept_list=accept_list)

    def _raise_for_status(
        self,
        method: str,
        path: str,
        status: int,
        response: httpx.Response,
    ) -> None:
        upstream_error = _extract_upstream_error(response)
        logger.warning(
            "Camofox %s %s returned HTTP %d: %s",
            method,
            path,
            status,
            upstream_error or response.text[:300],
        )
        details = {"status_code": status, "endpoint": path}

        if status in (401, 403):
            raise CamofoxAuthError("Camofox rejected the configured credentials", details=details)
        if status == 404 and _is_tab_not_found(upstream_error):
            raise CamofoxTabNotFoundError(f"Camofox tab no longer exists ({path})", details=details)
        if status == 409 and _is_session_gone(upstream_error):
            raise CamofoxSessionExpiredError(
                f"Camofox session is no longer active ({path})", details=details
            )
        if status == 408 or (status >= 500 and _looks_like_timeout(upstream_error)):
            raise CamofoxTimeoutError(f"Camofox {path} request timed out", details=details)
        if status >= 500:
            raise CamofoxUnavailableError(f"Camofox service error on {path}", details=details)
        raise CamofoxError(f"Camofox rejected the request on {path}", details=details)

    def _parse_json(
        self,
        method: str,
        path: str,
        status: int,
        response: httpx.Response,
        *,
        accept_list: bool = False,
    ) -> dict[str, Any] | list[Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning(
                "Camofox %s %s (HTTP %d) returned invalid JSON: %.200s",
                method,
                path,
                status,
                response.text,
            )
            raise CamofoxInvalidResponseError(
                f"Camofox {path} returned invalid JSON",
                details={"status_code": status, "endpoint": path},
            ) from exc
        if not isinstance(payload, (dict, list)) or (isinstance(payload, list) and not accept_list):
            logger.warning(
                "Camofox %s %s (HTTP %d) returned a non-object JSON payload", method, path, status
            )
            raise CamofoxInvalidResponseError(
                f"Camofox {path} returned a non-object JSON payload",
                details={"status_code": status, "endpoint": path},
            )
        return payload

    @staticmethod
    def _maybe_invalidate_tab(session: CamofoxSession, exc: CamofoxError) -> None:
        """Forget a locally tracked tab once the server reports it gone.

        ``CamofoxTabNotFoundError`` implies the tab died server-side; keeping
        it in the session registry would only produce repeated failures.
        """
        if not isinstance(exc, CamofoxTabNotFoundError):
            return
        endpoint = (exc.details or {}).get("endpoint", "")
        if "/tabs/" not in endpoint:
            return
        tab_id = endpoint.split("/tabs/", 1)[1].split("/", 1)[0]
        session.forget_tab(tab_id)
