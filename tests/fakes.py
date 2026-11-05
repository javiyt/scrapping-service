"""Shared in-memory fakes for the end-to-end integration tests (ticket #129).

These fakes satisfy the *structural* contracts of the orchestration layers
(search providers, scrape browser backends, agent-browser providers, and the
Camofox client), so the whole stack — REST app, routers, caches, orchestrators
— can be exercised with **zero network I/O and no external services**.

Nothing here does real I/O: every ``client(...).send()`` call raises, which
guarantees that a test passing with these fakes never touched the internet.

The fakes are deterministic: behaviour is scripted through constructor
arguments or per-test state (e.g. ``httpx.MockTransport`` handlers).
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.browser.providers.base import (
    ProviderActionResult,
    ProviderError,
    ProviderEvaluateResult,
    ProviderLink,
    ProviderLinks,
    ProviderNavigateResult,
    ProviderSession,
    ProviderSnapshot,
    ProviderTab,
)
from app.cache.models import CacheEntry
from app.cache.sqlite_cache import SqliteCache
from app.camofox.types import (
    CamofoxEvaluateResult,
    CamofoxTab,
)
from app.core.errors import (
    BrowserError,
    SearchProviderUnavailableError,
)
from app.scraper.http_fetcher import FetchResult
from app.search.models import (
    SearchCapabilities,
    SearchMetadata,
    SearchQuery,
    SearchResponse,
    SearchResult,
    Vertical,
)


def _no_network(
    request: httpx.Request,
) -> httpx.Response:  # pragma: no cover - raising IS the point
    raise AssertionError(
        f"Test fakes attempted real network I/O: {request.method} {request.url} "
        "(integration tests must not use the internet)"
    )


def dead_network_client() -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` whose transport refuses every request.

    Useful for wiring *real* clients (e.g. ``CamofoxClient``) into a fake:
    any accidental live request fails loudly instead of hitting the network.
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(_no_network))


# ===================================================================== search


class FakeSearchProvider:
    """In-memory :class:`SearchProvider` with fully scripted behaviour."""

    def __init__(
        self,
        name: str = "fake",
        *,
        available: bool = True,
        verticals: list[Vertical] | None = None,
        supports_language: bool = True,
        supports_safe_search: bool = True,
        supports_paging: bool = True,
        time_ranges: list[Any] | None = None,
        capability_error: Exception | None = None,
        search_error: Exception | None = None,
        result_url: str = "https://example.com/first",
        result_title: str | None = None,
    ) -> None:
        self._name = name
        self._available = available
        self._verticals = verticals or [Vertical.WEB]
        self._supports_language = supports_language
        self._supports_safe_search = supports_safe_search
        self._supports_paging = supports_paging
        self._time_ranges = time_ranges or []
        self._capability_error = capability_error
        self._search_error = search_error
        self._result_url = result_url
        self._result_title = result_title or f"Result from {name}"
        self.search_calls: list[SearchQuery] = []

    @property
    def name(self) -> str:
        return self._name

    async def capabilities(self) -> SearchCapabilities:
        if self._capability_error is not None:
            raise self._capability_error
        return SearchCapabilities(
            verticals=self._verticals,
            time_ranges=self._time_ranges,
            supports_language=self._supports_language,
            supports_safe_search=self._supports_safe_search,
            supports_paging=self._supports_paging,
            available=self._available,
        )

    async def search(self, request: SearchQuery) -> SearchResponse:
        self.search_calls.append(request)
        if self._search_error is not None:
            raise self._search_error
        return SearchResponse(
            query=request.query,
            verticals=request.verticals,
            results=[
                SearchResult(
                    title=self._result_title,
                    url=self._result_url,
                    snippet="Snippet",
                    rank=1,
                    vertical=request.verticals[0],
                    provider=self._name,
                )
            ],
            metadata=SearchMetadata(
                provider=self._name,
                providers_attempted=[self._name],
                elapsed_ms=1,
            ),
        )

    # ------------------------------------------------------------------ state
    def set_available(self, available: bool) -> None:
        self._available = available

    def set_search_error(self, error: Exception | None) -> None:
        self._search_error = error

    @property
    def search_call_count(self) -> int:
        return len(self.search_calls)


# ====================================================================== cache


class FakeSqliteCache(SqliteCache):
    """A :class:`SqliteCache` (real SQLite, on a real temp path) with
    time-travelling helpers for deterministic expiry tests.

    Time travelling rewrites the *stored* datetimes in the database, so the
    ``is_expired`` logic under test is the real production logic.
    """

    def backdate_entry(self, cache_key: str, seconds: float = 3600.0) -> bool:
        """Shift an entry's ``fetched_at`` / ``expires_at`` into the past."""
        if self._conn is None:
            return False
        cur = self._conn.cursor()
        cur.execute("SELECT fetched_at, expires_at FROM cache WHERE cache_key = ?", (cache_key,))
        row = cur.fetchone()
        if row is None or row["expires_at"] is None:
            return False
        expired = datetime.fromisoformat(row["expires_at"]) - timedelta(seconds=seconds)
        fetched = datetime.fromisoformat(row["fetched_at"]) - timedelta(seconds=seconds)
        self._conn.execute(
            "UPDATE cache SET fetched_at = ?, expires_at = ? WHERE cache_key = ?",
            (fetched.isoformat(), expired.isoformat(), cache_key),
        )
        self._conn.commit()
        cur.close()
        return True


def ok_cache_entry(url: str, mode: str = "http", expires_in: float = 3600.0) -> CacheEntry:
    """A valid (non-blocked) scrape cache entry for *url*."""
    from app.scraper.normalizer import make_cache_key

    now = datetime.now(UTC)
    html = "<html><body>" + "<p>real content " * 40 + "</body></html>"
    return CacheEntry(
        cache_key=make_cache_key(url),
        url=url,
        final_url=url,
        status_code=200,
        html=html,
        fetched_at=now,
        expires_at=now + timedelta(seconds=expires_in),
        mode=mode,
        content_length=len(html),
        headers=None,
        error_metadata=None,
    )


# ====================================================================== scrape


class FakeBrowserBackend:
    """A :class:`BrowserBackend` with scripted results/errors."""

    def __init__(
        self,
        name: str = "botasaurus",
        *,
        result: FetchResult | None = None,
        error: Exception | None = None,
        fetch_error: Exception | None = None,
        fetch_result: FetchResult | None = None,
    ) -> None:
        self._name = name
        self._fetch_error = fetch_error or error
        self._fetch_result = fetch_result or result
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append(url)
        if self._fetch_error is not None:
            raise self._fetch_error
        if self._fetch_result is not None:
            self._fetch_result.backend_name = self._name
            return self._fetch_result
        raise BrowserError("no result scripted")

    async def health(self) -> bool:
        return True

    def close(self) -> None:
        pass


def ok_fetch(url: str, backend: str | None = None, status_code: int = 200) -> FetchResult:
    """A valid rendered-document result (long enough to count as usable)."""
    html = f"<html><body>{url} " + "content " * 60 + "</body></html>"
    return FetchResult(
        html=html,
        status_code=status_code,
        final_url=url,
        backend_name=backend,
    )


# ===================================================================== camofox


class FakeCamofoxTab:
    """Structural stand-in for :class:`app.camofox.types.CamofoxTab`."""

    def __init__(self, tab_id: str, url: str | None = None, title: str | None = None) -> None:
        self.tab_id = tab_id
        self.url = url
        self.title = title


class FakeCamofoxSession:
    """Structural stand-in for :class:`app.camofox.session.CamofoxSession`.

    ``evaluate`` must return a real :class:`CamofoxEvaluateResult` because the
    Camofox browser backend inspects ``result.ok`` / ``result.result``.
    """

    def __init__(self, session_key: str, html: str | None = None, title: str | None = None) -> None:
        self.session_key = session_key
        self.user_id = f"user_{session_key}"
        self._html = html
        self._title = title
        self._tabs: list[CamofoxTab] = []

    def close_tab(self, tab: Any) -> None:
        self._tabs = [t for t in self._tabs if t is not tab]

    def navigate(self, tab: Any, url: str, **kwargs: Any) -> None:
        tab.url = url

    async def evaluate(self, tab: Any, expression: str, **kwargs: Any) -> CamofoxEvaluateResult:
        return CamofoxEvaluateResult(ok=True, result=self._html, result_type="str")


class FakeCamofoxClient:
    """Structural stand-in for :class:`app.camofox.client.CamofoxClient`.

    Fakes the *session* surface used by both the Camofox browser backend
    (``create_session`` / ``import_cookies``) and the Camofox agent-browser
    provider (``create_session``).  When ``fail_with`` is set, every session
    operation raises it — model a down/expired Camofox service.
    """

    def __init__(
        self,
        *,
        html: str | None = None,
        fail_with: Exception | None = None,
        healthy: bool = True,
    ) -> None:
        self._html = html
        self._fail_with = fail_with
        self._healthy = healthy
        self._counter = itertools.count(1)
        self.sessions_created = 0
        # The real client is an async context manager; expose a no-op.
        self._http: httpx.AsyncClient | None = None

    @property
    def config(self) -> Any:
        # ``CamofoxBrowserBackend`` reads ``client.config.api_key`` to decide
        # whether per-request cookie import is possible.
        return self

    api_key: str | None = None

    def create_session(self) -> FakeCamofoxSession:
        if self._fail_with is not None:
            raise self._fail_with
        self.sessions_created += 1
        return FakeCamofoxSession(f"key_{self._counter.__next__()}")

    async def probe_health(self) -> Any:
        from app.camofox.types import CamofoxHealthStatus

        return CamofoxHealthStatus(available=self._healthy, reason=None, version="9.9.9")

    def import_cookies(self, session: Any, cookies: list[Any], tab: Any | None = None) -> int:
        if self._fail_with is not None:
            raise self._fail_with
        return len(cookies)

    async def close_session(self, session: Any) -> None:
        pass

    async def close(self) -> None:
        pass


# ================================================================== agent browser


class FakeAgentBrowserProvider:
    """In-memory :class:`AgentBrowserProvider` for ownership/limits tests.

    Every operation carries an audit trail (``calls``) so tests can assert
    exactly which upstream calls happened (and that a *different* profile's
    request never reaches the provider with another profile's IDs).
    """

    name = "fake-browser"
    supports_evaluate = True

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, FakeCamofoxTab]] = {}
        self._counter = itertools.count(1)
        self.calls: list[tuple[str, Any, Any]] = []
        self.snapshot_payload = "rootWebArea\n  link @e1 example\n  button @e2 go"

    # ------------------------------------------------------------- sessions
    async def create_session(self) -> ProviderSession:
        sid = f"ps_{self._counter.__next__()}"
        self._sessions[sid] = {}
        return ProviderSession(provider_session_id=sid)

    async def close_session(self, provider_session_id: str) -> None:
        self._sessions.pop(provider_session_id, None)

    # ----------------------------------------------------------------- tabs
    async def create_tab(self, provider_session_id: str, url: str | None = None) -> ProviderTab:
        self._require(provider_session_id)
        tab = FakeCamofoxTab(f"pt_{self._counter.__next__()}")
        tab.url = url
        tab.title = "T"
        self._sessions[provider_session_id][tab.tab_id] = tab
        self.calls.append(("create_tab", provider_session_id, url))
        return ProviderTab(provider_tab_id=tab.tab_id, url=url, title="T")

    async def close_tab(self, provider_session_id: str, provider_tab_id: str) -> None:
        self._require(provider_session_id)
        self._sessions[provider_session_id].pop(provider_tab_id, None)
        self.calls.append(("close_tab", provider_session_id, provider_tab_id))

    # ------------------------------------------------------------- page data
    async def snapshot(
        self, provider_session_id: str, provider_tab_id: str, *, offset: int = 0
    ) -> ProviderSnapshot:
        self._require(provider_session_id)
        self._require_tab(provider_session_id, provider_tab_id)
        self.calls.append(("snapshot", provider_session_id, provider_tab_id))
        return ProviderSnapshot(
            url="https://example.com",
            title="Example",
            snapshot=self.snapshot_payload,
            refs_count=2,
            truncated=False,
        )

    async def get_links(
        self, provider_session_id: str, provider_tab_id: str, *, limit: int = 50
    ) -> ProviderLinks:
        self._require(provider_session_id)
        self._require_tab(provider_session_id, provider_tab_id)
        self.calls.append(("get_links", provider_session_id, provider_tab_id))
        return ProviderLinks(links=[ProviderLink("https://example.com/a", "a")], has_more=False)

    # --------------------------------------------------------------- actions
    async def navigate(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        url: str,
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderNavigateResult:
        self._require(provider_session_id)
        self._require_tab(provider_session_id, provider_tab_id)
        self.calls.append(("navigate", provider_session_id, url))
        return ProviderNavigateResult(ok=True, url=url, title="T")

    async def click(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        *,
        ref: str | None = None,
        selector: str | None = None,
    ) -> ProviderActionResult:
        self._require(provider_session_id)
        self._require_tab(provider_session_id, provider_tab_id)
        self.calls.append(("click", provider_session_id, ref))
        return ProviderActionResult(ok=True, url="https://example.com")

    async def type_text(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        text: str,
        *,
        ref: str | None = None,
        selector: str | None = None,
    ) -> ProviderActionResult:
        self._require(provider_session_id)
        self._require_tab(provider_session_id, provider_tab_id)
        self.calls.append(("type_text", provider_session_id, text))
        return ProviderActionResult(ok=True, url="https://example.com")

    async def evaluate(
        self,
        provider_session_id: str,
        provider_tab_id: str,
        expression: str,
        *,
        timeout_ms: int | None = None,
    ) -> ProviderEvaluateResult:
        self._require(provider_session_id)
        self._require_tab(provider_session_id, provider_tab_id)
        self.calls.append(("evaluate", provider_session_id, expression))
        return ProviderEvaluateResult(ok=True, result="42", result_type="number")

    # ---------------------------------------------------------------- internals
    def _require(self, provider_session_id: str) -> None:
        if provider_session_id not in self._sessions:
            raise ProviderError("upstream session not found")

    def _require_tab(self, provider_session_id: str, provider_tab_id: str) -> None:
        if provider_tab_id not in self._sessions[provider_session_id]:
            raise ProviderError("upstream tab not found")

    def closed_sessions(self) -> int:
        return len(self._sessions)


class ProviderLinkItem:
    """A link item in the structural provider contract shape."""

    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.text = text


# =============================================================== transcript


class FakeTranscriptProvider:
    """In-memory :class:`TranscriptProvider` with scripted behaviour."""

    def __init__(
        self,
        name: str = "fake-transcript",
        *,
        healthy: bool | None = None,
        available: bool | None = None,
        error: Exception | None = None,
        fetch_error: Exception | None = None,
        health_error: Exception | None = None,
    ) -> None:
        self._name = name
        # ``available`` = health reports available; ``healthy`` selects between
        # "probe raises" (None) and "probe answers unavailable" (False).
        self._available = True if available is None else available
        self._healthy = healthy if healthy is not None else True
        self._health_error = health_error
        self._fetch_error = fetch_error or error
        self.fetch_calls: list[tuple[str, list[str]]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def transcript_calls(self) -> int:
        """Alias so search- and transcript-style fakes read identically in tests."""
        return len(self.fetch_calls)

    async def health(self) -> Any:
        from app.transcript.contract import TranscriptHealth

        if self._health_error is not None:
            raise self._health_error
        if not self._healthy:
            return TranscriptHealth(available=False, reason="upstream down")
        return TranscriptHealth(available=self._available)

    async def fetch(self, video_id: str, languages: list[str]) -> Any:
        from app.transcript.models import (
            TranscriptMetadata,
            TranscriptResponse,
            TranscriptSegment,
        )

        self.fetch_calls.append((video_id, list(languages)))
        if self._fetch_error is not None:
            raise self._fetch_error
        segments = [TranscriptSegment(start=0.0, duration=1.0, text=f"hello from {self._name}")]
        return TranscriptResponse.from_segments(
            video_id=video_id,
            segments=segments,
            provider=self._name,
            metadata=TranscriptMetadata(
                provider=self._name,
                providers_attempted=[self._name],
            ),
        )

    # ------------------------------------------------------------------ state
    def set_transcript_error(self, error: Exception | None) -> None:
        self._fetch_error = error

    def set_healthy(self, healthy: bool) -> None:
        self._healthy = healthy


# ==================================================================== exports

__all__ = [
    "FakeAgentBrowserProvider",
    "FakeBrowserBackend",
    "FakeCamofoxClient",
    "FakeCamofoxSession",
    "FakeCamofoxTab",
    "FakeSearchProvider",
    "FakeSqliteCache",
    "FakeTranscriptProvider",
    "SearchProviderUnavailableError",
    "ok_cache_entry",
    "ok_fetch",
]
