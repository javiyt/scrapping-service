"""Unit tests for the Camofox agent browser provider (no network, no browser).

A minimal fake ``CamofoxClient``/``CamofoxSession`` stand in for the real
HTTP-backed ones, so the provider's mapping logic, error translation, and
stale-session handling are exercised without any external service.
"""

from __future__ import annotations

import pytest

from app.browser.providers.base import ProviderError
from app.browser.providers.camofox import CamofoxAgentBrowserProvider, build_camofox_provider
from app.camofox.types import (
    CamofoxTab,
)
from app.core.errors import (
    CamofoxError,
    CamofoxSessionExpiredError,
    CamofoxTabNotFoundError,
)


class _Session:
    """Just enough of :class:`CamofoxSession` for the provider to use."""

    def __init__(self, key: str, user: str) -> None:
        self.session_key = key
        self.user_id = user
        self._tabs: list[CamofoxTab] = []
        self.closed = False

    @property
    def tabs(self) -> tuple[CamofoxTab, ...]:
        return tuple(self._tabs)

    async def create_tab(self, url: str | None = None) -> CamofoxTab:
        tab = CamofoxTab(tab_id=f"tab_{len(self._tabs)}", url=url, title="T")
        self._tabs.append(tab)
        return tab

    async def navigate(self, tab, url, **kwargs):
        from app.camofox.types import CamofoxNavigateResult

        tab.url = url
        return CamofoxNavigateResult(ok=True, url=url, title="T")

    async def snapshot(self, tab, offset=0):
        from app.camofox.types import CamofoxSnapshot

        return CamofoxSnapshot(url=tab.url, snapshot="tree", refs_count=3, truncated=False)

    async def get_links(self, tab, *, limit=50, offset=0):
        from app.camofox.types import CamofoxLink, CamofoxLinks

        return CamofoxLinks(links=[CamofoxLink(url="https://a", text="a")])

    async def click(self, tab, *, ref=None, selector=None):
        from app.camofox.types import CamofoxActionResult

        return CamofoxActionResult(ok=True, url=tab.url)

    async def type_text(self, tab, text, *, ref=None, selector=None):
        from app.camofox.types import CamofoxActionResult

        return CamofoxActionResult(ok=True, url=tab.url)

    async def evaluate(self, tab, expression, *, timeout_ms=None):
        from app.camofox.types import CamofoxEvaluateResult

        return CamofoxEvaluateResult(ok=True, result="r", result_type="str", truncated=False)

    async def close_tab(self, tab) -> None:
        self._tabs = [t for t in self._tabs if t is not tab]

    async def close(self) -> None:
        self.closed = True

    def forget_all_tabs(self) -> None:
        self._tabs = []


class _Client:
    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self._n = 0

    def create_session(self) -> _Session:
        self._n += 1
        return _Session(f"key_{self._n}", f"user_{self._n}")

    async def probe_health(self):
        from app.camofox.types import CamofoxHealthStatus

        return CamofoxHealthStatus(
            available=self.healthy,
            reason=None if self.healthy else "unreachable",
            version="1.2.3",
        )


@pytest.fixture()
def provider() -> CamofoxAgentBrowserProvider:
    return CamofoxAgentBrowserProvider(_Client())


async def _seeded(provider: CamofoxAgentBrowserProvider):
    created = await provider.create_session()
    tab = await provider.create_tab(created.provider_session_id, "https://example.com")
    return created, tab


async def test_create_session_and_tab(provider: CamofoxAgentBrowserProvider) -> None:
    created = await provider.create_session()
    assert created.provider_session_id.startswith("key_")
    assert "user_" in created.context.get("camofox_user_id", "")
    tab = await provider.create_tab(created.provider_session_id, "https://x")
    assert tab.provider_tab_id == "tab_0"
    assert tab.url == "https://x"


async def test_close_session_idempotent(provider: CamofoxAgentBrowserProvider) -> None:
    created = await provider.create_session()
    await provider.close_session(created.provider_session_id)
    assert created.provider_session_id not in provider._sessions
    # Closing an unknown session is a no-op.
    await provider.close_session("nope")


async def test_require_session_missing_raises(provider: CamofoxAgentBrowserProvider) -> None:
    with pytest.raises(ProviderError):
        await provider.close_tab("nope", "tab_0")


async def test_require_tab_missing_raises(provider: CamofoxAgentBrowserProvider) -> None:
    created, _ = await _seeded(provider)
    with pytest.raises(ProviderError):
        await provider.close_tab(created.provider_session_id, "missing_tab")


async def test_navigate_snapshot_links(provider: CamofoxAgentBrowserProvider) -> None:
    created, tab = await _seeded(provider)
    nav = await provider.navigate(created.provider_session_id, tab.provider_tab_id, "https://x")
    assert nav.ok and nav.url == "https://x"
    snap = await provider.snapshot(created.provider_session_id, tab.provider_tab_id)
    assert snap.snapshot == "tree" and snap.refs_count == 3
    links = await provider.get_links(created.provider_session_id, tab.provider_tab_id)
    assert links.links and links.links[0].url == "https://a"


async def test_click_requires_ref_or_selector(provider: CamofoxAgentBrowserProvider) -> None:
    created, tab = await _seeded(provider)
    with pytest.raises(ProviderError):
        await provider.click(created.provider_session_id, tab.provider_tab_id)
    ok = await provider.click(created.provider_session_id, tab.provider_tab_id, ref="ref1")
    assert ok.ok


async def test_type_and_evaluate(provider: CamofoxAgentBrowserProvider) -> None:
    created, tab = await _seeded(provider)
    assert (await provider.type_text(created.provider_session_id, tab.provider_tab_id, "hi")).ok
    ev = await provider.evaluate(created.provider_session_id, tab.provider_tab_id, "1+1")
    assert ev.ok and ev.result == "r"


async def test_health(provider: CamofoxAgentBrowserProvider) -> None:
    provider._client = _Client(healthy=False)
    health = await provider.health()
    assert health.available is False
    assert health.reason == "unreachable"


async def test_release_drops_local_mapping(provider: CamofoxAgentBrowserProvider) -> None:
    created, tab = await _seeded(provider)
    assert provider.release(created.provider_session_id) is True
    assert provider.release("missing") is False


def test_build_provider_none_when_no_client() -> None:
    assert build_camofox_provider(None) is None


def test_build_provider_builds_from_client() -> None:
    built = build_camofox_provider(_Client())
    assert built is not None
    assert built.name == "camofox"


def test_constructor_requires_client() -> None:
    with pytest.raises(ValueError):
        CamofoxAgentBrowserProvider(None)  # type: ignore[arg-type]


async def test_navigate_drops_session_on_expiry(provider: CamofoxAgentBrowserProvider) -> None:
    created, tab = await _seeded(provider)
    session = provider._sessions[created.provider_session_id]

    async def _expired(tab, url, **kwargs):
        raise CamofoxSessionExpiredError("session gone")

    session.navigate = _expired  # type: ignore[method-assign]

    with pytest.raises(ProviderError):
        await provider.navigate(created.provider_session_id, tab.provider_tab_id, "https://x")
    # The stale session must have been dropped.
    assert created.provider_session_id not in provider._sessions


async def test_create_tab_drops_session_on_generic_error(
    provider: CamofoxAgentBrowserProvider,
) -> None:
    created = await provider.create_session()
    session = provider._sessions[created.provider_session_id]

    async def _boom(url=None):
        raise CamofoxError("boom")

    session.create_tab = _boom  # type: ignore[method-assign]

    with pytest.raises(ProviderError):
        await provider.create_tab(created.provider_session_id, "https://x")
    assert created.provider_session_id not in provider._sessions


async def test_create_tab_drops_session_on_value_error(
    provider: CamofoxAgentBrowserProvider,
) -> None:
    created = await provider.create_session()
    session = provider._sessions[created.provider_session_id]

    async def _bad(url=None):
        raise ValueError("bad url")

    session.create_tab = _bad  # type: ignore[method-assign]

    with pytest.raises(ProviderError):
        await provider.create_tab(created.provider_session_id, "https://x")
    assert created.provider_session_id not in provider._sessions


async def test_close_tab_tab_not_found_is_noop(provider: CamofoxAgentBrowserProvider) -> None:
    created, tab = await _seeded(provider)
    session = provider._sessions[created.provider_session_id]

    async def _gone(tab):
        raise CamofoxTabNotFoundError("gone")

    session.close_tab = _gone  # type: ignore[method-assign]

    # Should not raise (tab already gone upstream is the desired end state).
    await provider.close_tab(created.provider_session_id, tab.provider_tab_id)


async def test_close_tab_other_error_raises(provider: CamofoxAgentBrowserProvider) -> None:
    created, tab = await _seeded(provider)
    session = provider._sessions[created.provider_session_id]

    async def _net(tab):
        raise CamofoxError("network")

    session.close_tab = _net  # type: ignore[method-assign]

    with pytest.raises(ProviderError):
        await provider.close_tab(created.provider_session_id, tab.provider_tab_id)


async def test_close_session_swallows_error(provider: CamofoxAgentBrowserProvider) -> None:
    created = await provider.create_session()
    session = provider._sessions[created.provider_session_id]

    async def _gone():
        raise CamofoxError("already gone")

    session.close = _gone  # type: ignore[method-assign]

    # Closing an expired session is a no-op, must not raise.
    await provider.close_session(created.provider_session_id)
    assert created.provider_session_id not in provider._sessions


async def test_snapshot_get_links_navigate_click_type_evaluate_translate_error(
    provider: CamofoxAgentBrowserProvider,
) -> None:
    created, tab = await _seeded(provider)
    session = provider._sessions[created.provider_session_id]

    async def _r(*a, **k):
        raise CamofoxError("x")

    session.snapshot = _r  # type: ignore[method-assign]
    session.get_links = _r  # type: ignore[method-assign]
    session.navigate = _r  # type: ignore[method-assign]
    session.click = _r  # type: ignore[method-assign]
    session.type_text = _r  # type: ignore[method-assign]
    session.evaluate = _r  # type: ignore[method-assign]

    for call in (
        lambda: provider.snapshot(created.provider_session_id, tab.provider_tab_id),
        lambda: provider.get_links(created.provider_session_id, tab.provider_tab_id),
        lambda: provider.navigate(created.provider_session_id, tab.provider_tab_id, "https://x"),
        lambda: provider.click(created.provider_session_id, tab.provider_tab_id, ref="r"),
        lambda: provider.type_text(created.provider_session_id, tab.provider_tab_id, "t"),
        lambda: provider.evaluate(created.provider_session_id, tab.provider_tab_id, "1"),
    ):
        with pytest.raises(ProviderError):
            await call()
        # Snapshot/navigate/error on session expiry would drop; a generic
        # CamofoxError is not an expiry signal so the mapping survives.
        assert created.provider_session_id in provider._sessions
