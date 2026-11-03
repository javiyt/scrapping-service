"""Unit tests for the agent browser service (ownership, security, limits, TTL).

These use a scripted fake :class:`AgentBrowserProvider` — no Camofox, no
network.  They exercise the service policy directly (not the HTTP layer), so
each acceptance criterion maps to a focused test.
"""

from __future__ import annotations

import time

import pytest

from app.browser.config import AgentBrowserConfig
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
from app.browser.service import AgentBrowserService
from app.core.config import Settings
from app.core.errors import (
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

# --------------------------------------------------------------------------- fake


class FakeProvider:
    """A scripted in-memory agent browser provider.

    Records calls so tests can assert the ID mapping (provider ids are opaque
    ``prov_*`` strings distinct from service ids).
    """

    name = "fake"
    supports_evaluate = True

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on  # e.g. "create_session"/"navigate"/"snapshot"/...
        self._n = 0
        self.sessions: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.closed_sessions: list[str] = []
        self.closed_tabs: list[tuple[str, str]] = []

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"prov_{prefix}_{self._n}"

    def _fail(self, op: str) -> None:
        if self.fail_on == op:
            raise ProviderError(f"fake {op} failed", details={"op": op})

    async def create_session(self) -> ProviderSession:
        self._fail("create_session")
        pid = self._id("ses")
        self.sessions[pid] = {"tabs": {}}
        self.calls.append(("create_session", pid))
        return ProviderSession(provider_session_id=pid)

    async def close_session(self, provider_session_id: str) -> None:
        self._fail("close_session")
        self.closed_sessions.append(provider_session_id)
        self.sessions.pop(provider_session_id, None)

    def _sess(self, provider_session_id: str) -> dict:
        if provider_session_id not in self.sessions:
            raise ProviderError("session gone", details={})
        return self.sessions[provider_session_id]

    async def create_tab(self, provider_session_id: str, url: str | None = None) -> ProviderTab:
        self._fail("create_tab")
        tid = self._id("tab")
        self._sess(provider_session_id)["tabs"][tid] = {"url": url, "title": "t"}
        self.calls.append(("create_tab", provider_session_id, tid, url))
        return ProviderTab(provider_tab_id=tid, url=url, title="t")

    async def close_tab(self, provider_session_id: str, provider_tab_id: str) -> None:
        self._fail("close_tab")
        self.closed_tabs.append((provider_session_id, provider_tab_id))
        self._sess(provider_session_id)["tabs"].pop(provider_tab_id, None)

    async def navigate(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        url: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderNavigateResult:
        self._fail("navigate")
        entry = self._sess(provider_session_id)["tabs"].get(provider_tab_id)
        if entry is None:
            raise ProviderError("tab gone", details={})
        entry["url"] = url
        entry["title"] = f"title-{url}"
        return ProviderNavigateResult(ok=True, url=url, title=f"title-{url}")

    async def snapshot(
        self, provider_session_id: str, provider_tab_id: str, *, offset: int = 0
    ) -> ProviderSnapshot:
        self._fail("snapshot")
        entry = self._sess(provider_session_id)["tabs"].get(provider_tab_id)
        if entry is None:
            raise ProviderError("tab gone", details={})
        return ProviderSnapshot(
            url=entry.get("url"),
            title=entry.get("title"),
            snapshot="click @ref1 link\nstatic text",
            refs_count=1,
            truncated=False,
            has_more=False,
            next_offset=None,
        )

    async def get_links(
        self, provider_session_id: str, provider_tab_id: str, *, limit: int = 50
    ) -> ProviderLinks:
        self._fail("get_links")
        return ProviderLinks(
            links=[ProviderLink(url="https://example.com/a", text="a")],
            total=1,
            has_more=False,
        )

    async def click(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        *,
        ref: str | None = None,
        selector: str | None = None,
    ) -> ProviderActionResult:
        self._fail("click")
        if ref is None and selector is None:
            raise ProviderError("click requires a ref or a selector")
        return ProviderActionResult(ok=True, url="https://example.com/after")

    async def type_text(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        text: str,
        *,
        ref: str | None = None,
        selector: str | None = None,
    ) -> ProviderActionResult:
        self._fail("type")
        return ProviderActionResult(ok=True, url=None)

    async def evaluate(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        expression: str,
        *,
        timeout_ms: int | None = None,
    ) -> ProviderEvaluateResult:
        self._fail("evaluate")
        return ProviderEvaluateResult(ok=True, result=42, result_type="number")

    async def health(self) -> ProviderHealth:
        return ProviderHealth(available=True, reason=None, version="fake")


def _settings(**overrides) -> Settings:
    base = dict(
        agent_browser_enabled=True,
        agent_browser_allow_evaluate=False,
        agent_browser_max_sessions=2,
        agent_browser_max_tabs_per_session=2,
        agent_browser_session_ttl_seconds=3600,
        agent_browser_cleanup_interval_seconds=60,
    )
    base.update(overrides)
    return Settings(**base)


def _svc(provider: FakeProvider, **overrides) -> AgentBrowserService:
    """Build a service, bypassing pydantic validation for test-only policy values.

    The service is constructed with the (validated) default settings so its
    ``__init__`` succeeds, then ``settings`` / ``_base_config`` are swapped for
    the unvalidated test values.  ``model_copy`` bypasses pydantic re-validation,
    letting tests use a sub-minute TTL.
    """
    settings = _settings().model_copy(update=overrides)
    # Validate the *other* fields the config reads (limits) so construction works.
    svc = AgentBrowserService(provider, _settings())
    svc.settings = settings
    svc._base_config = AgentBrowserConfig.model_construct(
        enabled=svc.settings.agent_browser_enabled,
        allow_evaluate=svc.settings.agent_browser_allow_evaluate,
        max_sessions=svc.settings.agent_browser_max_sessions,
        max_tabs_per_session=svc.settings.agent_browser_max_tabs_per_session,
        session_ttl_seconds=svc.settings.agent_browser_session_ttl_seconds,
        cleanup_interval_seconds=svc.settings.agent_browser_cleanup_interval_seconds,
        operation_timeout_seconds=svc.settings.agent_browser_operation_timeout_seconds,
    )
    svc._base_security = dict(svc._base_security)
    return svc


# --------------------------------------------------------------------------- session


async def test_create_session_returns_service_owned_ids():
    provider = FakeProvider()
    svc = _svc(provider)
    session = await svc.create_session("alice")
    assert session.session_id.startswith("ses_")
    assert session.provider == "fake"
    assert session.tabs == []
    # A provider session exists behind the service id.
    assert len(provider.sessions) == 1
    assert provider.sessions[svc._sessions[session.session_id].provider_session_id]


async def test_create_session_limit_enforced():
    provider = FakeProvider()
    svc = _svc(provider, agent_browser_max_sessions=1)
    await svc.create_session("alice")
    with pytest.raises(BrowserSessionLimitError):
        await svc.create_session("alice")
    # Different profile is not affected by alice's session.
    session = await svc.create_session("bob")
    assert session.session_id


async def test_session_ownership_isolation():
    provider = FakeProvider()
    svc = _svc(provider)
    alice = await svc.create_session("alice")
    # Bob cannot see / touch alice's session (uniform 404 error type).
    with pytest.raises(BrowserSessionError):
        await svc.get_session("bob", alice.session_id)
    with pytest.raises(BrowserSessionError):
        await svc.delete_session("bob", alice.session_id)
    # Alice can.
    view = await svc.get_session("alice", alice.session_id)
    assert view.session_id == alice.session_id


async def test_unknown_session_raises_not_found():
    provider = FakeProvider()
    svc = _svc(provider)
    with pytest.raises(BrowserSessionError):
        await svc.get_session("alice", "ses_does_not_exist")


async def test_expired_session_raises_expired():
    provider = FakeProvider()
    svc = _svc(provider, agent_browser_session_ttl_seconds=1)
    session = await svc.create_session("alice")
    # Force expiry.
    svc._sessions[session.session_id].last_activity_at = time.time() - 10
    with pytest.raises(BrowserSessionExpiredError):
        await svc.get_session("alice", session.session_id)


async def test_sweep_expired_releases_upstream():
    provider = FakeProvider()
    svc = _svc(provider, agent_browser_session_ttl_seconds=1)
    session = await svc.create_session("alice")
    await svc.create_tab("alice", session.session_id, "https://example.com")
    # Expire it.
    svc._sessions[session.session_id].last_activity_at = time.time() - 100
    released = await svc.sweep_expired()
    assert released == 1
    # Upstream released exactly one session, and the local record is gone.
    assert session.session_id not in svc._sessions
    assert len(provider.closed_sessions) == 1
    assert provider.closed_tabs  # the open tab was closed too


async def test_delete_session_closes_upstream():
    provider = FakeProvider()
    svc = _svc(provider)
    session = await svc.create_session("alice")
    await svc.create_tab("alice", session.session_id, "https://example.com")
    await svc.delete_session("alice", session.session_id)
    assert session.session_id not in svc._sessions
    assert len(provider.closed_sessions) == 1
    assert provider.closed_tabs  # the tab was closed too


# --------------------------------------------------------------------------- tabs


async def test_create_tab_with_url_and_id_mapping():
    provider = FakeProvider()
    svc = _svc(provider)
    session = await svc.create_session("alice")
    tab = await svc.create_tab("alice", session.session_id, "https://example.com")
    assert tab.tab_id.startswith("tab_")
    assert tab.url == "https://example.com"
    # Service id maps to a distinct provider id.
    internal = svc._get_session("alice", session.session_id)
    tracked = internal.tabs[tab.tab_id]
    assert tracked.provider_tab_id.startswith("prov_")
    assert tracked.provider_tab_id != tab.tab_id


async def test_create_tab_rejects_localhost_url():
    provider = FakeProvider()
    svc = _svc(provider)
    session = await svc.create_session("alice")
    with pytest.raises(SecurityError):
        await svc.create_tab("alice", session.session_id, "http://127.0.0.1/")


async def test_create_tab_rejects_private_ip():
    provider = FakeProvider()
    svc = _svc(provider)
    session = await svc.create_session("alice")
    with pytest.raises(SecurityError):
        await svc.create_tab("alice", session.session_id, "http://10.0.0.5/")


async def test_create_tab_respects_domain_allowlist():
    provider = FakeProvider()
    svc = _svc(provider)
    svc._base_security["allowed_domains"] = ["example.com"]
    session = await svc.create_session("alice")
    # example.com is allowed.
    tab = await svc.create_tab("alice", session.session_id, "https://example.com/page")
    assert tab.url == "https://example.com/page"
    # A different domain is rejected.
    with pytest.raises(SecurityError):
        await svc.create_tab("alice", session.session_id, "https://other.com/")


async def test_tab_limit_enforced():
    provider = FakeProvider()
    svc = _svc(provider, agent_browser_max_tabs_per_session=1)
    session = await svc.create_session("alice")
    await svc.create_tab("alice", session.session_id, "https://example.com")
    with pytest.raises(BrowserTabLimitError):
        await svc.create_tab("alice", session.session_id, "https://example.com/2")


async def test_delete_tab_and_invalid_tab():
    provider = FakeProvider()
    svc = _svc(provider)
    session = await svc.create_session("alice")
    tab = await svc.create_tab("alice", session.session_id, "https://example.com")
    await svc.delete_tab("alice", session.session_id, tab.tab_id)
    internal = svc._get_session("alice", session.session_id)
    assert tab.tab_id not in internal.tabs
    with pytest.raises(BrowserTabError):
        svc._get_tab(internal, tab.tab_id)


# --------------------------------------------------------------------------- actions


async def test_snapshot_returns_normalized_shape():
    provider = FakeProvider()
    svc = _svc(provider)
    session = await svc.create_session("alice")
    tab = await svc.create_tab("alice", session.session_id, "https://example.com")
    from app.browser.models import SnapshotRequest

    result = await svc.snapshot(
        "alice", session.session_id, tab.tab_id, SnapshotRequest(include_links=True)
    )
    assert result.session_id == session.session_id
    assert result.tab_id == tab.tab_id
    assert result.provider.provider == "fake"
    assert result.refs_count == 1
    assert result.links[0].url == "https://example.com/a"
    assert "ref1" in result.snapshot


async def test_navigate_updates_tab_and_validates():
    provider = FakeProvider()
    svc = _svc(provider)
    session = await svc.create_session("alice")
    tab = await svc.create_tab("alice", session.session_id, "https://example.com")
    result = await svc.navigate("alice", session.session_id, tab.tab_id, "https://example.com/x")
    assert result.ok
    internal = svc._get_session("alice", session.session_id)
    assert internal.tabs[tab.tab_id].url == "https://example.com/x"
    with pytest.raises(SecurityError):
        await svc.navigate("alice", session.session_id, tab.tab_id, "http://localhost/")


async def test_click_requires_ref_or_selector():
    provider = FakeProvider()
    svc = _svc(provider)
    session = await svc.create_session("alice")
    tab = await svc.create_tab("alice", session.session_id, "https://example.com")
    with pytest.raises(ValidationError):
        await svc.click("alice", session.session_id, tab.tab_id)
    result = await svc.click("alice", session.session_id, tab.tab_id, ref="ref1")
    assert result.ok


async def test_type_text_works():
    provider = FakeProvider()
    svc = _svc(provider)
    session = await svc.create_session("alice")
    tab = await svc.create_tab("alice", session.session_id, "https://example.com")
    result = await svc.type_text("alice", session.session_id, tab.tab_id, "hello")
    assert result.ok


# --------------------------------------------------------------------------- evaluate


async def test_evaluate_disabled_by_default():
    provider = FakeProvider()
    svc = _svc(provider, agent_browser_allow_evaluate=False)
    session = await svc.create_session("alice")
    tab = await svc.create_tab("alice", session.session_id, "https://example.com")
    with pytest.raises(EvaluateDisabledError):
        await svc.evaluate("alice", session.session_id, tab.tab_id, "1+1")


async def test_evaluate_enabled_for_profile():
    provider = FakeProvider()
    svc = _svc(provider, agent_browser_allow_evaluate=True)
    session = await svc.create_session("alice")
    tab = await svc.create_tab("alice", session.session_id, "https://example.com")
    result = await svc.evaluate("alice", session.session_id, tab.tab_id, "1+1")
    assert result.ok
    assert result.result == 42


# --------------------------------------------------------------------------- provider failures


async def test_provider_failure_maps_to_public_error():
    provider = FakeProvider(fail_on="navigate")
    svc = _svc(provider)
    session = await svc.create_session("alice")
    tab = await svc.create_tab("alice", session.session_id, "https://example.com")
    with pytest.raises(BrowserProviderError) as exc_info:
        await svc.navigate("alice", session.session_id, tab.tab_id, "https://example.com/y")
    assert exc_info.value.status_code == 502
    assert exc_info.value.error_type == "browser_provider_error"


async def test_provider_create_tab_failure_is_public():
    provider = FakeProvider(fail_on="create_tab")
    svc = _svc(provider)
    session = await svc.create_session("alice")
    with pytest.raises(BrowserProviderError):
        await svc.create_tab("alice", session.session_id, "https://example.com")


# --------------------------------------------------------------------------- config / settings


def test_config_from_settings_and_profile_override():
    settings = _settings(agent_browser_max_sessions=3, agent_browser_allow_evaluate=True)
    config = AgentBrowserConfig.from_settings(settings)
    assert config.max_sessions == 3
    assert config.allow_evaluate is True
    assert config.enabled is True
