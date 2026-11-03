"""Tests for the browser backend abstraction and the Camofox browser backend.

Covers:

* :class:`BrowserBackendResult` — mapping ``FetchResult`` ↔ backend result, and
  the ``to_fetch_result`` conversion used by the scraper service.
* :class:`BotasaurusBackend` — thin adapter that stamps the backend name and
  normalises a driver exception into :class:`BrowserError`.
* :class:`CamofoxBrowserBackend` — session lifecycle (create → create tab →
  optional cookie import → navigate → wait for HTML → capture final URL →
  close tab in ``finally``), the render wait budget, cookie import behaviour,
  and error normalisation.  Uses scripted fakes of the Camofox client and
  session so no real Camofox service is required.
* The :class:`ScrapeRequest` keyword forwarding contract.
"""

import asyncio

import pytest

from app.camofox.config import CamofoxConfig
from app.camofox.session import CamofoxSession
from app.camofox.types import (
    CamofoxEvaluateResult,
    CamofoxNavigateResult,
    CamofoxTab,
)
from app.core.config import Settings
from app.core.errors import BrowserError, CamofoxError, CamofoxUnavailableError
from app.scraper.browser_backend import (
    BrowserBackend,
    BrowserBackendResult,
    ScrapeRequest,
    assert_contract,
)
from app.scraper.browser_backends import (
    BotasaurusBackend,
    CamofoxBrowserBackend,
    build_camofox_backend,
)
from app.scraper.http_fetcher import FetchResult

# --------------------------------------------------------------------------- fakes


def _camofox_config(**overrides) -> CamofoxConfig:
    base = dict(
        enabled=True,
        base_url="http://camofox.test:9543",
        timeout_seconds=5.0,
        access_key="access",
        api_key="elevated",
    )
    base.update(overrides)
    return CamofoxConfig(**base)


class _FakeTab:
    """Stand-in for :class:`CamofoxTab` with a scripted HTML/URL result."""

    def __init__(self, html: str, final_url: str, tab_id: str = "tab-1") -> None:
        self.tab_id = tab_id
        self.url = final_url
        self._html = html
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeSession(CamofoxSession):
    """A :class:`CamofoxSession` whose operations are scripted.

    We bypass the real ``CamofoxSession.__init__`` (which requires a real
    ``CamofoxClient``) and set the bookkeeping attributes directly.  Every
    operation method is overridden so no HTTP is performed.
    """

    def __init__(self, client, tab: _FakeTab) -> None:
        # Bypass real init (avoids requiring a real CamofoxClient).
        self._client = client
        self.user_id = "test-user"
        self.session_key = "test-key"
        self._tabs: dict = {}
        self._closed_tabs: set = set()
        self._fake_tab = tab
        self._navigate_error = None
        self._eval_error = None
        self._imported = None
        self.added_tab = False

    async def create_tab(self, url: str | None = None) -> CamofoxTab:
        self.add_tab(self._fake_tab)  # register locally so assert_owns passes
        return self._fake_tab

    async def navigate(self, tab, url: str | None = None, **kwargs) -> CamofoxNavigateResult:
        self.assert_owns(tab)
        if self._navigate_error is not None:
            raise self._navigate_error
        # Simulate a redirect: the tab's url is the *final* URL (set at init).
        # The navigate call itself doesn't change tab.url in this fake.
        return CamofoxNavigateResult(ok=True, url=tab.url)

    async def evaluate(self, tab, expression: str, *, timeout_ms: int | None = None):
        self.assert_owns(tab)
        if self._eval_error is not None:
            raise self._eval_error
        if "document" in expression:
            return CamofoxEvaluateResult(ok=True, result=self._fake_tab._html)
        return CamofoxEvaluateResult(ok=True, result=self._fake_tab.url)

    async def import_cookies(self, cookies, *, tab=None) -> int:
        self.assert_owns(tab)
        self._imported = cookies
        return len(cookies)

    async def close_tab(self, tab) -> None:
        self.forget_tab(tab.tab_id)
        await self._fake_tab.close()

    def set_navigate_error(self, exc: Exception) -> None:
        self._navigate_error = exc

    def set_eval_error(self, exc: Exception) -> None:
        self._eval_error = exc


class _FakeCamofoxClient:
    """Scripted stand-in for :class:`CamofoxClient`.

    * ``create_session(name, *, user_id=None, session_key=None)`` returns a
      :class:`_FakeSession` backed by the configured ``tab``.
    * ``import_cookies(session, cookies, *, tab=None)`` returns the count.
    * ``probe_health()`` returns ``available=True`` (so ``health()`` is healthy).
    * When ``session_error`` is set, ``create_session`` raises it.
    """

    def __init__(
        self,
        config: CamofoxConfig | None = None,
        *,
        tab: _FakeTab | None = None,
        session_error: Exception | None = None,
    ) -> None:
        self.config = config or _camofox_config()
        self._tab = tab
        self._session_error = session_error
        self.sessions_created = 0
        self.imports: list[list] = []

    def create_session(self, name=None, *, user_id=None, session_key=None) -> _FakeSession:
        self.sessions_created += 1
        if self._session_error is not None:
            raise self._session_error
        if self._tab is None:
            raise CamofoxUnavailableError("service unreachable")
        return _FakeSession(self, self._tab)

    def import_cookies(self, session, cookies, *, tab=None) -> int:
        self.imports.append(cookies)
        return len(cookies)

    async def probe_health(self):
        from app.camofox.types import CamofoxHealthStatus

        return CamofoxHealthStatus(available=True)

    async def close(self) -> None:
        self.closed = True


def _settings(**overrides) -> Settings:
    base = dict(
        api_key="k",
        scraper_timeout_seconds=30,
        scraper_user_agent_profile="desktop_es",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _settings_with_cookies() -> Settings:
    return _settings(
        camofox_api_key="camo-123",
        camofox_access_key="camo-access",
        camofox_api_base_url="http://camofox.test:9543",
        scraper_cookies_file="/tmp/cookies.txt",
        scraper_cookie_domains="example.com",
        scraper_cookie_ttl_seconds=1200,
    )


def _tab(html: str, final_url: str = "https://example.com/") -> _FakeTab:
    return _FakeTab(html=html, final_url=final_url)


def _backend(
    client: _FakeCamofoxClient, config: CamofoxConfig | None = None
) -> CamofoxBrowserBackend:
    return CamofoxBrowserBackend(config or _camofox_config(), client=client)


# --------------------------------------------------------------------------- BrowserBackendResult


def test_backend_result_round_trips_fetch_result():
    fr = FetchResult(
        html="<html>hi</html>",
        status_code=200,
        final_url="https://example.com/page",
        headers={"Content-Type": "text/html"},
    )
    res = BrowserBackendResult(fr, "camofox", {"note": "ok"})
    out = res.to_fetch_result()
    assert out.html == fr.html
    assert out.status_code == 200
    assert out.final_url == "https://example.com/page"
    assert out.backend_name == "camofox"
    assert out.backend_metadata == {"note": "ok"}


def test_backend_result_defaults_from_fetch_result():
    fr = FetchResult(
        html="x",
        status_code=200,
        final_url="https://example.com",
        backend_name="botasaurus",
        backend_metadata={"a": 1},
    )
    res = BrowserBackendResult(fr)
    assert res.backend_name == "botasaurus"
    assert res.backend_metadata == {"a": 1}
    assert res.to_fetch_result().backend_name == "botasaurus"


def test_backend_result_falls_back_to_browser_name():
    res = BrowserBackendResult(FetchResult(html="x", status_code=200, final_url="https://example.com"))
    assert res.backend_name == "browser"
    assert res.backend_metadata == {}


def test_backend_result_to_fetch_result_is_idempotent():
    fr = FetchResult(html="x", status_code=200, final_url="https://example.com")
    res = BrowserBackendResult(fr, "camofox")
    second = BrowserBackendResult(res.to_fetch_result(), "camofox")
    assert second.to_fetch_result().backend_name == "camofox"


def test_contract_assertion_accepts_valid_result():
    result = FetchResult(html="x" * 600, status_code=200, final_url="https://example.com")
    assert_contract(BrowserBackendResult(result, "camofox"))


def test_contract_assertion_rejects_plain_fetch_result():
    with pytest.raises(AssertionError):
        # A plain FetchResult is not a BrowserBackendResult.
        assert_contract(FetchResult(html="x", status_code=200, final_url="https://example.com"))


def test_protocol_is_runtime_checkable():
    # A BotasaurusBackend satisfies the structural protocol.
    backend = BotasaurusBackend({"headless": True})
    assert isinstance(backend, BrowserBackend)


# --------------------------------------------------------------------------- ScrapeRequest


def test_scrape_request_defaults():
    req = ScrapeRequest(url="https://example.com")
    assert req.timeout_seconds is None
    assert req.wait_until == "networkidle"
    assert req.scroll_config == {}
    assert req.screenshot_path is None
    assert req.proxy_url is None


def test_scrape_request_keyword_forwarding():
    req = ScrapeRequest(
        url="https://example.com",
        timeout_seconds=15,
        wait_until="domcontentloaded",
        wait_selector="h1",
        scroll_config={"enabled": True},
        screenshot_path="/tmp/x.png",
        proxy_url="http://127.0.0.1:8080",
    )
    assert req.timeout_seconds == 15
    assert req.wait_until == "domcontentloaded"
    assert req.wait_selector == "h1"
    assert req.scroll_config == {"enabled": True}
    assert req.screenshot_path == "/tmp/x.png"
    assert req.proxy_url == "http://127.0.0.1:8080"


# --------------------------------------------------------------------------- BotasaurusBackend


class _StubFetcher:
    """Minimal stand-in for :class:`BrowserFetcher`."""

    def __init__(self, html: str | None = None, error: Exception | None = None) -> None:
        self._html = html
        self._error = error
        self.closed = False
        self.last_args: dict | None = None

    async def fetch(self, **kwargs):
        self.last_args = kwargs
        if self._error is not None:
            raise self._error
        return FetchResult(
            html=self._html or "<html>botasaurus</html>",
            status_code=200,
            final_url=kwargs.get("url", "https://example.com"),
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def botasaurus_stub_args() -> dict:
    return {
        "headless": True,
        "arguments": ["--headless=new"],
        "timeout_seconds": 30,
        "user_agent": "ua",
        "window_size": (1366, 768),
        "proxy_url": None,
        "idle_timeout_seconds": 300,
        "max_uses": None,
    }

@pytest.fixture
def botasaurus_full_args() -> dict:
    return {
        "headless": True,
        "arguments": ["--headless=new"],
        "timeout_seconds": 30,
        "user_agent": "ua",
        "window_size": (1366, 768),
        "proxy_url": None,
        "idle_timeout_seconds": 300,
        "max_uses": None,
    }


async def test_botasaurus_backend_stamps_name(botasaurus_stub_args):
    backend = BotasaurusBackend(botasaurus_stub_args)
    backend._fetcher = _StubFetcher()
    result = await backend.fetch(
        url="https://example.com",
        timeout_seconds=None,
        wait_until="networkidle",
        wait_selector=None,
        scroll_config={},
        screenshot_path=None,
        proxy_url=None,
        cookies=None,
        extra_headers=None,
    )
    assert result.backend_name == "botasaurus"


async def test_botasaurus_backend_normalises_driver_error(botasaurus_stub_args):
    backend = BotasaurusBackend(botasaurus_stub_args)
    backend._fetcher = _StubFetcher(error=RuntimeError("driver exploded"))
    with pytest.raises(BrowserError):
        await backend.fetch(
            url="https://example.com",
            timeout_seconds=None,
            wait_until="networkidle",
            wait_selector=None,
            scroll_config={},
            screenshot_path=None,
            proxy_url=None,
            cookies=None,
            extra_headers=None,
        )


async def test_botasaurus_backend_passes_args_to_fetcher(botasaurus_full_args):
    botasaurus_stub_args = botasaurus_full_args
    backend = BotasaurusBackend(botasaurus_stub_args)
    backend._fetcher = _StubFetcher()
    await backend.fetch(
        url="https://example.com",
        timeout_seconds=7,
        wait_until="domcontentloaded",
        wait_selector="#x",
        scroll_config={"enabled": True},
        screenshot_path="/tmp/s.png",
        proxy_url="http://127.0.0.1:8080",
        cookies=[{"name": "a", "value": "1"}],
        extra_headers={"X-Test": "1"},
    )
    assert backend._fetcher.last_args["timeout_seconds"] == 7
    assert backend._fetcher.last_args["wait_selector"] == "#x"
    assert backend._fetcher.last_args["wait_until"] == "domcontentloaded"
    assert backend._fetcher.last_args["scroll_config"] == {"enabled": True}
    assert backend._fetcher.last_args["screenshot_path"] == "/tmp/s.png"
    assert backend._fetcher.last_args["proxy_url"] == "http://127.0.0.1:8080"


def test_botasaurus_fetcher_only_wraps_existing():
    fetcher = _StubFetcher()
    backend = BotasaurusBackend.fetcher_only(fetcher)
    assert backend._fetcher is fetcher


def test_botasaurus_health_and_close():
    backend = BotasaurusBackend({"headless": True})
    assert backend.name == "botasaurus"
    backend.close()  # no fetcher → no-op, must not raise


# --------------------------------------------------------------------------- CamofoxBrowserBackend


async def test_camofox_backend_happy_path():
    client = _FakeCamofoxClient(tab=_tab("<html><body>full page</body></html>"))
    backend = _backend(client)
    result = await backend.fetch(
        url="https://example.com/page",
        timeout_seconds=10,
        wait_until="networkidle",
        wait_selector=None,
        scroll_config={},
        screenshot_path=None,
        proxy_url=None,
        cookies=None,
        extra_headers=None,
    )
    assert result.backend_name == "camofox"
    assert result.status_code == 200
    assert "full page" in result.html
    assert client.sessions_created == 1


async def test_camofox_backend_captures_final_url_after_redirect():
    client = _FakeCamofoxClient(tab=_tab("<html>landed</html>", final_url="https://example.com/landed"))
    backend = _backend(client)
    result = await backend.fetch(
        url="https://example.com",
        timeout_seconds=None,
        wait_until="networkidle",
        wait_selector=None,
        scroll_config={},
        screenshot_path=None,
        proxy_url=None,
        cookies=None,
        extra_headers=None,
    )
    assert result.final_url == "https://example.com/landed"


async def test_camofox_backend_closes_tab_on_failure():
    # The document never reports complete → the render budget exhausts and the
    # backend raises BrowserError, but the tab is still closed in finally.
    closed: dict = {}

    class _NeverCompleteTab(_FakeTab):
        async def evaluate(self, expression, **kwargs):
            return CamofoxEvaluateResult(ok=True, result=None)  # not complete

        async def close(self):
            closed["tab"] = True

    class _Session(_FakeSession):
        def __init__(self, client, tab):
            super().__init__(client, tab)
            self._eval_error = None
            # Force the render budget to be tiny so the test is fast.
            self._config = _camofox_config(timeout_seconds=0.05)

    client = _FakeCamofoxClient()
    tab = _NeverCompleteTab(html="", final_url="https://example.com")
    client._tab = tab

    class _Client(_FakeCamofoxClient):
        def create_session(self, name=None, *, user_id=None, session_key=None):
            self.sessions_created += 1
            return _Session(self, tab)

    session_client = _Client()
    backend = _backend(session_client)
    with pytest.raises(BrowserError):
        await backend.fetch(
            url="https://example.com",
            timeout_seconds=0.05,
            wait_until="networkidle",
            wait_selector=None,
            scroll_config={},
            screenshot_path=None,
            proxy_url=None,
            cookies=None,
            extra_headers=None,
        )
    assert closed.get("tab") is True


async def test_camofox_backend_session_creation_error():
    client = _FakeCamofoxClient(session_error=CamofoxUnavailableError("service unreachable"))
    backend = _backend(client)
    # create_session is outside the try block → the raw CamofoxError propagates.
    with pytest.raises((BrowserError, CamofoxError)):
        await backend.fetch(
            url="https://example.com",
            timeout_seconds=None,
            wait_until="networkidle",
            wait_selector=None,
            scroll_config={},
            screenshot_path=None,
            proxy_url=None,
            cookies=None,
            extra_headers=None,
        )


async def test_camofox_backend_navigate_error_is_browser_error():
    client = _FakeCamofoxClient(tab=_tab("<html>x</html>"))
    backend = _backend(client)
    # Inject a navigate error by pre-setting the session's navigate error via
    # monkeypatching the session class factory.
    import app.scraper.browser_backends.camofox as mod  # noqa: F401 (context)

    class _SessionErrorFactory:
        def create_session(self, name=None, **kwargs):
            s = _FakeSession(self, _tab("<html>x</html>"))
            s.set_navigate_error(CamofoxError("boom"))
            return s

    client.create_session = _SessionErrorFactory().create_session  # type: ignore
    with pytest.raises(BrowserError):
        await backend.fetch(
            url="https://example.com",
            timeout_seconds=None,
            wait_until="networkidle",
            wait_selector=None,
            scroll_config={},
            screenshot_path=None,
            proxy_url=None,
            cookies=None,
            extra_headers=None,
        )


async def test_camofox_backend_imports_cookies_when_configured():
    client = _FakeCamofoxClient(tab=_tab("<html>with cookies</html>"))
    backend = _backend(client)
    result = await backend.fetch(
        url="https://example.com/x",
        timeout_seconds=None,
        wait_until="networkidle",
        wait_selector=None,
        scroll_config={},
        screenshot_path=None,
        proxy_url=None,
        cookies=[{"name": "session", "value": "abc"}],
        extra_headers=None,
    )
    assert result.status_code == 200
    # Cookie import uses the client's sensitive route → it was called once.
    assert len(client.imports) == 1
    assert client.imports[0][0].name == "session"


def test_camofox_backend_skips_cookie_import_without_api_key():
    # No api_key → cookie import is skipped (warning logged), no import call.
    client = _FakeCamofoxClient(
        config=_camofox_config(api_key=None, access_key="access"),
        tab=_tab("<html>no cookies</html>"),
    )
    backend = _backend(client, config=_camofox_config(api_key=None, access_key="access"))

    async def _run():
        # The fetch will call import_cookies only when self._can_import_cookies.
        await backend.fetch(
            url="https://example.com/x",
            timeout_seconds=None,
            wait_until="networkidle",
            wait_selector=None,
            scroll_config={},
            screenshot_path=None,
            proxy_url=None,
            cookies=[{"name": "session", "value": "abc"}],
            extra_headers=None,
        )

    asyncio.run(_run())
    assert client.imports == []


def test_camofox_name_and_health():
    backend = _backend(_FakeCamofoxClient(tab=_tab("<html>x</html>")))
    assert backend.name == "camofox"
    assert asyncio.run(backend.health()) is True


def test_camofox_health_reports_unavailable_when_client_missing():
    backend = _backend(_FakeCamofoxClient())
    backend._client = None  # simulate an unavailable client
    assert asyncio.run(backend.health()) is False


def test_build_camofox_backend_requires_client():
    # No client, and config disabled → returns None (the backend is optional).
    assert build_camofox_backend(_settings(), client=None) is None


def test_build_camofox_backend_with_client():
    # When a client is provided, the backend is built regardless of the
    # settings' camofox section (the client is the source of truth).
    client = _FakeCamofoxClient(tab=_tab("<html>x</html>"))
    backend = build_camofox_backend(_settings(), client=client)
    assert backend is not None
    assert backend.name == "camofox"
