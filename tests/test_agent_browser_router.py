"""End-to-end tests for the ``/v1/browser/...`` API (offline).

A scripted fake :class:`AgentBrowserProvider` (from
:mod:`tests.test_agent_browser_service`) is injected into
``app.state.agent_browser_service`` so no Camofox is needed.  These cover the
HTTP-layer acceptance criteria: auth required, the 503/403/409/404/410 error
mapping, the session lifecycle, and that provider identifiers never leak.

Error envelopes are returned as ``{"detail": {"error": {...}}}`` (FastAPI
``HTTPException``), so assertions read ``resp.json()["detail"]["error"]``.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import app.auth.resolver as auth_resolver
from app.auth.models import AuthContext, AuthProfile
from app.browser import AgentBrowserService
from app.browser.config import AgentBrowserConfig
from app.core.config import Settings
from app.main import app
from tests.test_agent_browser_service import FakeProvider

VALID_API_KEY = "test-key-for-tests"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _install_service(c: TestClient, provider: FakeProvider, **overrides) -> None:
    settings = Settings(**overrides)
    app.state.agent_browser_service = AgentBrowserService(provider, settings)


def _auth():
    return {"Authorization": f"Bearer {VALID_API_KEY}"}


def _error(resp):
    return resp.json()["detail"]["error"]


def _new_session(client: TestClient) -> str:
    resp = client.post("/v1/browser/sessions", headers=_auth())
    assert resp.status_code == 201
    return resp.json()["session"]["session_id"]


def _new_tab(client: TestClient, sid: str, url: str = "https://example.com") -> str:
    resp = client.post(f"/v1/browser/sessions/{sid}/tabs", headers=_auth(), params={"url": url})
    assert resp.status_code == 201
    return resp.json()["tab"]["tab_id"]


# --------------------------------------------------------------------------- auth / availability


def test_auth_required(client):
    resp = client.post("/v1/browser/sessions")
    assert resp.status_code == 401


def test_invalid_key_403(client):
    resp = client.post("/v1/browser/sessions", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 403


def test_disabled_returns_503(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=False)
    resp = client.post("/v1/browser/sessions", headers=_auth())
    assert resp.status_code == 503
    assert _error(resp)["type"] == "browser_disabled"


def test_capabilities_reflects_profile_limits(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    resp = client.get("/v1/browser/capabilities", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["provider"] == "fake"
    assert body["evaluate_enabled"] is False
    assert body["max_sessions"] == 5


# --------------------------------------------------------------------------- lifecycle


def test_session_lifecycle_and_id_mapping(client):
    provider = FakeProvider()
    _install_service(client, provider, agent_browser_enabled=True)

    resp = client.post("/v1/browser/sessions", headers=_auth())
    assert resp.status_code == 201
    session = resp.json()["session"]
    sid = session["session_id"]
    assert sid.startswith("ses_")
    assert "prov_" not in resp.text

    tab_id = _new_tab(client, sid)
    assert tab_id.startswith("tab_")

    # GET session shows the tab (flat BrowserSession shape).
    resp = client.get(f"/v1/browser/sessions/{sid}", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    assert len(body["tabs"]) == 1

    # Close the tab, then the session.
    resp = client.delete(f"/v1/browser/sessions/{sid}/tabs/{tab_id}", headers=_auth())
    assert resp.status_code == 200
    resp = client.delete(f"/v1/browser/sessions/{sid}", headers=_auth())
    assert resp.status_code == 200
    assert provider.closed_sessions


def test_delete_unknown_session_404(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    resp = client.delete("/v1/browser/sessions/ses_missing", headers=_auth())
    assert resp.status_code == 404
    assert _error(resp)["type"] == "browser_session_error"


def test_session_id_never_leaks_provider_id(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    sid = _new_session(client)
    body = client.get(f"/v1/browser/sessions/{sid}", headers=_auth()).text
    assert "prov_" not in body


def test_ownership_isolation_across_profiles(client):
    class _StubResolver:
        def __init__(self):
            self._profiles = {}
            self._key_entries = []
            self._default_profile = "default"
            self._expose_profile = False

        def _add(self, name, key):
            p = AuthProfile(name=name, key=key)
            self._profiles[name] = p
            self._key_entries.append((key, p))

        def authenticate(self, token):
            for key, profile in self._key_entries:
                if token == key:
                    return AuthContext(profile_name=profile.name)
            return None

        def effective_settings_for(self, profile_name=None):
            return Settings()

    stub = _StubResolver()
    stub._add("reader", "key-reader")
    stub._add("writer", "key-writer")
    auth_resolver._RESOLVER = stub

    _install_service(client, FakeProvider(), agent_browser_enabled=True)

    a = client.post("/v1/browser/sessions", headers={"Authorization": "Bearer key-reader"})
    assert a.status_code == 201
    sid = a.json()["session"]["session_id"]
    # The writer profile cannot read the reader's session (same 404 type).
    b = client.get(f"/v1/browser/sessions/{sid}", headers={"Authorization": "Bearer key-writer"})
    assert b.status_code == 404
    assert _error(b)["type"] == "browser_session_error"


# --------------------------------------------------------------------------- security / policy


def test_create_tab_localhost_rejected_403(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    sid = _new_session(client)
    resp = client.post(
        f"/v1/browser/sessions/{sid}/tabs", headers=_auth(), params={"url": "http://127.0.0.1/"}
    )
    assert resp.status_code == 403
    assert "security" in _error(resp)["type"]


def test_create_tab_private_ip_rejected_403(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    sid = _new_session(client)
    resp = client.post(
        f"/v1/browser/sessions/{sid}/tabs", headers=_auth(), params={"url": "http://10.1.2.3/"}
    )
    assert resp.status_code == 403


def test_navigate_private_ip_rejected_403(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    sid = _new_session(client)
    tab = _new_tab(client, sid)
    resp = client.post(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/navigate",
        headers=_auth(),
        json={"url": "http://10.1.2.3/"},
    )
    assert resp.status_code == 403


def test_navigate_ok(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    sid = _new_session(client)
    tab = _new_tab(client, sid)
    resp = client.post(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/navigate",
        headers=_auth(),
        json={"url": "https://example.com/page"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["url"] == "https://example.com/page"
    assert "prov_" not in resp.text


def test_click_requires_ref_or_selector_400(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    sid = _new_session(client)
    tab = _new_tab(client, sid)
    resp = client.post(f"/v1/browser/sessions/{sid}/tabs/{tab}/click", headers=_auth(), json={})
    assert resp.status_code == 400


def test_click_with_ref_ok(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    sid = _new_session(client)
    tab = _new_tab(client, sid)
    resp = client.post(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/click", headers=_auth(), json={"ref": "ref1"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_type_ok(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    sid = _new_session(client)
    tab = _new_tab(client, sid)
    resp = client.post(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/type", headers=_auth(), json={"text": "hello"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_snapshot_ok(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    sid = _new_session(client)
    tab = _new_tab(client, sid)
    resp = client.get(f"/v1/browser/sessions/{sid}/tabs/{tab}/snapshot", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    assert body["tab_id"] == tab
    assert body["refs_count"] == 1
    assert "prov_" not in resp.text


def test_snapshot_with_links(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    sid = _new_session(client)
    tab = _new_tab(client, sid)
    resp = client.get(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/snapshot",
        headers=_auth(),
        params={"include_links": "true"},
    )
    assert resp.status_code == 200
    links = resp.json()["links"]
    assert links[0]["url"] == "https://example.com/a"


def test_evaluate_disabled_403(client):
    _install_service(
        client, FakeProvider(), agent_browser_enabled=True, agent_browser_allow_evaluate=False
    )
    sid = _new_session(client)
    tab = _new_tab(client, sid)
    resp = client.post(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/evaluate",
        headers=_auth(),
        json={"expression": "1+1"},
    )
    assert resp.status_code == 403
    assert _error(resp)["type"] == "browser_evaluate_disabled"


def test_evaluate_enabled_200(client):
    _install_service(
        client, FakeProvider(), agent_browser_enabled=True, agent_browser_allow_evaluate=True
    )
    sid = _new_session(client)
    tab = _new_tab(client, sid)
    resp = client.post(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/evaluate",
        headers=_auth(),
        json={"expression": "1+1"},
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == 42


# --------------------------------------------------------------------------- limits / provider


def test_session_limit_409(client):
    _install_service(
        client, FakeProvider(), agent_browser_enabled=True, agent_browser_max_sessions=1
    )
    a = client.post("/v1/browser/sessions", headers=_auth())
    assert a.status_code == 201
    b = client.post("/v1/browser/sessions", headers=_auth())
    assert b.status_code == 409
    assert _error(b)["type"] == "browser_session_limit"


def test_tab_limit_409(client):
    _install_service(
        client,
        FakeProvider(),
        agent_browser_enabled=True,
        agent_browser_max_tabs_per_session=1,
    )
    sid = _new_session(client)
    t1 = client.post(
        f"/v1/browser/sessions/{sid}/tabs", headers=_auth(), params={"url": "https://example.com"}
    )
    assert t1.status_code == 201
    t2 = client.post(
        f"/v1/browser/sessions/{sid}/tabs", headers=_auth(), params={"url": "https://example.com/2"}
    )
    assert t2.status_code == 409
    assert _error(t2)["type"] == "browser_tab_limit"


def test_invalid_tab_404(client):
    _install_service(client, FakeProvider(), agent_browser_enabled=True)
    sid = _new_session(client)
    resp = client.get(f"/v1/browser/sessions/{sid}/tabs/tab_missing/snapshot", headers=_auth())
    assert resp.status_code == 404
    assert _error(resp)["type"] == "browser_tab_error"


def test_provider_failure_502(client):
    provider = FakeProvider(fail_on="navigate")
    _install_service(client, provider, agent_browser_enabled=True)
    sid = _new_session(client)
    tab = _new_tab(client, sid)
    resp = client.post(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/navigate",
        headers=_auth(),
        json={"url": "https://example.com/x"},
    )
    assert resp.status_code == 502
    assert _error(resp)["type"] == "browser_provider_error"


def test_expired_session_410(client):
    provider = FakeProvider()
    # Build settings validly, then swap in a sub-minute TTL via model_copy
    # (bypassing the >= 60 constraint) so the sweep fires quickly in tests.
    settings = Settings(agent_browser_enabled=True)
    settings = settings.model_copy(update={"agent_browser_session_ttl_seconds": 1})
    svc = AgentBrowserService(provider, Settings(agent_browser_enabled=True))
    svc.settings = settings
    svc._base_config = AgentBrowserConfig.model_construct(
        enabled=True,
        allow_evaluate=False,
        max_sessions=5,
        max_tabs_per_session=3,
        session_ttl_seconds=1,
        cleanup_interval_seconds=60,
        operation_timeout_seconds=30,
    )
    app.state.agent_browser_service = svc

    resp = client.post("/v1/browser/sessions", headers=_auth())
    sid = resp.json()["session"]["session_id"]

    time.sleep(1.2)
    resp = client.get(f"/v1/browser/sessions/{sid}", headers=_auth())
    assert resp.status_code == 410
    assert _error(resp)["type"] == "browser_session_expired"
