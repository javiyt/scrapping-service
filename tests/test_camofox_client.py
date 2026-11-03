"""Tests for the Camofox REST client (fully offline via ``httpx.MockTransport``).

The fake server emulates the real contract closely enough for the client to
exercise: auth gating (Bearer tokens), tab creation/navigation/snapshot/links/
click/type/evaluate/close, cookie import, session teardown, the windowed
snapshot payload, and the upstream error shapes (``{"error": ...}`` with
status codes).
"""

import asyncio
import json
import re

import httpx
import pytest

from app.camofox import CamofoxClient
from app.camofox.config import CamofoxConfig
from app.camofox.session import CamofoxSession
from app.camofox.types import CamofoxCookie
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

BASE_URL = "http://camofox:9377"
ACCESS_KEY = "access-secret-token-1"
API_KEY = "api-secret-token-2"


def _config(**overrides) -> CamofoxConfig:
    values = dict(
        enabled=True,
        base_url=BASE_URL,
        timeout_seconds=10.0,
        access_key=ACCESS_KEY,
        api_key=API_KEY,
    )
    values.update(overrides)
    return CamofoxConfig(**values)


class MockCamofox:
    """A fake Camofox server backing an injected ``httpx.AsyncClient``.

    Records every request (so tests can assert the auth headers and the
    identity fields the client sends) and serves configurable responses.
    When ``access_key``/``api_key`` are set, gated routes enforce a matching
    Bearer token, mirroring the real server's auth behaviour.
    """

    def __init__(
        self,
        *,
        access_key: str | None = None,
        api_key: str | None = None,
        status: int = 200,
        payload: dict | None = None,
        error: str | None = None,
        body: bytes | None = None,
        effect: Exception | None = None,
        health_payload: dict | None = None,
        health_status: int = 200,
        health_body: bytes | None = None,
        health_effect: Exception | None = None,
        tab_id: str | None = None,
        route: str | None = None,
    ) -> None:
        self.access_key = access_key
        self.api_key = api_key
        self.status = status
        self.payload = payload
        self.error = error
        self.body = body
        self.effect = effect
        self.health_payload = health_payload
        self.health_status = health_status
        self.health_body = health_body
        self.health_effect = health_effect
        self.tab_id = tab_id
        # When set, status/payload/error/body overrides apply only to routes
        # whose path ends with this suffix (e.g. "/snapshot") — so a tab can
        # be created with default behaviour before the interesting route fires.
        self.route = route
        self._tab_seq = 0
        self._created_tab_ids: list[str] = []
        self.requests: list[httpx.Request] = []

    # -- client factory ----------------------------------------------------

    def client(self) -> httpx.AsyncClient:
        async def async_handle(request: httpx.Request) -> httpx.Response:
            # Yield to the event loop so concurrent calls interleave like
            # real I/O would.
            await asyncio.sleep(0)
            return self._handle(request)

        return httpx.AsyncClient(
            base_url=BASE_URL,
            transport=httpx.MockTransport(async_handle),
        )

    def make_client(self) -> CamofoxClient:
        return CamofoxClient(_config(), client=self.client())

    # -- request helpers -----------------------------------------------------

    def last_request(self) -> httpx.Request:
        return self.requests[-1]

    def last_json(self) -> dict:
        return (
            json.loads(self.last_request().content.decode()) if self.last_request().content else {}
        )

    @property
    def last_auth(self) -> str | None:
        return self.last_request().headers.get("authorization")

    # -- fake server ---------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if path == "/health":
            if self.health_effect is not None:
                raise self.health_effect
            if self.health_body is not None:
                return httpx.Response(self.health_status, content=self.health_body)
            if self.health_status != 200:
                return httpx.Response(self.health_status, content=b"health error")
            payload = self.health_payload or {
                "ok": True,
                "running": True,
                "engine": "playwright",
                "version": "1.2.3",
                "browserConnected": False,
                "poolSize": 2,
                "activeUserCount": 1,
            }
            return self._json(payload)

        # Auth gating (mirrors the official fork): general routes require the
        # access key (or the api key when the server has no access key);
        # sensitive routes (cookie import) require the api key (or the access
        # key when the server has none).  The health endpoint is ungated.
        if self.access_key is not None or self.api_key is not None:
            is_sensitive = path.startswith("/sessions/") and path.endswith("/cookies")
            if is_sensitive:
                required = self.api_key if self.api_key is not None else self.access_key
            else:
                required = self.access_key if self.access_key is not None else self.api_key
            auth = request.headers.get("authorization", "")
            token = auth[len("Bearer ") :] if auth.startswith("Bearer ") else None
            if required is not None and token != required:
                return self._json(
                    {"error": "Forbidden" if is_sensitive else "Unauthorized"},
                    status=403 if is_sensitive else 401,
                )

        if self.effect is not None:
            raise self.effect
        if self.route is None or path.endswith(self.route):
            if self.body is not None:
                return httpx.Response(self.status, content=self.body)
            if self.status != 200:
                return self._json({"error": self.error or "upstream error"}, status=self.status)
            if self.payload is not None:
                return self._json(self.payload)
        return self._default_response(request, path)

    def _next_tab_id(self) -> str:
        """Unique tab ids per creation — like the real server."""
        self._tab_seq += 1
        new_id = self.tab_id if self.tab_id is not None else f"tab-{self._tab_seq}"
        if new_id not in self.created_tab_ids:
            self.created_tab_ids.append(new_id)
        return new_id

    @property
    def created_tab_ids(self) -> list[str]:
        return self._created_tab_ids

    def _default_response(self, request: httpx.Request, path: str) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        if path == "/tabs" and request.method == "POST":
            new_id = self._next_tab_id()
            return self._json(
                {"tabId": new_id, "url": body.get("url") or "about:blank", "title": "New Tab"}
            )
        if path == "/tabs" and request.method == "GET":
            created = self.created_tab_ids or ([self.tab_id] if self.tab_id else ["tab-1"])
            return self._json(
                {
                    "tabs": [
                        {
                            "targetId": "T999",
                            "tabId": tab_id,
                            "url": "https://example.com",
                            "title": "Example",
                            "listItemId": "sk-1",
                        }
                        for tab_id in created
                    ]
                }
            )
        if path.endswith("/navigate"):
            return self._json(
                {
                    "ok": True,
                    "url": body.get("url")
                    or "https://www.google.com/search?q=" + (body.get("query") or ""),
                    "title": "Search",
                }
            )
        if path.endswith("/snapshot"):
            return self._json(
                {
                    "url": "https://example.com",
                    "snapshot": '- link "Home" [ref=e1]',
                    "refsCount": 1,
                    "offset": 0,
                    "truncated": False,
                    "totalChars": 23,
                    "hasMore": False,
                }
            )
        if path.endswith("/links"):
            limit = request.url.params.get("limit", "50")
            offset = request.url.params.get("offset", "0")
            return self._json(
                {
                    "links": [
                        {"url": "https://a.example", "text": "A"},
                        {"url": "https://b.example", "text": "B"},
                    ],
                    "pagination": {
                        "total": 2,
                        "offset": int(offset),
                        "limit": int(limit),
                        "hasMore": False,
                    },
                }
            )
        if path.endswith("/click"):
            return self._json({"ok": True, "url": "https://example.com/after-click"})
        if path.endswith("/type"):
            return self._json({"ok": True})
        if path.endswith("/evaluate"):
            return self._json({"ok": True, "result": 42, "resultType": "number"})
        if path.startswith("/sessions/") and path.endswith("/cookies"):
            return self._json(
                {"ok": True, "userId": body.get("userId"), "count": len(body.get("cookies", []))}
            )
        if path.startswith("/sessions/"):
            return self._json({"ok": True})
        if path.startswith("/tabs/"):
            return self._json({"ok": True, "url": "https://example.com"})
        return self._json({"ok": True})

    @staticmethod
    def _json(payload: dict, status: int = 200) -> httpx.Response:
        return httpx.Response(
            status,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )


def _session(client: CamofoxClient, name: str = "test") -> CamofoxSession:
    return client.create_session(name)


def _cookie() -> CamofoxCookie:
    return CamofoxCookie(
        name="session", value="cookie-value-secret", domain="example.com", http_only=True
    )


# --------------------------------------------------------------------- auth


class TestAuthHeaders:
    @pytest.mark.asyncio
    async def test_general_route_uses_access_key(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        await client.create_tab(session)
        assert mock.last_auth == f"Bearer {ACCESS_KEY}"

    @pytest.mark.asyncio
    async def test_sensitive_route_uses_api_key(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        await client.import_cookies(session, [_cookie()])
        assert mock.last_auth == f"Bearer {API_KEY}"

    @pytest.mark.asyncio
    async def test_only_api_key_set_falls_back_for_general(self):
        mock = MockCamofox()
        client = CamofoxClient(_config(access_key=None), client=mock.client())
        session = _session(client)
        await client.create_tab(session)
        assert mock.last_auth == f"Bearer {API_KEY}"

    @pytest.mark.asyncio
    async def test_only_access_key_set_falls_back_for_sensitive(self):
        mock = MockCamofox()
        client = CamofoxClient(_config(api_key=None), client=mock.client())
        session = _session(client)
        await client.import_cookies(session, [_cookie()])
        assert mock.last_auth == f"Bearer {ACCESS_KEY}"

    @pytest.mark.asyncio
    async def test_no_keys_no_header(self):
        mock = MockCamofox()
        client = CamofoxClient(_config(access_key=None, api_key=None), client=mock.client())
        session = _session(client)
        await client.create_tab(session)
        assert mock.last_auth is None

    @pytest.mark.asyncio
    async def test_health_sends_no_credentials(self):
        mock = MockCamofox()
        client = mock.make_client()
        await client.probe_health()
        assert mock.last_auth is None

    @pytest.mark.asyncio
    async def test_rejected_credentials_map_to_auth_error(self):
        # Server has an access key (general) and its own api key (sensitive);
        # the client presents the right access key but a *wrong* api key, so
        # the sensitive cookie route must reject it.
        mock = MockCamofox(access_key=ACCESS_KEY, api_key="server-api-key")
        client = CamofoxClient(
            _config(access_key=ACCESS_KEY, api_key="wrong-api"), client=mock.client()
        )
        session = _session(client)
        with pytest.raises(CamofoxAuthError) as exc_info:
            await client.import_cookies(session, [_cookie()])
        assert exc_info.value.status_code == 502
        assert exc_info.value.details.get("status_code") == 403

    @pytest.mark.asyncio
    async def test_general_route_rejects_wrong_key(self):
        mock = MockCamofox(access_key="server-access-key")
        client = CamofoxClient(
            _config(access_key="wrong-access", api_key=None), client=mock.client()
        )
        session = _session(client)
        with pytest.raises(CamofoxAuthError) as exc_info:
            await client.create_tab(session)
        assert exc_info.value.status_code == 502
        assert exc_info.value.details.get("status_code") == 401


# ------------------------------------------------------------------- errors


class TestErrorMapping:
    @pytest.mark.asyncio
    async def test_connection_failure_maps_to_unavailable(self):
        mock = MockCamofox(effect=httpx.ConnectError("connection refused"))
        client = mock.make_client()
        session = _session(client)
        with pytest.raises(CamofoxUnavailableError):
            await client.create_tab(session)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc_class", "status", "mapped_status"),
        [
            (CamofoxAuthError, 401, 502),
            (CamofoxAuthError, 403, 502),
            (CamofoxTimeoutError, 408, 504),
            (CamofoxUnavailableError, 500, 503),
            (CamofoxError, 400, 502),
            (CamofoxError, 422, 502),
            (CamofoxError, 429, 502),
        ],
    )
    async def test_status_codes(self, exc_class, status, mapped_status):
        mock = MockCamofox(status=status, error="some upstream problem")
        client = mock.make_client()
        session = _session(client)
        with pytest.raises(exc_class) as exc_info:
            await client.create_tab(session)
        # Mapped status is fixed per exception family; the upstream status
        # is preserved in details for debugging.
        assert exc_info.value.status_code == mapped_status
        assert exc_info.value.details.get("status_code") == status

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc_class", "status", "error"),
        [
            (CamofoxTabNotFoundError, 404, "Tab not found"),
            (CamofoxSessionExpiredError, 409, "No active session for userId"),
            (CamofoxSessionExpiredError, 409, "No canonical profile for userId"),
            (CamofoxTimeoutError, 500, "Navigation timed out after 30000ms"),
            (CamofoxUnavailableError, 500, "Internal server error"),
            (CamofoxError, 409, "Profile already in use"),
        ],
    )
    async def test_upstream_error_markers(self, exc_class, status, error):
        mock = MockCamofox(status=status, error=error)
        client = mock.make_client()
        session = _session(client)
        with pytest.raises(exc_class):
            await client.create_tab(session)

    @pytest.mark.asyncio
    async def test_404_without_marker_is_generic(self):
        mock = MockCamofox(status=404, error="Not Found")
        client = mock.make_client()
        session = _session(client)
        with pytest.raises(CamofoxError) as exc_info:
            await client.create_tab(session)
        assert not isinstance(exc_info.value, CamofoxTabNotFoundError)

    @pytest.mark.asyncio
    async def test_transport_read_timeout(self):
        mock = MockCamofox(effect=httpx.ReadTimeout("slow"))
        client = mock.make_client()
        session = _session(client)
        with pytest.raises(CamofoxTimeoutError):
            await client.create_tab(session)

    @pytest.mark.asyncio
    async def test_invalid_json(self):
        mock = MockCamofox(status=200, body=b"this is not json")
        client = mock.make_client()
        session = _session(client)
        with pytest.raises(CamofoxInvalidResponseError):
            await client.create_tab(session)

    @pytest.mark.asyncio
    async def test_non_object_json(self):
        mock = MockCamofox(status=200, body=b"[1, 2, 3]")
        client = mock.make_client()
        session = _session(client)
        with pytest.raises(CamofoxInvalidResponseError):
            await client.create_tab(session)

    @pytest.mark.asyncio
    async def test_navigation_failure_maps_to_navigation_error(self):
        # The 5xx only applies to the navigate route — tab creation succeeds.
        mock = MockCamofox(
            route="/navigate", status=500, error="Blocked request to private IP address"
        )
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        with pytest.raises(CamofoxNavigationError):
            await client.navigate(session, tab, url="http://169.254.169.254")

    @pytest.mark.asyncio
    async def test_navigation_404_stays_specific(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        mock.status = 404
        mock.error = "Tab not found"
        with pytest.raises(CamofoxTabNotFoundError):
            await client.navigate(session, tab, url="https://x.example")

    @pytest.mark.asyncio
    async def test_secrets_never_reach_errors(self):
        scenarios = [
            ("401", MockCamofox(status=401, error="Invalid API key"), CamofoxAuthError),
            (
                "500",
                MockCamofox(status=500, error="Browser failed: password is hunter2"),
                CamofoxUnavailableError,
            ),
            ("timeout", MockCamofox(effect=httpx.ReadTimeout("slow")), CamofoxTimeoutError),
            ("badjson", MockCamofox(status=200, body=b"oops"), CamofoxInvalidResponseError),
        ]
        for label, mock, exc_class in scenarios:
            client = mock.make_client()
            session = _session(client)
            with pytest.raises(CamofoxError) as exc_info:
                await client.create_tab(session)
            assert isinstance(exc_info.value, exc_class)
            rendered = json.dumps(exc_info.value.to_dict())
            assert ACCESS_KEY not in rendered
            assert API_KEY not in rendered
            assert "hunter2" not in rendered
            assert BASE_URL not in rendered


# ------------------------------------------------------------------- health


class TestHealthProbe:
    @pytest.mark.asyncio
    async def test_healthy(self):
        mock = MockCamofox()
        client = mock.make_client()
        status = await client.probe_health()
        assert status.available is True
        assert status.reason is None
        assert status.version == "1.2.3"
        assert status.browser_connected is False
        assert status.pool_size == 2
        assert status.active_user_count == 1

    @pytest.mark.asyncio
    async def test_server_says_not_running(self):
        mock = MockCamofox(health_payload={"ok": False, "running": False})
        client = mock.make_client()
        status = await client.probe_health()
        assert status.available is False
        assert status.reason is not None

    @pytest.mark.asyncio
    async def test_server_error_is_not_raised(self):
        mock = MockCamofox(health_status=500)
        client = mock.make_client()
        status = await client.probe_health()
        assert status.available is False
        assert status.reason is not None

    @pytest.mark.asyncio
    async def test_connection_refused_is_not_raised(self):
        mock = MockCamofox(health_effect=httpx.ConnectError("refused"))
        client = mock.make_client()
        status = await client.probe_health()
        assert status.available is False
        assert status.reason is not None

    @pytest.mark.asyncio
    async def test_malformed_health_payload_is_not_raised(self):
        mock = MockCamofox(health_body=b"not json")
        client = mock.make_client()
        status = await client.probe_health()
        assert status.available is False
        assert status.reason is not None


# -------------------------------------------------------------------- tabs


class TestTabOperations:
    @pytest.mark.asyncio
    async def test_create_tab_sends_identity_and_registers(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client, name="alice")
        tab = await client.create_tab(session, url="https://example.com")
        body = mock.last_json()
        assert body["userId"] == session.user_id
        assert body["sessionKey"] == session.session_key
        assert body["url"] == "https://example.com"
        assert tab.tab_id == "tab-1"
        assert tab.url == "https://example.com"
        assert tab in session.tabs

    @pytest.mark.asyncio
    async def test_create_tab_without_tab_id_is_invalid_response(self):
        mock = MockCamofox(payload={"url": "about:blank"})
        client = mock.make_client()
        session = _session(client)
        with pytest.raises(CamofoxInvalidResponseError):
            await client.create_tab(session)

    @pytest.mark.asyncio
    async def test_list_tabs_upserts_registry(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tabs = await client.list_tabs(session)
        assert len(tabs) == 1
        assert tabs[0].tab_id == "tab-1"
        assert tabs[0].session_key == "sk-1"
        # A listed tab can be operated on afterwards.
        snap = await client.snapshot(session, session.tabs[0])
        assert snap.snapshot

    @pytest.mark.asyncio
    async def test_navigate_url(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        result = await client.navigate(session, tab, url="https://example.com/x")
        body = mock.last_json()
        assert body["url"] == "https://example.com/x"
        assert body["userId"] == session.user_id
        assert result.url == "https://example.com/x"
        assert tab.url == "https://example.com/x"

    @pytest.mark.asyncio
    async def test_navigate_engine_shorthand_becomes_macro(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        await client.navigate(session, tab, engine="google", query="dolors")
        body = mock.last_json()
        assert body["macro"] == "@google_search"
        assert body["query"] == "dolors"
        assert "url" not in body

    @pytest.mark.asyncio
    async def test_navigate_requires_target(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        with pytest.raises(ValidationError):
            await client.navigate(session, tab)

    @pytest.mark.asyncio
    async def test_snapshot_windowed_fields(self):
        mock = MockCamofox(
            route="/snapshot",
            payload={
                "url": "https://example.com",
                "snapshot": "partial",
                "refsCount": 7,
                "offset": 100,
                "truncated": True,
                "totalChars": 100,
                "hasMore": True,
                "nextOffset": 200,
            },
        )
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        result = await client.snapshot(session, tab, offset=100)
        assert result.truncated is True
        assert result.has_more is True
        assert result.next_offset == 200
        assert result.refs_count == 7
        assert mock.last_request().url.params["offset"] == "100"

    @pytest.mark.asyncio
    async def test_snapshot_missing_text_is_invalid(self):
        mock = MockCamofox(
            route="/snapshot", payload={"url": "https://example.com", "refsCount": 0}
        )
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        with pytest.raises(CamofoxInvalidResponseError):
            await client.snapshot(session, tab)

    @pytest.mark.asyncio
    async def test_get_links_object_and_pagination(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        result = await client.get_links(session, tab, limit=10, offset=5)
        assert [link.url for link in result.links] == ["https://a.example", "https://b.example"]
        assert result.total == 2
        assert result.limit == 10
        assert mock.last_request().url.params["limit"] == "10"

    @pytest.mark.asyncio
    async def test_get_links_bare_list(self):
        mock = MockCamofox(
            route="/links", payload=[{"url": "https://a.example"}, "https://bare.example"]
        )
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        result = await client.get_links(session, tab)
        assert len(result.links) == 2
        assert result.total == 2

    @pytest.mark.asyncio
    async def test_get_links_missing_array_is_invalid(self):
        mock = MockCamofox(route="/links", payload={"wrong": True})
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        with pytest.raises(CamofoxInvalidResponseError):
            await client.get_links(session, tab)

    @pytest.mark.asyncio
    async def test_click_by_ref_and_selector(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        result = await client.click(session, tab, ref="e1")
        assert mock.last_json()["ref"] == "e1"
        assert result.ok is True
        assert tab.url == "https://example.com/after-click"
        await client.click(session, tab, selector="a.btn")
        assert mock.last_json()["selector"] == "a.btn"

    @pytest.mark.asyncio
    async def test_click_requires_target(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        with pytest.raises(ValidationError):
            await client.click(session, tab)

    @pytest.mark.asyncio
    async def test_type_text(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        result = await client.type_text(session, tab, "hello", selector="#q")
        body = mock.last_json()
        assert body["text"] == "hello"
        assert body["selector"] == "#q"
        assert result.ok is True

    @pytest.mark.asyncio
    async def test_evaluate_success(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        result = await client.evaluate(session, tab, "1 + 1", timeout_ms=1000)
        assert mock.last_json()["expression"] == "1 + 1"
        assert mock.last_json()["timeout"] == 1000
        assert result.ok is True
        assert result.result == 42

    @pytest.mark.asyncio
    async def test_evaluate_handled_failure_returned(self):
        mock = MockCamofox(
            route="/evaluate",
            payload={
                "ok": False,
                "error": "ReferenceError: x is not defined",
                "errorType": "js_error",
            },
        )
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        result = await client.evaluate(session, tab, "x")
        assert result.ok is False
        assert result.error_type == "js_error"

    @pytest.mark.asyncio
    async def test_evaluate_timeout_raised(self):
        mock = MockCamofox(
            route="/evaluate",
            payload={"ok": False, "error": "Evaluation timed out", "errorType": "timeout"},
        )
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        with pytest.raises(CamofoxTimeoutError):
            await client.evaluate(session, tab, "await new Promise(() => {})")

    @pytest.mark.asyncio
    async def test_close_tab_idempotent_and_forgets(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        await client.close_tab(session, tab)
        assert mock.last_request().method == "DELETE"
        assert session.tabs == ()
        mock.status = 404
        mock.error = "Tab not found"
        await client.close_tab(session, tab)  # still fine

    @pytest.mark.asyncio
    async def test_tab_not_found_invalidates_registry(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        mock.status = 404
        mock.error = "Tab not found"
        with pytest.raises(CamofoxTabNotFoundError):
            await client.snapshot(session, tab)
        assert session.tabs == ()  # forgotten after the server reported it gone


# ----------------------------------------------------------------- cookies


class TestCookieImport:
    @pytest.mark.asyncio
    async def test_cookie_wire_shape(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        cookie = CamofoxCookie(
            name="s",
            value="v",
            domain="example.com",
            path="/",
            http_only=True,
            secure=True,
            same_site="Lax",
        )
        count = await client.import_cookies(session, [cookie])
        body = mock.last_json()
        assert count == 1
        assert body["cookies"][0] == {
            "name": "s",
            "value": "v",
            "domain": "example.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
        assert mock.last_auth == f"Bearer {API_KEY}"

    @pytest.mark.asyncio
    async def test_empty_list_short_circuits(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        assert await client.import_cookies(session, []) == 0
        assert mock.requests == []

    @pytest.mark.asyncio
    async def test_cookie_scoped_to_tab(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = _session(client)
        tab = await client.create_tab(session)
        await client.import_cookies(session, [_cookie()], tab=tab)
        assert mock.last_json()["tabId"] == "tab-1"

    @pytest.mark.asyncio
    async def test_cookie_value_never_reaches_errors(self):
        mock = MockCamofox(status=403, error="Forbidden")
        client = mock.make_client()
        session = _session(client)
        with pytest.raises(CamofoxAuthError) as exc_info:
            await client.import_cookies(session, [_cookie()])
        assert "cookie-value-secret" not in json.dumps(exc_info.value.to_dict())


# --------------------------------------------------------------- session api


class TestSessionApi:
    @pytest.mark.asyncio
    async def test_session_close_tears_down_upstream(self):
        mock = MockCamofox()
        client = mock.make_client()
        session = client.create_session("worker")
        await session.create_tab()
        assert session.tabs != ()
        await session.close()
        assert mock.last_request().method == "DELETE"
        assert mock.last_request().url.path == f"/sessions/{session.user_id}"
        assert session.tabs == ()

    @pytest.mark.asyncio
    async def test_session_isolation(self):
        mock = MockCamofox()
        client = mock.make_client()
        a = client.create_session("a")
        b = client.create_session("b")
        tab_a = await a.create_tab()
        tab_b = await b.create_tab()
        # Each session's requests carry its own userId.
        await a.snapshot(tab_a)
        await b.snapshot(tab_b)
        last_two = mock.requests[-2:]
        assert last_two[0].url.params["userId"] == a.user_id
        assert last_two[1].url.params["userId"] == b.user_id
        # Cross-session use is refused.
        with pytest.raises(ValidationError):
            await a.snapshot(b.tabs[0])

    @pytest.mark.asyncio
    async def test_concurrent_sessions_never_share_tabs(self):
        mock = MockCamofox()
        client = mock.make_client()
        sessions = [client.create_session(f"worker-{i}") for i in range(4)]

        async def work(session: CamofoxSession) -> None:
            tab = await session.create_tab()
            await session.navigate(tab, url="https://example.com")
            await session.snapshot(tab)

        await asyncio.gather(*(work(s) for s in sessions))
        user_ids = {session.user_id for session in sessions}
        assert len(user_ids) == 4
        for request in mock.requests:
            if request.url.path == "/tabs" and request.method == "POST":
                sent_user = json.loads(request.content.decode())["userId"]
                assert sent_user in user_ids


# ---------------------------------------------------------------- lifecycle


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_close_own_client(self):
        client = CamofoxClient(_config())
        assert client._client.is_closed is False
        await client.close()
        assert client._client.is_closed is True
        await client.close()  # idempotent

    @pytest.mark.asyncio
    async def test_injected_client_not_closed(self):
        mock = MockCamofox()
        injected = mock.client()
        client = CamofoxClient(_config(), client=injected)
        await client.close()
        assert injected.is_closed is False

    @pytest.mark.asyncio
    async def test_context_manager_closes(self):
        async with CamofoxClient(_config()) as client:
            assert client._client.is_closed is False
        assert client._client.is_closed is True

    def test_requires_base_url(self):
        with pytest.raises(ValueError, match="base_url"):
            CamofoxClient(CamofoxConfig(base_url=None))


# ----------------------------------------------------------------- user ids


class TestUserIdGeneration:
    def test_named_sessions_get_slug(self):
        client = CamofoxClient(_config())
        user_id = client.make_user_id("Melissa's Fan Page!")
        assert user_id.startswith("scrapping-service-")
        assert re.match(r"^scrapping-service-[a-zA-Z0-9-]+$", user_id)

    def test_anonymous_sessions_are_unique(self):
        client = CamofoxClient(_config())
        ids = {client.make_user_id() for _ in range(50)}
        assert len(ids) == 50

    def test_custom_prefix(self):
        client = CamofoxClient(_config(user_id_prefix="scraper-api"))
        assert client.make_user_id("x").startswith("scraper-api-")
