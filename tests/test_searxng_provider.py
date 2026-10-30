"""Tests for the SearXNG :class:`SearchProvider` implementation.

All SearXNG HTTP traffic is mocked with ``httpx.MockTransport`` — no live
SearXNG instance (public or otherwise) is ever contacted, as required by the
ticket.  The mock mimics the instance's real ``/config`` and ``/search``
(JSON) endpoints.
"""

import asyncio
import json
from datetime import datetime

import httpx
import pytest

from app.core.errors import (
    InvalidProviderResponseError,
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
)
from app.search import SearchProvider, SearchQuery
from app.search.models import SafeSearch, TimeRange, Vertical
from app.search.providers.searxng import SearxngConfig, SearxngProvider
from app.search.providers.searxng.provider import _normalize_result

BASE_URL = "http://searxng.test"


# ==================================================================== fixtures
# Mimics a SearXNG instance with:
#   - google:       general + news, enabled, time-range + paging support
#   - duckduckgo:   general, enabled
#   - brave:        general, DISABLED (only reachable as a disabled engine)
#   - openstreetmap: map, enabled, no time-range support, no paging
#
# Therefore the instance's enabled categories are {general, news, map}.
CONFIG_PAYLOAD: dict = {
    "categories": ["general", "news", "images", "videos", "map"],
    "engines": [
        {
            "name": "google",
            "categories": ["general", "news"],
            "shortcut": "g",
            "enabled": True,
            "paging": True,
            "language_support": True,
            "safesearch": 2,
            "time_range_support": True,
        },
        {
            "name": "duckduckgo",
            "categories": ["general"],
            "shortcut": "ddg",
            "enabled": True,
            "paging": True,
            "language_support": True,
            "safesearch": 1,
            "time_range_support": False,
        },
        {
            "name": "brave",
            "categories": ["general"],
            "shortcut": "brave",
            "enabled": False,
            "paging": True,
            "language_support": True,
            "safesearch": 2,
            "time_range_support": True,
        },
        {
            "name": "openstreetmap",
            "categories": ["map"],
            "shortcut": "osm",
            "enabled": True,
            "paging": False,
            "language_support": False,
            "safesearch": 0,
            "time_range_support": False,
        },
    ],
    "safe_search": 1,
    "instance_name": "test-instance",
    "version": "4.0.0",
    "default_locale": "en",
}

SEARCH_PAYLOAD: dict = {
    "query": "dolphins",
    "results": [
        {
            "url": "https://example.com/first",
            "title": "First result",
            "content": "First snippet",
            "engine": "google",
            "engines": ["google", "duckduckgo"],
            "category": "general",
            "img_src": "https://img.example/first.jpg",
            "publishedDate": "2025-06-01T10:30:00Z",
            "score": 1.5,
            "positions": [1],
        },
        {
            "url": "https://news.example/second",
            "title": "Second result",
            "content": "Second snippet",
            "engine": "google",
            "engines": ["google"],
            "category": "news",
            "publishedDate": None,
            "score": 0.7,
        },
        {
            "url": "https://maps.example/third",
            "title": "Third result",
            "content": "",
            "engine": "openstreetmap",
            "engines": ["openstreetmap"],
            "category": "map",
            "score": 2.2,
        },
    ],
    "answers": [],
    "corrections": [],
    "infoboxes": [],
    "suggestions": [],
    "unresponsive_engines": [["brave", "timeout"]],
    "number_of_results": 123,
}


class MockSearxng:
    """A fake SearXNG instance backing an injected ``httpx.AsyncClient``.

    Records every request so tests can assert on the translated SearXNG
    query parameters.  Responses are fully configurable.
    """

    def __init__(
        self,
        config_payload: dict | None = None,
        config_status: int = 200,
        config_body: bytes | None = None,
        config_content_type: str = "application/json",
        search_payload: dict | None = None,
        search_status: int = 200,
        search_body: bytes | None = None,
        search_content_type: str = "application/json",
        effect: Exception | None = None,
        search_effect: Exception | None = None,
    ) -> None:
        self._config_payload = config_payload if config_payload is not None else CONFIG_PAYLOAD
        self._config_status = config_status
        self._config_body = config_body
        self._config_content_type = config_content_type
        self._search_payload = search_payload if search_payload is not None else SEARCH_PAYLOAD
        self._search_status = search_status
        self._search_body = search_body
        self._search_content_type = search_content_type
        self._effect = effect
        self._search_effect = search_effect
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.AsyncClient:
        async def async_handle(request: httpx.Request) -> httpx.Response:
            # Yield to the event loop so concurrent provider calls
            # interleave (matching real async I/O behaviour).
            await asyncio.sleep(0)
            return self._handle(request)

        return httpx.AsyncClient(
            base_url=BASE_URL,
            transport=httpx.MockTransport(async_handle),
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._effect is not None:
            raise self._effect
        if request.url.path == "/config":
            if self._config_body is not None:
                return httpx.Response(
                    self._config_status,
                    content=self._config_body,
                    headers={"content-type": self._config_content_type},
                )
            if self._config_status != 200:
                return httpx.Response(self._config_status, content=b"config error")
            return self._json(self._config_payload)
        if request.url.path == "/search":
            if self._search_effect is not None:
                raise self._search_effect
            if self._search_body is not None:
                return httpx.Response(
                    self._search_status,
                    content=self._search_body,
                    headers={"content-type": self._search_content_type},
                )
            if self._search_status != 200:
                return httpx.Response(self._search_status, content=b"search error")
            return self._json(self._search_payload)
        raise AssertionError(f"unexpected request path: {request.url.path}")

    @staticmethod
    def _json(payload: dict) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )

    # ------------------------------------------------------------- assertions

    def config_request_count(self) -> int:
        return sum(1 for r in self.requests if r.url.path == "/config")

    def search_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/search"]

    def search_params(self) -> dict:
        assert self.search_requests(), "no /search request was made"
        last = self.search_requests()[-1]
        return dict(last.url.params)


@pytest.fixture
def make_provider():
    """Build a SearxngProvider wired to a fresh MockSearxng instance."""

    def _make(
        config: SearxngConfig | None = None,
        **mock_kwargs,
    ) -> tuple[SearxngProvider, MockSearxng]:
        mock = MockSearxng(**mock_kwargs)
        provider = SearxngProvider(
            config or SearxngConfig(enabled=True, base_url=BASE_URL),
            client=mock.client(),
        )
        return provider, mock

    return _make


def _full_query(**overrides) -> SearchQuery:
    defaults = dict(
        query="dolphins",
        verticals=["web", "news"],
        language="en-US",
        time_range="week",
        safe_search=SafeSearch.MODERATE,
        page=2,
    )
    defaults.update(overrides)
    return SearchQuery(**defaults)


# ================================================================== contract
class TestContract:
    def test_name(self, make_provider):
        provider, _ = make_provider()
        assert provider.name == "searxng"

    def test_satisfies_protocol(self, make_provider):
        provider, _ = make_provider()
        assert isinstance(provider, SearchProvider)

    async def test_close_with_injected_client_is_safe(self, make_provider):
        provider, _ = make_provider()
        await provider.close()  # must not close the injected client


# ================================================================== concurrency
class TestDiscoveryConcurrency:
    async def test_concurrent_searches_share_one_discovery(self, make_provider):
        # Both searches start before either discovery completes, so the
        # second must hit the under-lock re-check instead of re-fetching.
        provider, mock = make_provider()
        provider._instance = None
        provider._instance_fetched_at = 0.0
        responses = await asyncio.gather(
            provider.search(_full_query()),
            provider.search(_full_query()),
        )
        assert all(r.results for r in responses)
        assert mock.config_request_count() == 1
        assert len(mock.search_requests()) == 2


# ================================================================== discovery
class TestConfigDiscovery:
    async def test_capabilities_from_config(self, make_provider):
        provider, _ = make_provider()
        caps = await provider.capabilities()
        # Derived from the enabled engines: general, news, map.
        assert caps.available is True
        assert caps.verticals == [Vertical.WEB, Vertical.NEWS, Vertical.MAPS]
        assert caps.time_ranges == list(TimeRange)
        assert caps.supports_language is True
        assert caps.supports_safe_search is True
        assert caps.supports_paging is True
        assert caps.supports_source_selection is True
        assert caps.max_results is None

    async def test_time_ranges_only_when_an_engine_supports_them(self, make_provider):
        config_payload = json.loads(json.dumps(CONFIG_PAYLOAD))
        for engine in config_payload["engines"]:
            engine["time_range_support"] = False
        provider, _ = make_provider(config_payload=config_payload)
        caps = await provider.capabilities()
        assert caps.time_ranges == []
        assert caps.supports_paging is False

    async def test_unreachable_instance_reports_unavailable(self, make_provider):
        provider, _ = make_provider(config_status=500)
        caps = await provider.capabilities()
        assert caps.available is False
        # The full static vertical mapping is still advertised.
        assert len(caps.verticals) == 10

    async def test_config_failure_surfaces_as_unavailable_error(self, make_provider):
        provider, _ = make_provider(config_status=500)
        with pytest.raises(SearchProviderUnavailableError) as excinfo:
            await provider.search(_full_query())
        assert excinfo.value.details["status_code"] == 500


# =================================================================== normal search
class TestNormalSearch:
    async def test_normal_web_search(self, make_provider):
        provider, mock = make_provider()
        response = await provider.search(_full_query())

        # Request translation.
        params = mock.search_params()
        assert params["q"] == "dolphins"
        assert params["format"] == "json"
        assert params["categories"] == "general,news"
        assert params["language"] == "en-US"
        assert params["time_range"] == "week"
        assert params["safesearch"] == "1"
        assert params["pageno"] == "2"

        # Response normalization.
        assert response.query == "dolphins"
        assert response.metadata.provider == "searxng"
        assert response.metadata.providers_attempted == ["searxng"]
        assert response.metadata.elapsed_ms >= 0
        assert response.metadata.page == 2
        assert response.metadata.total_results == 123
        assert response.metadata.degraded_capabilities == []
        assert any("unresponsive SearXNG engines: brave" in w for w in response.metadata.warnings)

        first = response.results[0]
        assert first.title == "First result"
        assert first.url == "https://example.com/first"
        assert first.snippet == "First snippet"
        assert first.rank == 1
        assert first.vertical == Vertical.WEB
        assert first.source == "google"
        assert first.provider == "searxng"
        assert first.thumbnail_url == "https://img.example/first.jpg"
        assert first.published_at == datetime(2025, 6, 1, 10, 30, tzinfo=first.published_at.tzinfo)

    async def test_score_is_not_mapped(self, make_provider):
        provider, _ = make_provider()
        response = await provider.search(_full_query())
        # Provider-specific scores must not leak as cross-provider scores.
        assert all(result.score is None for result in response.results)

    async def test_empty_results_are_valid(self, make_provider):
        payload = dict(SEARCH_PAYLOAD, results=[], number_of_results=0)
        provider, mock = make_provider(search_payload=payload)
        response = await provider.search(_full_query())
        assert response.results == []
        assert response.metadata.total_results == 0
        assert response.metadata.degraded_capabilities == []

    async def test_missing_number_of_results(self, make_provider):
        payload = dict(SEARCH_PAYLOAD)
        del payload["number_of_results"]
        provider, _ = make_provider(search_payload=payload)
        response = await provider.search(_full_query())
        assert response.metadata.total_results is None

    async def test_empty_snippet_becomes_none(self, make_provider):
        provider, _ = make_provider()
        response = await provider.search(_full_query())
        assert response.results[2].snippet is None

    async def test_response_verticals_echo_searched_verticals(self, make_provider):
        provider, _ = make_provider()
        response = await provider.search(_full_query(verticals=["web", "news"]))
        assert response.verticals == [Vertical.WEB, Vertical.NEWS]


# ================================================================== capability mapping
class TestCapabilityMapping:
    async def test_vertical_mapping_drops_disabled_categories(self, make_provider):
        # 'images' has no enabled engine on the mock instance.
        provider, mock = make_provider()
        response = await provider.search(_full_query(verticals=["web", "news", "images"]))
        params = mock.search_params()
        assert params["categories"] == "general,news"
        assert "vertical:images" in response.metadata.degraded_capabilities
        assert any("vertical 'images' skipped" in w for w in response.metadata.warnings)
        assert response.verticals == [Vertical.WEB, Vertical.NEWS]

    async def test_all_verticals_unsupported_falls_back_to_general(self, make_provider):
        provider, mock = make_provider()
        response = await provider.search(_full_query(verticals=["images"]))
        # No 'categories' parameter → SearXNG default (general/web).
        assert "categories" not in mock.search_params()
        assert response.verticals == [Vertical.WEB]
        assert "vertical:images" in response.metadata.degraded_capabilities
        assert response.results  # results still normalized, tagged web

    async def test_language_from_request(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query(language="pt-BR"))
        assert mock.search_params()["language"] == "pt-BR"

    async def test_default_language_from_config(self, make_provider):
        config = SearxngConfig(enabled=True, base_url=BASE_URL, default_language="en")
        provider, mock = make_provider(config=config)
        await provider.search(_full_query(language=None))
        assert mock.search_params()["language"] == "en"

    async def test_no_language_omits_parameter(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query(language=None))
        assert "language" not in mock.search_params()

    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (SafeSearch.OFF, "0"),
            (SafeSearch.MODERATE, "1"),
            (SafeSearch.STRICT, "2"),
        ],
    )
    async def test_safe_search_levels(self, make_provider, level, expected):
        provider, mock = make_provider()
        await provider.search(_full_query(safe_search=level))
        assert mock.search_params()["safesearch"] == expected

    @pytest.mark.parametrize(
        ("time_range", "expected"),
        [
            (TimeRange.DAY, "day"),
            (TimeRange.WEEK, "week"),
            (TimeRange.MONTH, "month"),
            (TimeRange.YEAR, "year"),
        ],
    )
    async def test_time_range_mapping(self, make_provider, time_range, expected):
        provider, mock = make_provider()
        await provider.search(_full_query(time_range=time_range))
        assert mock.search_params()["time_range"] == expected

    async def test_time_range_omitted_when_absent(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query(time_range=None))
        assert "time_range" not in mock.search_params()

    async def test_time_range_degraded_when_engine_cannot_filter(self, make_provider):
        # Only openstreetmap covers 'map' and it does not support time ranges.
        provider, mock = make_provider()
        response = await provider.search(_full_query(verticals=["maps"], time_range="week"))
        assert mock.search_params()["time_range"] == "week"
        assert "time_range" in response.metadata.degraded_capabilities
        assert any("time-range" in w for w in response.metadata.warnings)

    async def test_paging_parameter(self, make_provider):
        provider, mock = make_provider()
        response = await provider.search(_full_query(page=3))
        assert mock.search_params()["pageno"] == "3"
        assert response.metadata.page == 3

    async def test_source_selection_by_name(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query(sources=["google", "duckduckgo"]))
        assert mock.search_params()["engines"] == "google,duckduckgo"

    async def test_source_selection_by_shortcut(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query(sources=["ddg"]))
        assert mock.search_params()["engines"] == "duckduckgo"

    async def test_source_matching_is_case_insensitive(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query(sources=["Google"]))
        assert mock.search_params()["engines"] == "google"

    async def test_unknown_source_degraded(self, make_provider):
        provider, mock = make_provider()
        response = await provider.search(_full_query(sources=["google", "bing"]))
        assert mock.search_params()["engines"] == "google"
        assert "sources" in response.metadata.degraded_capabilities
        assert any("bing" in w for w in response.metadata.warnings)

    async def test_all_sources_unknown_omits_engines(self, make_provider):
        provider, mock = make_provider()
        response = await provider.search(_full_query(sources=["bing", "ask"]))
        assert "engines" not in mock.search_params()
        assert "sources" in response.metadata.degraded_capabilities

    async def test_limit_truncates_client_side(self, make_provider):
        provider, _ = make_provider()
        response = await provider.search(_full_query(limit=2))
        assert len(response.results) == 2
        assert [r.rank for r in response.results] == [1, 2]
        assert "limit" in response.metadata.degraded_capabilities
        assert any("truncated" in w for w in response.metadata.warnings)

    async def test_limit_larger_than_page_is_harmless(self, make_provider):
        provider, _ = make_provider()
        response = await provider.search(_full_query(limit=50))
        assert len(response.results) == 3
        assert "limit" not in response.metadata.degraded_capabilities


# ================================================================== error handling
class TestHttpErrors:
    async def test_timeout_raises_domain_timeout_error(self, make_provider):
        provider, _ = make_provider(effect=httpx.ReadTimeout("read timeout"))
        with pytest.raises(SearchProviderTimeoutError):
            await provider.search(_full_query())

    async def test_connect_timeout_on_config_is_reported_not_raised(self, make_provider):
        # capabilities() must never raise for a down instance — it reports it.
        provider, _ = make_provider(effect=httpx.ConnectTimeout("connect"))
        caps = await provider.capabilities()
        assert caps.available is False

    async def test_connect_timeout_on_search_raises_domain_timeout_error(self, make_provider):
        provider, _ = make_provider(effect=httpx.ConnectTimeout("connect"))
        with pytest.raises(SearchProviderTimeoutError):
            await provider.search(_full_query())

    async def test_connection_error_raises_unavailable(self, make_provider):
        provider, _ = make_provider(effect=httpx.ConnectError("connection refused"))
        with pytest.raises(SearchProviderUnavailableError) as excinfo:
            await provider.search(_full_query())
        assert excinfo.value.details["base_url"] == BASE_URL

    async def test_non_2xx_search_raises_unavailable(self, make_provider):
        provider, _ = make_provider(search_status=500)
        with pytest.raises(SearchProviderUnavailableError) as excinfo:
            await provider.search(_full_query())
        assert excinfo.value.details["status_code"] == 500

    async def test_search_connect_error_raises_unavailable(self, make_provider):
        # /config succeeds, /search fails to connect.
        provider, _ = make_provider(search_effect=httpx.ConnectError("no route to host"))
        with pytest.raises(SearchProviderUnavailableError) as excinfo:
            await provider.search(_full_query())
        assert excinfo.value.details["base_url"] == BASE_URL

    async def test_search_read_timeout_raises_domain_timeout_error(self, make_provider):
        provider, _ = make_provider(search_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(SearchProviderTimeoutError):
            await provider.search(_full_query())

    async def test_403_search_json_disabled_raises_unavailable(self, make_provider):
        # SearXNG returns 403 when the JSON format is not enabled.
        provider, _ = make_provider(search_status=403)
        with pytest.raises(SearchProviderUnavailableError) as excinfo:
            await provider.search(_full_query())
        assert excinfo.value.details["status_code"] == 403

    async def test_stale_cache_used_when_config_refresh_fails(self, make_provider):
        config = SearxngConfig(enabled=True, base_url=BASE_URL, config_cache_ttl_seconds=0)
        provider, mock = make_provider(config=config)

        # First search primes the discovery cache.
        first = await provider.search(_full_query())
        assert first.metadata.degraded_capabilities == []

        # The /config endpoint now fails ...
        mock._config_status = 500

        # ... but the search still succeeds from the stale cache.
        second = await provider.search(_full_query())
        assert second.metadata.degraded_capabilities == []
        assert mock.config_request_count() == 2  # attempted refresh, then fell back


class TestMalformedResponses:
    async def test_invalid_json_raises(self, make_provider):
        provider, _ = make_provider(
            search_body=b"not json at all",
            search_content_type="text/html",
        )
        with pytest.raises(InvalidProviderResponseError) as excinfo:
            await provider.search(_full_query())
        assert excinfo.value.details["content_type"] == "text/html"

    async def test_non_object_json_raises(self, make_provider):
        provider, _ = make_provider(search_body=b"[]", search_content_type="application/json")
        with pytest.raises(InvalidProviderResponseError):
            await provider.search(_full_query())

    async def test_missing_results_key_raises(self, make_provider):
        payload = dict(SEARCH_PAYLOAD)
        del payload["results"]
        provider, _ = make_provider(search_payload=payload)
        with pytest.raises(InvalidProviderResponseError, match="results"):
            await provider.search(_full_query())

    async def test_results_not_a_list_raises(self, make_provider):
        payload = dict(SEARCH_PAYLOAD, results="nope")
        provider, _ = make_provider(search_payload=payload)
        with pytest.raises(InvalidProviderResponseError, match="results"):
            await provider.search(_full_query())

    async def test_all_results_malformed_raises(self, make_provider):
        payload = {
            "query": "dolphins",
            "results": [
                {"title": "no url"},
                {"url": "not-a-url", "title": "bad url"},
                {"url": "https://ok.example", "title": ""},
            ],
        }
        provider, _ = make_provider(search_payload=payload)
        with pytest.raises(InvalidProviderResponseError, match="none could be normalized"):
            await provider.search(_full_query())

    async def test_partial_malformed_results_are_dropped(self, make_provider):
        payload = dict(
            SEARCH_PAYLOAD,
            results=[
                dict(SEARCH_PAYLOAD["results"][0]),
                {"title": "missing url", "url": None},
                dict(SEARCH_PAYLOAD["results"][2]),
            ],
        )
        provider, _ = make_provider(search_payload=payload)
        response = await provider.search(_full_query())
        assert len(response.results) == 2
        # Ranks preserve the provider's original ordering (1 and 3).
        assert [r.rank for r in response.results] == [1, 3]
        assert any("dropped 1 malformed" in w for w in response.metadata.warnings)

    async def test_invalid_config_json_raises(self, make_provider):
        provider, _ = make_provider(config_body=b"{", config_content_type="text/html")
        with pytest.raises(InvalidProviderResponseError):
            await provider.capabilities()

    async def test_invalid_published_date_becomes_none(self, make_provider):
        payload = dict(SEARCH_PAYLOAD)
        payload["results"] = [dict(payload["results"][0], publishedDate="not-a-date")]
        provider, _ = make_provider(search_payload=payload)
        response = await provider.search(_full_query())
        assert response.results[0].published_at is None


# ================================================================= result normalization
class TestNormalizeResult:
    def test_non_dict_entry_is_dropped(self):
        assert _normalize_result("not a dict", 1, "searxng") is None
        assert _normalize_result(None, 1, "searxng") is None

    def test_missing_url_is_dropped(self):
        assert _normalize_result({"title": "t"}, 1, "searxng") is None

    def test_relative_url_is_dropped(self):
        assert _normalize_result({"url": "/relative", "title": "t"}, 1, "searxng") is None

    def test_empty_title_is_dropped(self):
        assert _normalize_result({"url": "https://x.example", "title": "  "}, 1, "searxng") is None

    def test_source_from_engines_list_when_engine_missing(self):
        result = _normalize_result(
            {"url": "https://x.example", "title": "t", "engines": ["google", "ddg"]},
            1,
            "searxng",
        )
        assert result is not None
        assert result.source == "google"

    def test_empty_engine_without_fallback_becomes_none(self):
        result = _normalize_result(
            {"url": "https://x.example", "title": "t", "engine": ""}, 1, "searxng"
        )
        assert result is not None
        assert result.source is None

    def test_oversized_source_is_truncated(self):
        long_name = "e" * 150
        result = _normalize_result(
            {"url": "https://x.example", "title": "t", "engine": long_name}, 1, "searxng"
        )
        assert result is not None
        assert result.source == "e" * 100

    def test_relative_thumbnail_is_dropped(self):
        result = _normalize_result(
            {
                "url": "https://x.example",
                "title": "t",
                "img_src": "relative/image.jpg",
            },
            1,
            "searxng",
        )
        assert result is not None
        assert result.thumbnail_url is None

    def test_thumbnail_falls_back_to_thumbnail_key(self):
        result = _normalize_result(
            {"url": "https://x.example", "title": "t", "thumbnail": "https://img.example/t.png"},
            1,
            "searxng",
        )
        assert result is not None
        assert result.thumbnail_url == "https://img.example/t.png"

    def test_unknown_category_becomes_web(self):
        result = _normalize_result(
            {"url": "https://x.example", "title": "t", "category": "some-new-category"},
            1,
            "searxng",
        )
        assert result is not None
        assert result.vertical == Vertical.WEB

    def test_datetime_published_date_passthrough(self):
        value = datetime(2025, 1, 2, 3, 4, 5)
        result = _normalize_result(
            {"url": "https://x.example", "title": "t", "publishedDate": value}, 1, "searxng"
        )
        assert result is not None
        assert result.published_at == value

    async def test_non_dict_results_are_dropped_via_payload(self, make_provider):
        payload = dict(
            SEARCH_PAYLOAD,
            results=[dict(SEARCH_PAYLOAD["results"][0]), None, "junk"],
        )
        provider, _ = make_provider(search_payload=payload)
        response = await provider.search(_full_query())
        assert len(response.results) == 1
        assert any("dropped 2 malformed" in w for w in response.metadata.warnings)


# ==================================================================== caching
class TestUnresponsiveReporting:
    async def test_empty_unresponsive_list_yields_no_warning(self, make_provider):
        payload = dict(SEARCH_PAYLOAD, unresponsive_engines=[])
        provider, _ = make_provider(search_payload=payload)
        response = await provider.search(_full_query())
        assert not any("unresponsive" in w for w in response.metadata.warnings)

    async def test_missing_unresponsive_key_yields_no_warning(self, make_provider):
        payload = dict(SEARCH_PAYLOAD)
        del payload["unresponsive_engines"]
        provider, _ = make_provider(search_payload=payload)
        response = await provider.search(_full_query())
        assert not any("unresponsive" in w for w in response.metadata.warnings)


class TestConfigCaching:
    async def test_config_fetched_once_per_ttl(self, make_provider):
        config = SearxngConfig(enabled=True, base_url=BASE_URL, config_cache_ttl_seconds=3600)
        provider, mock = make_provider(config=config)
        await provider.search(_full_query())
        await provider.search(_full_query())
        await provider.search(_full_query())
        assert mock.config_request_count() == 1
        assert len(mock.search_requests()) == 3

    async def test_ttl_zero_refetches_every_time(self, make_provider):
        config = SearxngConfig(enabled=True, base_url=BASE_URL, config_cache_ttl_seconds=0)
        provider, mock = make_provider(config=config)
        await provider.search(_full_query())
        await provider.search(_full_query())
        assert mock.config_request_count() == 2
