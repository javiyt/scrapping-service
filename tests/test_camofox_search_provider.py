"""Tests for the Camofox :class:`SearchProvider` implementation.

The Camofox client is a hand-rolled fake (see :class:`FakeCamofoxClient`) —
no real Camofox service, browser, or network is ever touched, as required
by the ticket.  The fake mirrors the client's tab/session lifecycle so the
provider's *resource cleanup* behaviour can be asserted directly.
"""

import asyncio
import uuid
from typing import Any

import pytest

from app.camofox.config import CamofoxConfig
from app.camofox.session import CamofoxSession
from app.camofox.types import (
    CamofoxActionResult,
    CamofoxCookie,
    CamofoxEvaluateResult,
    CamofoxHealthStatus,
    CamofoxLink,
    CamofoxLinks,
    CamofoxNavigateResult,
    CamofoxSnapshot,
    CamofoxTab,
    CamofoxTabInfo,
)
from app.core.errors import (
    CamofoxError,
    CamofoxTimeoutError,
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
)
from app.search import SearchProvider, SearchQuery
from app.search.models import SafeSearch, TimeRange, Vertical
from app.search.providers.camofox import (
    CamofoxSearchConfig,
    CamofoxSearchProvider,
)
from app.search.providers.camofox.mapping import (
    SOURCE_TO_MACRO,
    normalize_source_name,
)
from app.search.providers.camofox.provider import (
    _collect_snippets,
    _host_matches,
    _host_of,
    _parse_snapshot_links,
)

CLIENT_CONFIG = CamofoxConfig(
    enabled=True,
    base_url="http://camofox:9377",
    access_key="access-secret",
    api_key="api-secret",
)

HEALTH_OK = CamofoxHealthStatus(available=True)
HEALTH_DOWN = CamofoxHealthStatus(available=False, reason="server reported not running")

# A Google results page: accessibility-tree snapshot + extracted links.
GOOGLE_SNAPSHOT = """
- search "Miami Dolphins" [ref=e1, url=https://www.google.com/search?q=Miami+Dolphins]
- link "Dolphins 2026 season preview" [ref=e4, url=https://www.espn.com/nfl/story/_/id/47111111/dolphins-preview]
  Dolphins open the 2026 season hosting Buffalo on opening Sunday.
- link "Dolphins roster moves" [ref=e6, url=https://www.profootballtalk.com/2026/07/dolphins-roster-moves/]
  Miami signed three undrafted free agents on Thursday, three receivers among them.
- link "Dolphins news hub" [ref=e7, url=https://www.miamiheroes.net/sports/dolphins/]
  Breaking news and analysis from the Miami sports section, updated daily.
"""

GOOGLE_LINKS = [
    CamofoxLink(
        url="https://www.espn.com/nfl/story/_/id/47111111/dolphins-preview",
        text="Dolphins 2026 season preview",
    ),
    CamofoxLink(
        url="https://www.profootballtalk.com/2026/07/dolphins-roster-moves/",
        text="Dolphins roster moves - ProFootballTalk",
    ),
    CamofoxLink(url="https://www.miamiheroes.net/sports/dolphins/", text="Dolphins news hub"),
    CamofoxLink(url="https://www.espn.com/nfl/story/_/id/47111111/dolphins-preview", text="ESPN"),
    CamofoxLink(url="/search?q=other", text="relative link must be dropped"),
]

REDDIT_SNAPSHOT = """
- search "Miami Dolphins" [ref=e1, url=https://www.reddit.com/search/?q=Miami+Dolphins]
- link "r/dolphins: training camp expectations" [ref=e3, url=https://www.reddit.com/r/dolphins/comments/abc123/training-camp/]
  What are your realistic expectations for the 2026 season overall?
- link "r/nfl: Dolphins win or bust?" [ref=e4, url=https://www.reddit.com/r/nfl/comments/def456/win-or-bust/]
  Everyone is talking about the brand new offense this offseason.
"""

REDDIT_LINKS = [
    CamofoxLink(
        url="https://www.reddit.com/r/dolphins/comments/abc123/training-camp/",
        text="r/dolphins: Dolphins training camp expectations",
    ),
    CamofoxLink(
        url="https://www.reddit.com/r/nfl/comments/def456/win-or-bust/",
        text="r/nfl: Dolphins win or bust?",
    ),
]

YOUTUBE_SNAPSHOT = """
- search "Miami Dolphins highlights" [ref=e1, url=https://www.youtube.com/results?search_query=highlights]
- link "Dolphins @ Chiefs highlights" [ref=e3, url=https://www.youtube.com/watch?v=abc123def45]
  Full game highlights from the week one opener in Kansas City.
- link "Dolphins training camp drills" [ref=e4, url=https://youtu.be/xyz789ghi01]
"""

YOUTUBE_LINKS = [
    CamofoxLink(
        url="https://www.youtube.com/watch?v=abc123def45", text="Dolphins @ Chiefs highlights"
    ),
    CamofoxLink(url="https://youtu.be/xyz789ghi01", text="Dolphins training camp drills"),
]


class FakeCamofoxClient:  # noqa: S101 -- deliberate structural fake, not a real client
    """A hand-rolled stand-in for :class:`app.camofox.client.CamofoxClient`.

    Implements the *exact* subset of the client surface the provider uses
    (``config``, ``create_session``, ``probe_health``, ``close``) plus the
    session-level operations the session facade delegates to
    (``create_tab``, ``navigate``, ``get_links``, ``snapshot``,
    ``close_tab``, ``close_session``) so the *real*
    :class:`CamofoxSession` (with its real tab-registry bookkeeping) runs
    against it.  Every operation returns the canned value this client was
    configured with, or raises the canned exception.  No HTTP is ever made.
    """

    def __init__(
        self,
        *,
        health: CamofoxHealthStatus | Exception | None = None,
        navigate: CamofoxNavigateResult | Exception | None = None,
        links: CamofoxLinks | Exception | None = None,
        snapshot: CamofoxSnapshot | Exception | None = None,
        create_tab: Exception | None = None,
        close_tab: Exception | None = None,
        close_session: Exception | None = None,
    ) -> None:
        self.config = CLIENT_CONFIG
        self.health = health
        self.navigate_val = navigate
        self.links_val = links
        self.snapshot_val = snapshot
        self.create_tab_exc = create_tab
        self.close_tab_exc = close_tab
        self.close_session_exc = close_session
        self.sessions: list[CamofoxSession] = []
        self.closed = False
        self._lock = asyncio.Lock()
        self._self = None
        self._self = self

    # -- provider surface ----------------------------------------------------
    def create_session(self, *args: Any, **kwargs: Any) -> CamofoxSession:
        session = CamofoxSession(self, user_id=f"scrapping-service-{uuid.uuid4().hex[:12]}")
        _instrument_session(session)
        self.sessions.append(session)
        return session

    @property
    def client(self) -> "FakeCamofoxClient":
        """The real client exposes ``.client``; the session facade stores it."""
        return self

    async def probe_health(self) -> CamofoxHealthStatus:
        if isinstance(self.health, Exception):
            raise self.health
        if self.health is not None:
            return self.health
        return HEALTH_OK

    async def close(self) -> None:
        self.closed = True

    # -- session-level operations (the session facade delegates here) --------
    def _run(self, value: Any) -> Any:
        if isinstance(value, Exception):
            raise value
        return value

    async def create_tab(self, session: CamofoxSession, url: str | None = None) -> CamofoxTab:
        self._run(self.create_tab_exc)
        session.tabs_created += 1  # type: ignore[attr-defined]
        tab = CamofoxTab(tab_id=f"tab-{session.user_id}-{session.tabs_created}", url=url)  # type: ignore[attr-defined]
        session.add_tab(tab)
        return tab

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
        session.last_navigate = {"macro": macro, "engine": engine, "query": query, "url": url}  # type: ignore[attr-defined]
        self._last_navigate = {"macro": macro, "engine": engine, "query": query, "url": url}
        return self._run(self.navigate_val)

    async def get_links(
        self, session: CamofoxSession, tab: CamofoxTab, *, limit: int = 50, offset: int = 0
    ) -> CamofoxLinks:
        self.last_links = {"limit": limit, "offset": offset}
        return self._run(self.links_val)

    async def snapshot(
        self, session: CamofoxSession, tab: CamofoxTab, offset: int = 0
    ) -> CamofoxSnapshot:
        self.last_snapshot = {"offset": offset}
        return self._run(self.snapshot_val)

    async def close_tab(self, session: CamofoxSession, tab: CamofoxTab) -> None:
        session.tabs_closed.append(tab.tab_id)  # type: ignore[attr-defined]
        session.forget_tab(tab.tab_id)
        self._run(self.close_tab_exc)

    async def close_session(self, session: CamofoxSession) -> None:
        session.session_closed = True  # type: ignore[attr-defined]
        session.forget_all_tabs()
        self._run(self.close_session_exc)

    # -- session-facade surface (never used: the real facade delegates to the
    #    client-level methods above; present only to complete the protocol) --
    async def list_tabs(self, session: CamofoxSession) -> list[CamofoxTabInfo]:  # pragma: no cover
        return []

    async def click(  # pragma: no cover - not used
        self, session: CamofoxSession, tab: CamofoxTab, **kwargs: Any
    ) -> CamofoxActionResult:
        return CamofoxActionResult(ok=True)

    async def type_text(  # pragma: no cover - not used
        self, session: CamofoxSession, tab: CamofoxTab, text: str, **kwargs: Any
    ) -> CamofoxActionResult:
        return CamofoxActionResult(ok=True)

    async def evaluate(  # pragma: no cover - not used
        self, session: CamofoxSession, tab: CamofoxTab, expression: str, **kwargs: Any
    ) -> CamofoxEvaluateResult:
        return CamofoxEvaluateResult(ok=True)

    async def import_cookies(  # pragma: no cover - not used
        self, session: CamofoxSession, cookies: list[CamofoxCookie], **kwargs: Any
    ) -> int:
        return len(cookies)


def _instrument_session(session: CamofoxSession) -> None:
    """Give a fake-backed session the lifecycle counters the tests assert on."""
    session.tabs_created = 0  # type: ignore[attr-defined]
    session.tabs_closed = []  # type: ignore[attr-defined]
    session.session_closed = False  # type: ignore[attr-defined]


def _default_client() -> FakeCamofoxClient:
    return FakeCamofoxClient(**google_page())


def make_provider(
    client: FakeCamofoxClient | None = None,
    **config_overrides: Any,
) -> CamofoxSearchProvider:
    """Build the provider with a fake client (and optionally patched config)."""
    if client is None:
        client = FakeCamofoxClient()
    config = config_overrides or {}
    return CamofoxSearchProvider(CamofoxSearchConfig(**config), client=client)  # type: ignore[arg-type]


def google_page() -> dict[str, Any]:
    return dict(
        navigate=CamofoxNavigateResult(
            ok=True, url="https://www.google.com/search?q=Miami+Dolphins", title="Google"
        ),
        links=CamofoxLinks(links=GOOGLE_LINKS, total=len(GOOGLE_LINKS), limit=100),
        snapshot=CamofoxSnapshot(
            url="https://www.google.com/search?q=Miami+Dolphins",
            snapshot=GOOGLE_SNAPSHOT,
            refs_count=7,
            total_chars=len(GOOGLE_SNAPSHOT),
        ),
    )


def reddit_page() -> dict[str, Any]:
    return dict(
        navigate=CamofoxNavigateResult(
            ok=True, url="https://www.reddit.com/search/?q=Miami+Dolphins", title="Reddit"
        ),
        links=CamofoxLinks(links=REDDIT_LINKS, total=len(REDDIT_LINKS), limit=100),
        snapshot=CamofoxSnapshot(
            url="https://www.reddit.com/search/?q=Miami+Dolphins",
            snapshot=REDDIT_SNAPSHOT,
            refs_count=4,
            total_chars=len(REDDIT_SNAPSHOT),
        ),
    )


def youtube_page() -> dict[str, Any]:
    return dict(
        navigate=CamofoxNavigateResult(
            ok=True, url="https://www.youtube.com/results?search_query=highlights", title="YouTube"
        ),
        links=CamofoxLinks(links=YOUTUBE_LINKS, total=len(YOUTUBE_LINKS), limit=100),
        snapshot=CamofoxSnapshot(
            url="https://www.youtube.com/results?search_query=highlights",
            snapshot=YOUTUBE_SNAPSHOT,
            refs_count=4,
            total_chars=len(YOUTUBE_SNAPSHOT),
        ),
    )


# ================================================================== tests
class TestProviderContract:
    def test_satisfies_structural_contract(self):
        provider = make_provider()
        assert isinstance(provider, SearchProvider)
        assert provider.name == "camofox"

    def test_requires_client(self):
        with pytest.raises(ValueError, match="CamofoxClient"):
            CamofoxSearchProvider(CamofoxSearchConfig(), client=None)  # type: ignore[arg-type]

    async def test_close_closes_shared_client(self):
        client = FakeCamofoxClient()
        provider = make_provider(client)
        assert client.closed is False
        await provider.close()
        assert client.closed is True
        # Idempotent.
        await provider.close()

    def test_diagnostics_shape(self):
        provider = make_provider()
        diagnostics = provider.diagnostics()
        assert diagnostics["base_url_configured"] is True
        assert diagnostics["last_health"] is None


class TestCapabilities:
    async def test_healthy_capabilities(self):
        provider = make_provider(FakeCamofoxClient(health=HEALTH_OK))
        caps = await provider.capabilities()
        assert caps.available is True
        assert set(caps.verticals) == {Vertical.WEB, Vertical.SOCIAL, Vertical.VIDEOS}
        assert caps.supports_source_selection is True
        assert set(caps.sources) == {"google", "reddit", "youtube"}
        assert caps.supports_language is False
        assert caps.supports_safe_search is False
        assert caps.supports_paging is False
        assert caps.time_ranges == []
        assert caps.max_results is None

    async def test_max_results_advertised(self):
        provider = make_provider(max_results=20)
        caps = await provider.capabilities()
        assert caps.max_results == 20

    async def test_unavailable_when_health_down(self):
        provider = make_provider(FakeCamofoxClient(health=HEALTH_DOWN))
        caps = await provider.capabilities()
        assert caps.available is False

    async def test_probe_failure_is_not_raised(self):
        provider = make_provider(FakeCamofoxClient(health=CamofoxError("boom", details={})))
        caps = await provider.capabilities()
        assert caps.available is False

    async def test_healthy_probe_not_repeated(self):
        client = FakeCamofoxClient(health=HEALTH_OK)
        calls = 0
        original = client.probe_health

        async def counting() -> CamofoxHealthStatus:
            nonlocal calls
            calls += 1
            return await original()

        client.probe_health = counting  # type: ignore[method-assign]
        provider = make_provider(client)
        await provider.capabilities()
        await provider.capabilities()
        assert calls == 1

    async def test_caps_degradations_match_honest_limits(self):
        """capabilities().degradations() agrees with what search() reports."""
        provider = make_provider()
        request = SearchQuery(
            query="q",
            verticals=[Vertical.NEWS],
            language="en",
            time_range=TimeRange.WEEK,
            page=2,
        )
        caps = await provider.capabilities()
        degraded = caps.degradations(request)
        assert "vertical:news" in degraded
        assert "language" in degraded
        assert "time_range" in degraded
        assert "paging" in degraded
        assert "safe_search" in degraded  # default is MODERATE

    async def test_caps_degradations_for_unsupported_request(self):
        caps = await make_provider().capabilities()
        request = SearchQuery(
            query="q",
            verticals=[Vertical.NEWS],
            language="en",
            time_range=TimeRange.WEEK,
            page=2,
        )
        degraded = caps.degradations(request)
        assert "vertical:news" in degraded
        assert "language" in degraded
        assert "time_range" in degraded
        assert "paging" in degraded
        assert "safe_search" in degraded  # default is MODERATE


class TestSourceMapping:
    async def test_explicit_reddit_source_uses_reddit_macro(self):
        client = FakeCamofoxClient(**reddit_page())
        provider = make_provider(client)
        request = SearchQuery(query="Miami Dolphins", sources=["reddit"], provider="camofox")
        response = await provider.search(request)
        session = client.sessions[0]
        assert session.last_navigate["macro"] == "@reddit_search"
        assert session.last_navigate["query"] == "Miami Dolphins"
        assert all(result.source == "reddit" for result in response.results)
        assert all(result.vertical == Vertical.SOCIAL for result in response.results)

    @pytest.mark.parametrize(
        "source,macro,vertical",
        [
            ("google", "@google_search", Vertical.WEB),
            ("reddit", "@reddit_search", Vertical.SOCIAL),
            ("youtube", "@youtube_search", Vertical.VIDEOS),
        ],
    )
    async def test_source_macro_table(self, source: str, macro: str, vertical: Vertical):
        assert SOURCE_TO_MACRO[source] == macro
        assert normalize_source_name(source) == source
        # Each macro resolves to the expected vertical in the provider.
        provider = make_provider()
        resolved_macro, resolved_source, resolved_vertical = provider._resolve_macro(
            SearchQuery(query="q", sources=[source])
        )
        assert resolved_macro == macro
        assert resolved_source == source
        assert resolved_vertical == vertical

    async def test_source_normalization_tolerates_variants(self):
        provider = make_provider()
        variants = ("Reddit", "@reddit", "reddit_search")
        for variant in variants:
            macro, _source, _vertical = provider._resolve_macro(
                SearchQuery(query="q", sources=[variant])
            )
            assert macro == "@reddit_search"
        assert normalize_source_name("@reddit") == "reddit"
        macro, _source, _vertical = provider._resolve_macro(
            SearchQuery(query="q", sources=["@REDDIT".upper()])
        )
        assert macro == "@reddit_search"

    async def test_unknown_source_falls_back_to_web_and_degrades(self):
        client = _default_client()
        provider = make_provider(client)
        request = SearchQuery(query="Miami Dolphins", sources=["bing"], provider="camofox")
        response = await provider.search(request)
        session = client.sessions[0]
        assert session.last_navigate["macro"] == "@google_search"
        assert "sources" in response.metadata.degraded_capabilities
        assert any("source" in warning for warning in response.metadata.warnings)

    async def test_first_known_source_wins(self):
        client = FakeCamofoxClient(**reddit_page())
        provider = make_provider(client)
        request = SearchQuery(query="q", sources=["youtube", "reddit"], provider="camofox")
        await provider.search(request)
        assert client.sessions[0].last_navigate["macro"] == "@youtube_search"


class TestVerticalMapping:
    @pytest.mark.parametrize(
        "vertical,macro,source",
        [
            (Vertical.WEB, "@google_search", "google"),
            (Vertical.SOCIAL, "@reddit_search", "reddit"),
            (Vertical.VIDEOS, "@youtube_search", "youtube"),
        ],
    )
    async def test_verticals_map_to_macros(self, vertical: Vertical, macro: str, source: str):
        provider = make_provider()
        resolved_macro, resolved_source, resolved_vertical = provider._resolve_macro(
            SearchQuery(query="q", verticals=[vertical])
        )
        assert resolved_macro == macro
        assert resolved_source == source
        assert resolved_vertical == vertical

    async def test_source_takes_precedence_over_verticals(self):
        provider = make_provider()
        macro, source, vertical = provider._resolve_macro(
            SearchQuery(
                query="q", verticals=[Vertical.VIDEOS], sources=["reddit"], provider="camofox"
            )
        )
        assert macro == "@reddit_search"
        assert vertical == Vertical.SOCIAL

    async def test_unmapped_verticals_degrade_and_fall_back_to_web(self):
        client = _default_client()
        provider = make_provider(client)
        request = SearchQuery(
            query="q", verticals=[Vertical.NEWS, Vertical.MAPS], provider="camofox"
        )
        response = await provider.search(request)
        session = client.sessions[0]
        assert session.last_navigate["macro"] == "@google_search"
        degraded = response.metadata.degraded_capabilities
        assert "vertical:news" in degraded
        assert "vertical:maps" in degraded

    async def test_multiple_mapped_verticals_use_the_first(self):
        client = FakeCamofoxClient(**youtube_page())
        provider = make_provider(client)
        request = SearchQuery(
            query="Miami Dolphins highlights", verticals=[Vertical.WEB, Vertical.VIDEOS]
        )
        response = await provider.search(request)
        assert client.sessions[0].last_navigate["macro"] == "@google_search"
        assert response.verticals == [Vertical.WEB]
        assert all(result.vertical == Vertical.WEB for result in response.results)


class TestResultNormalization:
    async def test_google_search_normalizes_results(self):
        client = _default_client()
        provider = make_provider(client)
        request = SearchQuery(query="Miami Dolphins", provider="camofox")
        response = await provider.search(request)

        assert response.metadata.provider == "camofox"
        assert response.metadata.providers_attempted == ["camofox"]
        assert response.query == "Miami Dolphins"
        assert response.verticals == [Vertical.WEB]

        urls = [result.url for result in response.results]
        # Engine chrome (mail, about/relative) is filtered out; duplicates deduped.
        assert urls == [
            "https://www.espn.com/nfl/story/_/id/47111111/dolphins-preview",
            "https://www.profootballtalk.com/2026/07/dolphins-roster-moves/",
            "https://www.miamiheroes.net/sports/dolphins/",
        ]
        first, second, third = response.results
        assert (first.rank, second.rank, third.rank) == (1, 2, 3)
        assert first.title == "Dolphins 2026 season preview"
        assert first.snippet == "Dolphins open the 2026 season hosting Buffalo on opening Sunday."
        assert first.source is None  # web search: no explicit source requested
        assert first.provider == "camofox"
        assert first.vertical == Vertical.WEB
        assert second.snippet == (
            "Miami signed three undrafted free agents on Thursday, three receivers among them."
        )
        # A link with no snapshot title falls back to the page-link text; the
        # third result *does* carry a snippet in this canned page.
        assert third.title == "Dolphins news hub"
        assert third.snippet == (
            "Breaking news and analysis from the Miami sports section, updated daily."
        )

    async def test_source_driven_search_tags_results_with_the_source(self):
        """When the caller asks for `google` explicitly, results are tagged google."""
        client = _default_client()
        provider = make_provider(client)
        response = await provider.search(
            SearchQuery(query="q", sources=["google"], provider="camofox")
        )
        assert all(result.source == "google" for result in response.results)

    async def test_reddit_search_normalizes_results(self):
        client = FakeCamofoxClient(**reddit_page())
        provider = make_provider(client)
        request = SearchQuery(
            query="Miami Dolphins", sources=["reddit"], verticals=[Vertical.SOCIAL]
        )
        response = await provider.search(request)
        urls = [result.url for result in response.results]
        assert urls == [
            "https://www.reddit.com/r/dolphins/comments/abc123/training-camp/",
            "https://www.reddit.com/r/nfl/comments/def456/win-or-bust/",
        ]
        first, second = response.results
        assert first.vertical == Vertical.SOCIAL
        assert first.source == "reddit"
        assert first.snippet == "What are your realistic expectations for the 2026 season overall?"
        assert second.snippet == "Everyone is talking about the brand new offense this offseason."

    async def test_youtube_search_normalizes_results(self):
        client = FakeCamofoxClient(**youtube_page())
        provider = make_provider(client)
        request = SearchQuery(
            query="Miami Dolphins highlights",
            verticals=[Vertical.VIDEOS],
            provider="camofox",
        )
        response = await provider.search(request)
        urls = [result.url for result in response.results]
        assert urls == [
            "https://www.youtube.com/watch?v=abc123def45",
            "https://youtu.be/xyz789ghi01",
        ]
        first, second = response.results
        assert first.vertical == Vertical.VIDEOS
        # vertical-driven: the caller asked for a vertical, not a source
        assert first.source is None
        assert first.snippet == ("Full game highlights from the week one opener in Kansas City.")
        assert second.title == "Dolphins training camp drills"
        assert second.snippet is None

    async def test_empty_search_returns_empty_results_not_error(self):
        page = google_page()
        page["links"] = CamofoxLinks(links=[], total=0, limit=100)
        client = FakeCamofoxClient(**page)
        provider = make_provider(client)
        response = await provider.search(SearchQuery(query="no such thing xyz"))
        assert response.results == []
        assert response.metadata.provider == "camofox"

    async def test_empty_results_with_zero_links_and_snapshot(self):
        page = google_page()
        page["links"] = CamofoxLinks(links=[], total=0)
        page["snapshot"] = CamofoxSnapshot(snapshot="", refs_count=0)
        client = FakeCamofoxClient(**page)
        provider = make_provider(client)
        response = await provider.search(SearchQuery(query="nothing"))
        assert response.results == []

    async def test_malformed_snapshot_and_links_do_not_raise(self):
        page = google_page()
        page["links"] = CamofoxLinks(
            links=[CamofoxLink(url="javascript:void(0)", text="script link")], total=1
        )
        page["snapshot"] = CamofoxSnapshot(snapshot="garbage-with-no-refs")
        client = FakeCamofoxClient(**page)
        provider = make_provider(client)
        response = await provider.search(SearchQuery(query="q"))
        assert response.results == []  # unusable links simply yield nothing

    async def test_results_without_usable_titles_are_dropped(self):
        links = [
            CamofoxLink(url="https://www.reddit.com/r/x/short", text="/"),
            CamofoxLink(url="https://www.reddit.com/r/y/real", text="A real thread title"),
        ]
        snapshot = CamofoxSnapshot(
            snapshot=(
                "- link [ref=e1, url=https://www.reddit.com/r/x/short]\n"
                '- link "A real thread title" [ref=e2, url=https://www.reddit.com/r/y/real]\n'
            ),
            refs_count=2,
        )
        client = FakeCamofoxClient(
            navigate=CamofoxNavigateResult(ok=True, url="https://www.reddit.com/search/"),
            links=CamofoxLinks(links=links, total=2),
            snapshot=snapshot,
        )
        provider = make_provider(client)
        response = await provider.search(SearchQuery(query="q", sources=["reddit"]))
        assert [result.url for result in response.results] == ["https://www.reddit.com/r/y/real"]
        assert response.results[0].rank == 1

    async def test_limit_truncates_results(self):
        client = _default_client()
        provider = make_provider(client)
        response = await provider.search(
            SearchQuery(query="Miami Dolphins", limit=2, provider="camofox")
        )
        assert len(response.results) == 2
        assert response.results[0].rank == 1
        assert response.results[1].rank == 2
        assert "limit" in response.metadata.degraded_capabilities

    async def test_max_results_cap_degrades(self):
        client = _default_client()
        provider = make_provider(client, max_results=1)
        caps = await provider.capabilities()
        assert caps.max_results == 1
        degraded = caps.degradations(SearchQuery(query="q", limit=10))
        assert "limit" in degraded


class TestTabCleanup:
    async def test_tab_and_session_closed_on_success(self):
        client = _default_client()
        provider = make_provider(client)
        await provider.search(SearchQuery(query="Miami Dolphins"))
        session = client.sessions[0]
        assert session.tabs_created == 1
        assert len(session.tabs_closed) == 1
        assert session.session_closed is True
        assert session.tabs == ()  # registry drained
        assert provider._session is None

    async def test_tab_and_session_closed_on_navigation_error(self):
        page = google_page()
        page["navigate"] = CamofoxError("navigation blew up", details={"status_code": 500})
        client = FakeCamofoxClient(**page)
        provider = make_provider(client)
        with pytest.raises(SearchProviderUnavailableError):
            await provider.search(SearchQuery(query="Miami Dolphins"))
        session = client.sessions[0]
        assert session.tabs_created == 1
        assert len(session.tabs_closed) == 1
        assert session.session_closed is True
        assert provider._session is None

    async def test_tab_closed_when_create_tab_fails(self):
        # create_tab raises: no tab exists, but the session must still be released.
        page = google_page()
        page["create_tab"] = CamofoxError("browser start failed", details={})
        client = FakeCamofoxClient(**page)
        provider = make_provider(client)
        with pytest.raises(SearchProviderUnavailableError):
            await provider.search(SearchQuery(query="Miami Dolphins"))
        session = client.sessions[0]
        assert session.tabs_created == 0
        assert session.tabs_closed == []
        assert session.session_closed is True
        assert provider._session is None

    async def test_tab_closed_when_navigation_reports_not_ok(self):
        page = google_page()
        page["navigate"] = CamofoxNavigateResult(ok=False, url=None, title=None)
        client = FakeCamofoxClient(**page)
        provider = make_provider(client)
        with pytest.raises(SearchProviderUnavailableError):
            await provider.search(SearchQuery(query="Miami Dolphins"))
        session = client.sessions[0]
        assert len(session.tabs_closed) == 1
        assert session.session_closed is True

    async def test_cleanup_failure_never_masks_search_result(self):
        page = google_page()
        page["close_tab"] = CamofoxError("close failed", details={})
        page["close_session"] = CamofoxError("close session failed", details={})
        client = FakeCamofoxClient(**page)
        provider = make_provider(client)
        response = await provider.search(SearchQuery(query="Miami Dolphins"))
        assert len(response.results) == 3  # the search outcome is intact
        session = client.sessions[0]
        assert session.session_closed is True  # the provider attempted it
        assert session.forget_all_tabs.__self__.tabs == ()  # registry still drained

    async def test_tab_not_closed_when_create_fails_but_no_tab(self):
        page = google_page()
        page["navigate"] = CamofoxError("nav", details={})
        client = FakeCamofoxClient(**page)
        # Sanity: with a tab created and a nav error, cleanup runs on the tab.
        provider = make_provider(client)
        with pytest.raises(SearchProviderUnavailableError):
            await provider.search(SearchQuery(query="x"))
        assert len(client.sessions[0].tabs_closed) == 1


class TestUnsupportedCapabilities:
    async def test_unsupported_filters_reported_as_degraded(self):
        client = _default_client()
        provider = make_provider(client)
        request = SearchQuery(
            query="q",
            language="en",
            time_range=TimeRange.WEEK,
            safe_search=SafeSearch.STRICT,
            page=3,
            provider="camofox",
        )
        response = await provider.search(request)
        degraded = response.metadata.degraded_capabilities
        assert "language" in degraded
        assert "time_range" in degraded
        assert "safe_search" in degraded
        assert "paging" in degraded
        assert any("time range" in w for w in response.metadata.warnings)
        assert any("language" in w for w in response.metadata.warnings)
        assert any("safe-search" in w for w in response.metadata.warnings)
        assert any("page 2+" in w for w in response.metadata.warnings)

    async def test_safe_search_off_is_honored_not_degraded(self):
        client = _default_client()
        provider = make_provider(client)
        request = SearchQuery(query="q", safe_search=SafeSearch.OFF, provider="camofox")
        response = await provider.search(request)
        assert "safe_search" not in response.metadata.degraded_capabilities


class TestFailureMapping:
    @pytest.mark.parametrize(
        ("name", "exc_class"),
        [
            ("navigate", SearchProviderUnavailableError),
            ("links", SearchProviderUnavailableError),
            ("snapshot", SearchProviderUnavailableError),
        ],
    )
    async def test_camofox_error_maps_to_search_unavailable(self, name: str, exc_class: type):
        page = google_page()
        page[name] = CamofoxError("x", details={})
        client = FakeCamofoxClient(**page)
        provider = make_provider(client)
        with pytest.raises(exc_class):
            await provider.search(SearchQuery(query="q"))

    def test_error_mapping_table(self):
        from app.core.errors import (
            CamofoxAuthError,
            CamofoxInvalidResponseError,
            CamofoxNavigationError,
            CamofoxSessionExpiredError,
            CamofoxTabNotFoundError,
            CamofoxTimeoutError,
            CamofoxUnavailableError,
        )
        from app.search.providers.camofox.provider import _map_camofox_error

        assert isinstance(
            _map_camofox_error(CamofoxTimeoutError("t", details={})), SearchProviderTimeoutError
        )
        assert isinstance(
            _map_camofox_error(CamofoxUnavailableError("u", details={})),
            SearchProviderUnavailableError,
        )
        assert isinstance(
            _map_camofox_error(CamofoxAuthError("a", details={})), SearchProviderUnavailableError
        )
        assert isinstance(
            _map_camofox_error(CamofoxTabNotFoundError("t", details={})),
            SearchProviderUnavailableError,
        )
        assert isinstance(
            _map_camofox_error(CamofoxSessionExpiredError("s", details={})),
            SearchProviderUnavailableError,
        )
        assert isinstance(
            _map_camofox_error(CamofoxNavigationError("n", details={})),
            SearchProviderUnavailableError,
        )
        # The remaining Camofox error types are all *transient* browser
        # failures → search provider unavailable (so auto-mode can fall
        # back to another provider), matching the SearXNG provider's
        # transient/terminal semantics.
        for exc in (
            CamofoxInvalidResponseError("i", details={}),
            CamofoxError("generic", details={}),
        ):
            assert isinstance(_map_camofox_error(exc), SearchProviderUnavailableError)

    async def test_search_timeout_is_search_timeout(self):
        client = FakeCamofoxClient(navigate=CamofoxTimeoutError("Camofox search timed out"))
        provider = make_provider(client)
        with pytest.raises(SearchProviderTimeoutError):
            await provider.search(SearchQuery(query="q"))


# ================================================================== mapping helpers
class TestSnapshotParsing:
    def test_parses_links_with_and_without_attributes(self):
        links = _parse_snapshot_links(GOOGLE_SNAPSHOT)
        by_ref = {link.ref: link for link in links}
        assert by_ref["e4"].text == "Dolphins 2026 season preview"
        assert by_ref["e4"].url == "https://www.espn.com/nfl/story/_/id/47111111/dolphins-preview"
        assert by_ref["e6"].url == "https://www.profootballtalk.com/2026/07/dolphins-roster-moves/"
        assert by_ref["e7"].text == "Dolphins news hub"

    def test_link_without_url_has_empty_url(self):
        links = _parse_snapshot_links('- link "No target" [ref=e9]')
        assert links == [_parse_snapshot_links('- link "No target" [ref=e9]')[0]]
        assert links[0].ref == "e9"
        assert links[0].url is None
        assert links[0].text == "No target"

    def test_relative_url_dropped(self):
        links = _parse_snapshot_links('- link "x" [ref=e1, url=/relative/path]')
        assert links[0].url is None

    def test_collect_snippets_bounds_at_next_link(self):
        text = (
            '- link "One" [ref=e1, url=https://a.example/one]\n'
            "  Snippet one text.\n"
            "- chrome\n"
            '- link "Two" [ref=e2, url=https://a.example/two]\n'
            "  Snippet two text.\n"
            "  Tail after two.\n"
        )
        snippets = _collect_snippets(text, ["e1", "e2"])
        assert snippets["e1"] == "Snippet one text."
        assert snippets["e2"] == "Snippet two text."

    def test_collect_snippets_last_link_no_snippet(self):
        text = '- link "Only" [ref=e1, url=https://a.example/only]\n'
        assert _collect_snippets(text, ["e1"]) == {}

    def test_collect_snippets_skips_rank_badge_and_dates(self):
        text = (
            '- link "One" [ref=e1, url=https://a.example/one]\n'
            "  2 days ago\n"
            "  The real snippet sentence lives here.\n"
        )
        snippets = _collect_snippets(text, ["e1"])
        assert snippets["e1"] == "The real snippet sentence lives here."

    def test_host_matching(self):
        assert _host_matches("reddit.com", ("reddit.com",)) is True
        assert _host_matches("www.reddit.com", ("reddit.com",)) is True
        assert _host_matches("old.reddit.com", ("reddit.com",)) is True
        assert _host_matches("www.youtube.com", ("youtube.com", "youtu.be")) is True
        assert _host_matches("youtu.be", ("youtube.com", "youtu.be")) is True
        assert _host_matches("notreddit.com", ("reddit.com",)) is False
        assert _host_matches("evil-reddit.com", ("reddit.com",)) is False
        # An empty host tuple means "no filtering" (used for Google web results).
        assert _host_matches("www.espn.com", ()) is True

    def test_host_of(self):
        assert _host_of("https://www.reddit.com/r/x") == "reddit.com"
        assert _host_of("https://YouTube.Com/results?x=1") == "youtube.com"
        assert _host_of("not-a-url") is None
        assert _host_of("ftp://x.example/") is None
        assert _host_of("https://host:8080/path") == "host"
