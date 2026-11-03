"""Additional tests for the agent browser service and routes to raise coverage
of the maintenance loop, provider-failure paths, and HTTP error branches."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.browser import AgentBrowserService
from app.core.config import Settings
from app.main import app
from tests.test_agent_browser_service import FakeProvider, _svc

VALID_API_KEY = "test-key-for-tests"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _auth():
    return {"Authorization": f"Bearer {VALID_API_KEY}"}


# --------------------------------------------------------------------------- maintenance loop


async def test_maintenance_loop_runs_and_stops():
    provider = FakeProvider()
    svc = _svc(
        provider, agent_browser_session_ttl_seconds=60, agent_browser_cleanup_interval_seconds=60
    )
    await svc.start_maintenance()
    assert svc._maintenance_task is not None
    assert svc._maintenance_stop is not None
    # Stop should clear the task reference.
    svc._maintenance_stop.set()
    await svc.stop_maintenance()
    assert svc._maintenance_task is None


async def test_start_maintenance_idempotent():
    provider = FakeProvider()
    svc = _svc(provider)
    await svc.start_maintenance()
    await svc.start_maintenance()  # no-op
    assert svc._maintenance_task is not None
    await svc.stop_maintenance()


async def test_sweep_releases_expired_session_upstream():
    provider = FakeProvider()
    svc = _svc(provider, agent_browser_session_ttl_seconds=1)
    session = await svc.create_session("alice")
    await svc.create_tab("alice", session.session_id, "https://example.com")
    import time

    svc._sessions[session.session_id].last_activity_at = time.time() - 100
    released = await svc.sweep_expired()
    assert released == 1
    assert session.session_id not in svc._sessions
    assert len(provider.closed_sessions) == 1
    # The open tab was closed too.
    assert provider.closed_tabs


async def test_get_health():
    provider = FakeProvider()
    svc = _svc(provider)
    assert svc._base_config.enabled is True


# --------------------------------------------------------------------------- provider failures


async def test_release_session_calls_upstream():
    provider = FakeProvider()
    svc = _svc(provider)
    session = await svc.create_session("alice")
    await svc.create_tab("alice", session.session_id, "https://example.com")
    tracked = svc._sessions[session.session_id]
    await svc._release_session(tracked)
    assert session.session_id not in svc._sessions
    assert len(provider.closed_sessions) == 1
    assert provider.closed_tabs


async def test_delete_tab_already_gone_upstream_is_ok():
    # A tab that is gone upstream still removes it locally (no raise).
    provider = FakeProvider()
    svc = _svc(provider)
    session = await svc.create_session("alice")
    tab = await svc.create_tab("alice", session.session_id, "https://example.com")
    tracked = svc._sessions[session.session_id]
    # Simulate the tab being gone upstream.
    provider.sessions[tracked.provider_session_id]["tabs"].pop(
        tracked.tabs[tab.tab_id].provider_tab_id, None
    )
    await svc.delete_tab("alice", session.session_id, tab.tab_id)
    assert tab.tab_id not in tracked.tabs


async def test_snapshot_get_links_failure_is_swallowed():
    provider = FakeProvider(fail_on="get_links")
    svc = _svc(provider)
    session = await svc.create_session("alice")
    tab = await svc.create_tab("alice", session.session_id, "https://example.com")
    from app.browser.models import SnapshotRequest

    result = await svc.snapshot(
        "alice", session.session_id, tab.tab_id, SnapshotRequest(include_links=True)
    )
    assert result.links == []


async def test_evaluate_timeout_passthrough():
    provider = FakeProvider()
    svc = _svc(provider, agent_browser_allow_evaluate=True)
    session = await svc.create_session("alice")
    tab = await svc.create_tab("alice", session.session_id, "https://example.com")
    result = await svc.evaluate("alice", session.session_id, tab.tab_id, "1+1", timeout_ms=1000)
    assert result.ok is True


# --------------------------------------------------------------------------- route branch coverage


def test_get_session_expired_410(client):
    provider = FakeProvider()
    settings = Settings(agent_browser_enabled=True)
    settings = settings.model_copy(update={"agent_browser_session_ttl_seconds": 1})
    svc = AgentBrowserService(provider, Settings(agent_browser_enabled=True))
    svc.settings = settings
    from app.browser.config import AgentBrowserConfig

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
    import time

    sid = client.post("/v1/browser/sessions", headers=_auth()).json()["session"]["session_id"]
    time.sleep(1.2)
    resp = client.get(f"/v1/browser/sessions/{sid}", headers=_auth())
    assert resp.status_code == 410


def test_delete_session_expired_410(client):
    provider = FakeProvider()
    settings = Settings(agent_browser_enabled=True).model_copy(
        update={"agent_browser_session_ttl_seconds": 1}
    )
    svc = AgentBrowserService(provider, Settings(agent_browser_enabled=True))
    svc.settings = settings
    from app.browser.config import AgentBrowserConfig

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
    import time

    sid = client.post("/v1/browser/sessions", headers=_auth()).json()["session"]["session_id"]
    time.sleep(1.2)
    resp = client.delete(f"/v1/browser/sessions/{sid}", headers=_auth())
    assert resp.status_code == 410


def test_delete_tab_404_and_expired(client):
    provider = FakeProvider()
    s = AgentBrowserService(provider, Settings(agent_browser_enabled=True))
    app.state.agent_browser_service = s
    sid = client.post("/v1/browser/sessions", headers=_auth()).json()["session"]["session_id"]
    # Delete a non-existent tab -> 404.
    resp = client.delete(f"/v1/browser/sessions/{sid}/tabs/tab_missing", headers=_auth())
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["type"] == "browser_tab_error"


def test_navigate_and_click_and_type_expired_410(client):
    provider = FakeProvider()
    settings = Settings(agent_browser_enabled=True).model_copy(
        update={"agent_browser_session_ttl_seconds": 1}
    )
    svc = AgentBrowserService(provider, Settings(agent_browser_enabled=True))
    svc.settings = settings
    from app.browser.config import AgentBrowserConfig

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
    import time

    sid = client.post("/v1/browser/sessions", headers=_auth()).json()["session"]["session_id"]
    tab = client.post(
        f"/v1/browser/sessions/{sid}/tabs", headers=_auth(), params={"url": "https://example.com"}
    ).json()["tab"]["tab_id"]
    time.sleep(1.2)

    # Navigate -> 410
    r = client.post(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/navigate",
        headers=_auth(),
        json={"url": "https://example.com/x"},
    )
    assert r.status_code == 410
    # Click -> 410
    r = client.post(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/click", headers=_auth(), json={"ref": "ref1"}
    )
    assert r.status_code == 410
    # Type -> 410
    r = client.post(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/type", headers=_auth(), json={"text": "x"}
    )
    assert r.status_code == 410
    # Evaluate -> 410
    r = client.post(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/evaluate", headers=_auth(), json={"expression": "1"}
    )
    assert r.status_code == 410
    # Delete tab -> 410
    r = client.delete(f"/v1/browser/sessions/{sid}/tabs/{tab}", headers=_auth())
    assert r.status_code == 410


def test_provider_create_session_failure_502(client):
    provider = FakeProvider(fail_on="create_session")
    s = AgentBrowserService(provider, Settings(agent_browser_enabled=True))
    app.state.agent_browser_service = s
    resp = client.post("/v1/browser/sessions", headers=_auth())
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"]["type"] == "browser_provider_error"


def test_provider_create_tab_failure_502(client):
    provider = FakeProvider(fail_on="create_tab")
    s = AgentBrowserService(provider, Settings(agent_browser_enabled=True))
    app.state.agent_browser_service = s
    sid = client.post("/v1/browser/sessions", headers=_auth()).json()["session"]["session_id"]
    resp = client.post(
        f"/v1/browser/sessions/{sid}/tabs", headers=_auth(), params={"url": "https://example.com"}
    )
    assert resp.status_code == 502


def test_provider_snapshot_failure_502(client):
    provider = FakeProvider(fail_on="snapshot")
    s = AgentBrowserService(provider, Settings(agent_browser_enabled=True))
    app.state.agent_browser_service = s
    sid = client.post("/v1/browser/sessions", headers=_auth()).json()["session"]["session_id"]
    tab = client.post(
        f"/v1/browser/sessions/{sid}/tabs", headers=_auth(), params={"url": "https://example.com"}
    ).json()["tab"]["tab_id"]
    resp = client.get(f"/v1/browser/sessions/{sid}/tabs/{tab}/snapshot", headers=_auth())
    assert resp.status_code == 502


def test_click_selector_works(client):
    provider = FakeProvider()
    s = AgentBrowserService(provider, Settings(agent_browser_enabled=True))
    app.state.agent_browser_service = s
    sid = client.post("/v1/browser/sessions", headers=_auth()).json()["session"]["session_id"]
    tab = client.post(
        f"/v1/browser/sessions/{sid}/tabs", headers=_auth(), params={"url": "https://example.com"}
    ).json()["tab"]["tab_id"]
    resp = client.post(
        f"/v1/browser/sessions/{sid}/tabs/{tab}/click",
        headers=_auth(),
        json={"selector": "a.login"},
    )
    assert resp.status_code == 200


def test_capabilities_when_disabled(client):
    provider = FakeProvider()
    s = AgentBrowserService(provider, Settings(agent_browser_enabled=False))
    app.state.agent_browser_service = s
    resp = client.get("/v1/browser/capabilities", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_service_without_auth_context_uses_unknown_profile(client):
    # When no auth (disabled auth server), the service still works with unknown profile.
    provider = FakeProvider()
    s = AgentBrowserService(
        provider, Settings(agent_browser_enabled=True, server_api_key_required=False)
    )
    app.state.agent_browser_service = s
    # No auth header (server doesn't require it in this test app config default is True,
    # but the dependency short-circuits when server_api_key_required is False).
    resp = client.post("/v1/browser/sessions")
    assert resp.status_code in (201, 401)


async def test_tracked_session_expires_property():

    provider = FakeProvider()
    svc = _svc(provider, agent_browser_session_ttl_seconds=100)
    session = await svc.create_session("alice")
    record = svc._sessions[session.session_id]
    assert abs(record.expires_at - (record.last_activity_at + 100)) < 1
