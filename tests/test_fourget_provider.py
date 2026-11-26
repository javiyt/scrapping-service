"""Tests for the 4get :class:`SearchProvider` implementation.

All 4get HTTP traffic is mocked with ``httpx.MockTransport`` — no live 4get
instance (public or otherwise) is ever contacted, and there is no internet
access, as required by the ticket.  The mock mimics the instance's
``/ami4get`` (health) and ``/api/v1/*`` (search) endpoints.
"""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.core.errors import (
    InvalidProviderResponseError,
    NoProviderAvailableError,
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
)
from app.search import SearchProvider, SearchQuery
from app.search.models import SafeSearch, Vertical
from app.search.providers.fourget import FourgetConfig, FourgetProvider
from app.search.providers.fourget.provider import _normalize_result
from app.search.router import FALLBACK_ELIGIBLE_ERRORS

BASE_URL = "http://4get.test"
PASS_TOKEN = "super-secret-pass"


# ==================================================================== payloads
WEB_PAYLOAD: dict = {
    "status": "ok",
    "query": "dolphins",
    "answer": [],
    "result": [
        {
            "title": "First result",
            "url": "https://example.com/first",
            "snippet": "First snippet",
            "engine": "duckduckgo",
            "lang": "en",
            "date": 1750000000,
            "image": "https://img.example/first.jpg",
            "time": None,
        },
        {
            "title": "Second result",
            "url": "https://example.com/second",
            "snippet": None,
            "engine": None,
            "lang": None,
            "date": None,
            "image": None,
            "time": None,
        },
        {"title": None, "url": None},  # fully null → dropped
    ],
    "npt": "opaque-npt-token-abc",
    "spelling": None,
    "related": ["dolphin"],
    "table": None,
    "time": 1.2,
}

NEWS_PAYLOAD: dict = {
    "status": "ok",
    "query": "dolphins",
    "result": [
        {
            "title": "News item",
            "url": "https://news.example/item",
            "snippet": "Breaking news",
            "engine": "brave",
            "date": 1750000000,
            "image": None,
            "time": None,
        }
    ],
    "npt": None,
}

IMAGES_PAYLOAD: dict = {
    "status": "ok",
    "query": "dolphins",
    "result": [
        {
            "title": "Dolphin photo",
            "url": "https://photos.example/dolphin.jpg",
            "snippet": None,
            "engine": "marginalia",
            "date": None,
            "image": "https://photos.example/thumb.jpg",
            "time": None,
        }
    ],
    "npt": "another-npt-token",
}

VIDEOS_PAYLOAD: dict = {
    "status": "ok",
    "query": "dolphins",
    "result": [
        {
            "title": "Dolphin video",
            "url": "https://video.example/watch/1",
            "snippet": "Watch dolphins",
            "engine": "duckduckgo",
            "date": 1740000000,
            "image": "https://video.example/thumb.jpg",
            "time": 245,
        }
    ],
    "npt": None,
}

MUSIC_PAYLOAD: dict = {
    "status": "ok",
    "query": "dolphins",
    "result": [
        {
            "title": "Dolphin Song",
            "url": "https://music.example/track/1",
            "snippet": None,
            "engine": "brave",
            "artist": "Dolphin Band",
            "album": "Ocean Album",
            "date": None,
            "image": "https://music.example/cover.jpg",
            "time": None,
        },
        {
            "title": "Explicit track",
            "url": "https://music.example/track/2",
            "snippet": "An explicit track",
            "engine": None,
            "artist": None,
            "album": None,
            "date": None,
            "image": None,
            "time": None,
        },
    ],
    "npt": None,
}

HEALTH_PAYLOAD: dict = {
    "status": "ok",
    "instance": "test-4get",
    "version": "1.0",
}


class MockFourget:
    """A fake 4get instance backing an injected ``httpx.AsyncClient``.

    Records every request so tests can assert on the translated 4get query
    parameters, cookies, and endpoint paths.  Responses are configurable;
    ``search_handler`` may supply a per-request response override.
    """

    def __init__(
        self,
        health_payload: dict | None = None,
        health_status: int = 200,
        health_body: bytes | None = None,
        health_content_type: str = "application/json",
        search_payload: dict | None = None,
        search_status: int = 200,
        search_body: bytes | None = None,
        search_content_type: str = "application/json",
        effect: Exception | None = None,
        search_handler=None,
    ) -> None:
        self._health_payload = health_payload if health_payload is not None else HEALTH_PAYLOAD
        self._health_status = health_status
        self._health_body = health_body
        self._health_content_type = health_content_type
        self._search_payload = search_payload if search_payload is not None else WEB_PAYLOAD
        self._search_status = search_status
        self._search_body = search_body
        self._search_content_type = search_content_type
        self._effect = effect
        self._search_handler = search_handler
        self.requests: list[httpx.Request] = []
        self.search_request_count = 0

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
        if request.url.path == "/ami4get":
            if self._health_body is not None:
                return httpx.Response(
                    self._health_status,
                    content=self._health_body,
                    headers={"content-type": self._health_content_type},
                )
            return _json_response(self._health_payload, self._health_status)
        if request.url.path.startswith("/api/v1/"):
            self.search_request_count += 1
            if self._search_handler is not None:
                return self._search_handler(request)
            if self._search_body is not None:
                return httpx.Response(
                    self._search_status,
                    content=self._search_body,
                    headers={"content-type": self._search_content_type},
                )
            if self._search_status != 200:
                return httpx.Response(self._search_status, content=b"search error body")
            return _json_response(self._search_payload, 200)
        raise AssertionError(f"unexpected request path: {request.url.path}")

    # ------------------------------------------------------------- assertions
    def search_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.startswith("/api/v1/")]

    def search_request_paths(self) -> list[str]:
        return [r.url.path for r in self.search_requests()]

    def search_params(self) -> dict:
        assert self.search_requests(), "no /api/v1/* request was made"
        return dict(self.search_requests()[-1].url.params)

    def search_cookie(self) -> str | None:
        requests = self.search_requests()
        return requests[-1].headers.get("cookie") if requests else None

    def health_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/ami4get"]


def _json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )


@pytest.fixture
def make_provider():
    """Build a FourgetProvider wired to a fresh MockFourget instance."""

    def _make(
        config: FourgetConfig | None = None,
        **mock_kwargs,
    ) -> tuple[FourgetProvider, MockFourget]:
        mock = MockFourget(**mock_kwargs)
        provider = FourgetProvider(
            config or FourgetConfig(enabled=True, base_url=BASE_URL),
            client=mock.client(),
        )
        return provider, mock

    return _make


def _full_query(**overrides) -> SearchQuery:
    defaults = dict(
        query="dolphins",
        verticals=["web"],
        safe_search=SafeSearch.MODERATE,
    )
    defaults.update(overrides)
    return SearchQuery(**defaults)


# ================================================================== contract
class TestContract:
    def test_name(self, make_provider):
        provider, _ = make_provider()
        assert provider.name == "4get"

    def test_satisfies_protocol(self, make_provider):
        provider, _ = make_provider()
        assert isinstance(provider, SearchProvider)

    async def test_close_with_injected_client_is_safe(self, make_provider):
        provider, _ = make_provider()
        await provider.close()  # must not close the injected client


# ================================================================ health probe
class TestHealthProbe:
    async def test_capabilities_report(self, make_provider):
        provider, mock = make_provider()
        caps = await provider.capabilities()
        assert caps.available is True
        assert caps.verticals == [
            Vertical.WEB,
            Vertical.NEWS,
            Vertical.IMAGES,
            Vertical.VIDEOS,
            Vertical.MUSIC,
        ]
        assert caps.time_ranges == []
        assert caps.supports_language is True
        assert caps.supports_safe_search is True
        assert caps.supports_paging is False
        assert caps.supports_source_selection is True
        assert caps.max_results is None
        assert caps.sources is None
        assert len(mock.health_requests()) == 1
        assert not mock.search_requests()  # health never hits a search endpoint

    async def test_health_cache_respects_ttl(self, make_provider):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, health_cache_ttl_seconds=3600)
        provider, mock = make_provider(config=config)
        await provider.capabilities()
        await provider.capabilities()
        await provider.capabilities()
        assert len(mock.health_requests()) == 1

    async def test_health_ttl_zero_reprobes_every_time(self, make_provider):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, health_cache_ttl_seconds=0)
        provider, mock = make_provider(config=config)
        await provider.capabilities()
        await provider.capabilities()
        assert len(mock.health_requests()) == 2

    @pytest.mark.parametrize(
        ("kwargs", "label"),
        [
            ({"health_status": 500}, "5xx"),
            ({"health_status": 429}, "429"),
            ({"effect": httpx.ReadTimeout("timeout")}, "timeout"),
            ({"effect": httpx.ConnectError("connection refused")}, "connection"),
        ],
    )
    async def test_probe_failures_report_unavailable_without_raising(
        self, make_provider, kwargs, label
    ):
        provider, _ = make_provider(**kwargs)
        caps = await provider.capabilities()
        assert caps.available is False
        # The static capability set is still reported.
        assert len(caps.verticals) == 5

    async def test_probe_non_ok_status_reports_unavailable(self, make_provider):
        provider, _ = make_provider(health_payload={"status": "pass-required"})
        caps = await provider.capabilities()
        assert caps.available is False

    async def test_probe_non_json_body_still_available(self, make_provider):
        provider, _ = make_provider(
            health_body=b"<html>not json</html>",
            health_content_type="text/html",
        )
        caps = await provider.capabilities()
        assert caps.available is True


# =============================================================== normalization
class TestNormalization:
    async def test_web_search_normalization(self, make_provider):
        provider, mock = make_provider()
        response = await provider.search(_full_query())

        assert mock.search_request_paths() == ["/api/v1/web"]
        assert mock.search_params()["s"] == "dolphins"

        assert response.query == "dolphins"
        assert response.verticals == [Vertical.WEB]
        assert response.metadata.provider == "4get"
        assert response.metadata.providers_attempted == ["4get"]
        assert response.metadata.page == 1
        assert response.metadata.total_pages is None
        assert response.metadata.total_results is None
        assert response.metadata.elapsed_ms >= 0

        # Two normalizable results; the fully-null entry is dropped.
        assert len(response.results) == 2
        assert any("dropped 1 malformed" in w for w in response.metadata.warnings)

        first = response.results[0]
        assert first.title == "First result"
        assert first.url == "https://example.com/first"
        assert first.snippet == "First snippet"
        assert first.rank == 1
        assert first.vertical == Vertical.WEB
        assert first.source == "duckduckgo"
        assert first.provider == "4get"
        assert first.thumbnail_url == "https://img.example/first.jpg"
        assert first.published_at == datetime.fromtimestamp(1750000000, tz=UTC)
        assert first.score is None

        second = response.results[1]
        assert second.rank == 2
        assert second.snippet is None
        assert second.source is None
        assert second.thumbnail_url is None
        assert second.published_at is None

    async def test_news_search_uses_news_endpoint(self, make_provider):
        provider, mock = make_provider(search_payload=NEWS_PAYLOAD)
        response = await provider.search(_full_query(verticals=["news"]))
        assert mock.search_request_paths() == ["/api/v1/news"]
        assert response.verticals == [Vertical.NEWS]
        assert response.results[0].vertical == Vertical.NEWS
        assert response.results[0].source == "brave"
        assert response.results[0].published_at == datetime.fromtimestamp(1750000000, tz=UTC)

    async def test_images_search_uses_images_endpoint(self, make_provider):
        provider, mock = make_provider(search_payload=IMAGES_PAYLOAD)
        response = await provider.search(_full_query(verticals=["images"]))
        assert mock.search_request_paths() == ["/api/v1/images"]
        assert response.results[0].vertical == Vertical.IMAGES
        assert response.results[0].thumbnail_url == "https://photos.example/thumb.jpg"
        assert response.results[0].source == "marginalia"

    async def test_videos_search_uses_videos_endpoint(self, make_provider):
        provider, mock = make_provider(search_payload=VIDEOS_PAYLOAD)
        response = await provider.search(_full_query(verticals=["videos"]))
        assert mock.search_request_paths() == ["/api/v1/videos"]
        result = response.results[0]
        assert result.vertical == Vertical.VIDEOS
        assert result.thumbnail_url == "https://video.example/thumb.jpg"
        assert result.published_at == datetime.fromtimestamp(1740000000, tz=UTC)

    async def test_music_search_uses_music_endpoint_and_snippet_fallback(self, make_provider):
        provider, mock = make_provider(search_payload=MUSIC_PAYLOAD)
        response = await provider.search(_full_query(verticals=["music"]))
        assert mock.search_request_paths() == ["/api/v1/music"]
        first = response.results[0]
        assert first.vertical == Vertical.MUSIC
        assert first.title == "Dolphin Song"
        # No snippet on the item → composed from artist + album.
        assert first.snippet == "Dolphin Band — Ocean Album"
        second = response.results[1]
        assert second.snippet == "An explicit track"

    async def test_null_fields_are_tolerated(self, make_provider):
        payload = {
            "status": "ok",
            "result": [
                {
                    "title": "Bare result",
                    "url": "https://bare.example",
                    "snippet": None,
                    "engine": None,
                    "date": None,
                    "image": None,
                    "time": None,
                }
            ],
            "npt": None,
        }
        provider, _ = make_provider(search_payload=payload)
        response = await provider.search(_full_query())
        result = response.results[0]
        assert result.snippet is None
        assert result.source is None
        assert result.published_at is None
        assert result.thumbnail_url is None

    async def test_npt_token_never_leaks_into_response(self, make_provider):
        provider, mock = make_provider()
        response = await provider.search(_full_query())
        dumped = json.dumps(response.model_dump(mode="json"))
        assert "opaque-npt-token-abc" not in dumped
        assert "npt" not in dumped
        # A second search must not resend the token either.
        await provider.search(_full_query())
        for request in mock.search_requests():
            assert "npt" not in dict(request.url.params)


class TestNoLeak:
    async def test_4get_specific_fields_do_not_leak(self, make_provider):
        provider, _ = make_provider()
        response = await provider.search(_full_query())
        dumped = json.dumps(response.model_dump(mode="json"))
        for token in ("npt", "scraper", "engine", "answer", "spelling", "extendedsearch"):
            assert token not in dumped
        # Result objects carry exactly the domain fields.
        for result in response.results:
            assert set(result.model_dump().keys()) == {
                "title",
                "url",
                "snippet",
                "rank",
                "vertical",
                "source",
                "provider",
                "thumbnail_url",
                "published_at",
                "score",
            }


# ===================================================================== params
class TestRequestParameters:
    async def test_query_parameter(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query(query="bottlenose dolphin"))
        assert mock.search_params()["s"] == "bottlenose dolphin"

    async def test_scraper_from_sources(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query(sources=["brave", "google"]))
        assert mock.search_params()["scraper"] == "brave"  # first source wins

    async def test_scraper_from_config_default(self, make_provider):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, scraper="duckduckgo")
        provider, mock = make_provider(config=config)
        await provider.search(_full_query())
        assert mock.search_params()["scraper"] == "duckduckgo"

    async def test_sources_override_config_scraper(self, make_provider):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, scraper="duckduckgo")
        provider, mock = make_provider(config=config)
        await provider.search(_full_query(sources=["yandex"]))
        assert mock.search_params()["scraper"] == "yandex"

    async def test_no_scraper_omits_parameter(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query())
        assert "scraper" not in mock.search_params()

    async def test_language_from_request(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query(language="pt-BR"))
        assert mock.search_params()["lang"] == "pt-BR"

    async def test_language_from_config_default(self, make_provider):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, default_language="en")
        provider, mock = make_provider(config=config)
        await provider.search(_full_query())
        assert mock.search_params()["lang"] == "en"

    async def test_no_language_omits_parameter(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query())
        assert "lang" not in mock.search_params()

    async def test_region_from_config(self, make_provider):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, default_region="us")
        provider, mock = make_provider(config=config)
        await provider.search(_full_query())
        assert mock.search_params()["country"] == "us"

    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (SafeSearch.OFF, "1"),
            (SafeSearch.MODERATE, "0"),
            (SafeSearch.STRICT, "0"),
        ],
    )
    async def test_safe_search_levels(self, make_provider, level, expected):
        provider, mock = make_provider()
        await provider.search(_full_query(safe_search=level))
        assert mock.search_params()["nsfw"] == expected

    async def test_nsfw_default_applies_at_moderate(self, make_provider):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, default_nsfw=True)
        provider, mock = make_provider(config=config)
        await provider.search(_full_query(safe_search=SafeSearch.MODERATE))
        assert mock.search_params()["nsfw"] == "1"
        await provider.search(_full_query(safe_search=SafeSearch.STRICT))
        assert mock.search_params()["nsfw"] == "0"

    async def test_time_range_never_sends_date_parameters(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query(time_range="week"))
        params = mock.search_params()
        for param in ("after", "before", "time_range", "date"):
            assert param not in params


# ================================================================== paging/npt
class TestPaging:
    async def test_page_one_default_metadata(self, make_provider):
        provider, _ = make_provider()
        response = await provider.search(_full_query())
        assert response.metadata.page == 1

    async def test_page_over_one_degrades_to_page_one(self, make_provider):
        provider, mock = make_provider()
        response = await provider.search(_full_query(page=3))
        assert response.metadata.page == 1
        assert "paging" in response.metadata.degraded_capabilities
        assert any("npt" in w for w in response.metadata.warnings)
        # The provider never sends npt, even to reach page 3.
        assert "npt" not in mock.search_params()

    async def test_expired_or_invalid_token_maps_to_invalid_response(self, make_provider):
        # An instance that rejects a token reports it via the envelope.
        provider, _ = make_provider(search_payload={"status": "npt expired", "result": []})
        with pytest.raises(InvalidProviderResponseError) as excinfo:
            await provider.search(_full_query())
        assert "npt expired" in excinfo.value.message
        assert excinfo.value.details == {"status_code": 200}

    async def test_no_npt_in_state_after_search(self, make_provider):
        provider, _ = make_provider()
        await provider.search(_full_query())
        # The token must not be stashed on the provider for later use.
        assert not any(
            isinstance(value, str) and "opaque-npt-token-abc" in value
            for value in vars(provider).values()
        )


# ==================================================================== pass cookie
class TestPassCookie:
    async def test_pass_cookie_sent_when_configured(self, make_provider):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, pass_token=PASS_TOKEN)
        provider, mock = make_provider(config=config)
        await provider.search(_full_query())
        cookie = mock.search_cookie()
        assert cookie is not None
        assert f"pass={PASS_TOKEN}" in cookie

        # The health probe carries it too.
        provider._available = None
        provider._available_at = 0.0
        caps = await provider.capabilities()
        assert caps.available is True
        health_cookie = mock.health_requests()[-1].headers.get("cookie")
        assert health_cookie is not None
        assert f"pass={PASS_TOKEN}" in health_cookie

    async def test_no_cookie_when_pass_not_configured(self, make_provider):
        provider, mock = make_provider()
        await provider.search(_full_query())
        assert mock.search_cookie() is None

    async def test_429_raises_non_retryable_pass_error(self, make_provider):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, pass_token=PASS_TOKEN)
        provider, _ = make_provider(config=config, search_status=429)
        with pytest.raises(NoProviderAvailableError) as excinfo:
            await provider.search(_full_query())
        error = excinfo.value
        # Clear configuration error naming the fix ...
        assert "pass" in error.message.lower()
        assert "SCRAPER_SEARCH_4GET_PASS_TOKEN" in error.message
        # ... but never the token itself, upstream body, or instance URL.
        assert PASS_TOKEN not in error.message
        assert "search error body" not in error.message
        assert BASE_URL not in error.message
        assert PASS_TOKEN not in json.dumps(error.details)
        assert "search error body" not in json.dumps(error.details)
        # Non-retryable: the router never falls back on this error class.
        assert type(error) not in FALLBACK_ELIGIBLE_ERRORS

    async def test_pass_token_never_in_diagnostics(self, make_provider):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, pass_token=PASS_TOKEN)
        provider, _ = make_provider(config=config)
        diagnostics = provider.diagnostics()
        dumped = json.dumps(diagnostics)
        assert PASS_TOKEN not in dumped
        assert diagnostics["pass_configured"] is True
        assert diagnostics["base_url_configured"] is True

    async def test_pass_token_never_in_response_metadata(self, make_provider):
        config = FourgetConfig(enabled=True, base_url=BASE_URL, pass_token=PASS_TOKEN)
        provider, _ = make_provider(config=config)
        response = await provider.search(_full_query())
        assert PASS_TOKEN not in json.dumps(response.model_dump(mode="json"))


# ================================================================== error mapping
class TestHttpErrors:
    async def test_timeout_raises_domain_timeout_error(self, make_provider):
        provider, _ = make_provider(effect=httpx.ReadTimeout("read timeout"))
        with pytest.raises(SearchProviderTimeoutError) as excinfo:
            await provider.search(_full_query())
        assert "4get" in excinfo.value.message
        assert BASE_URL not in excinfo.value.message

    async def test_connection_error_raises_unavailable(self, make_provider):
        provider, _ = make_provider(effect=httpx.ConnectError("connection refused"))
        with pytest.raises(SearchProviderUnavailableError) as excinfo:
            await provider.search(_full_query())
        assert excinfo.value.message == "4get instance is unreachable"
        assert BASE_URL not in excinfo.value.message

    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_server_errors_raise_unavailable(self, make_provider, status):
        provider, _ = make_provider(search_status=status)
        with pytest.raises(SearchProviderUnavailableError) as excinfo:
            await provider.search(_full_query())
        assert excinfo.value.details["status_code"] == status
        # The upstream body must not leak into the error.
        assert "search error body" not in excinfo.value.message
        assert "search error body" not in json.dumps(excinfo.value.details)

    async def test_client_error_404_raises_unavailable(self, make_provider):
        provider, _ = make_provider(search_status=404)
        with pytest.raises(SearchProviderUnavailableError):
            await provider.search(_full_query())

    async def test_timeout_details_carry_only_seconds(self, make_provider):
        provider, _ = make_provider(effect=httpx.ConnectTimeout("slow connect"))
        with pytest.raises(SearchProviderTimeoutError) as excinfo:
            await provider.search(_full_query())
        assert excinfo.value.details == {"timeout_seconds": 15.0}


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

    async def test_missing_status_raises(self, make_provider):
        provider, _ = make_provider(search_payload={"result": []})
        with pytest.raises(InvalidProviderResponseError, match="failure"):
            await provider.search(_full_query())

    async def test_non_ok_status_raises(self, make_provider):
        provider, _ = make_provider(search_payload={"status": "error", "result": []})
        with pytest.raises(InvalidProviderResponseError, match="status: error"):
            await provider.search(_full_query())

    async def test_missing_result_key_raises(self, make_provider):
        provider, _ = make_provider(search_payload={"status": "ok"})
        with pytest.raises(InvalidProviderResponseError, match="'result'"):
            await provider.search(_full_query())

    async def test_result_not_a_list_raises(self, make_provider):
        provider, _ = make_provider(search_payload={"status": "ok", "result": "nope"})
        with pytest.raises(InvalidProviderResponseError, match="'result'"):
            await provider.search(_full_query())

    async def test_all_results_malformed_raises(self, make_provider):
        payload = {
            "status": "ok",
            "result": [
                {"title": "no url"},
                {"url": "not-a-url", "title": "bad url"},
                {"url": "https://ok.example", "title": ""},
            ],
        }
        provider, _ = make_provider(search_payload=payload)
        with pytest.raises(InvalidProviderResponseError, match="none could be normalized"):
            await provider.search(_full_query())

    async def test_empty_results_are_valid(self, make_provider):
        provider, _ = make_provider(search_payload={"status": "ok", "result": []})
        response = await provider.search(_full_query())
        assert response.results == []


# ================================================================ capabilities / filters
class TestDegradedFilters:
    async def test_unsupported_vertical_degraded(self, make_provider):
        provider, mock = make_provider()
        response = await provider.search(_full_query(verticals=["web", "social"]))
        assert mock.search_request_paths() == ["/api/v1/web"]
        assert "vertical:social" in response.metadata.degraded_capabilities
        assert any("social" in w for w in response.metadata.warnings)
        assert response.verticals == [Vertical.WEB]

    async def test_first_supported_vertical_wins(self, make_provider):
        provider, mock = make_provider()
        response = await provider.search(_full_query(verticals=["music", "news"]))
        # music is earlier in the list and supported → it is served, even
        # though news is also supported (one request per search, by design).
        assert mock.search_request_paths() == ["/api/v1/music"]
        assert response.verticals == [Vertical.MUSIC]
        # news is supported but not served (one request per search) → degraded.
        assert response.metadata.degraded_capabilities == ["vertical:news"]

    async def test_no_supported_vertical_falls_back_to_web(self, make_provider):
        provider, mock = make_provider()
        response = await provider.search(_full_query(verticals=["maps"]))
        assert mock.search_request_paths() == ["/api/v1/web"]
        assert "vertical:maps" in response.metadata.degraded_capabilities
        assert response.verticals == [Vertical.WEB]

    async def test_time_range_degraded(self, make_provider):
        provider, _ = make_provider()
        response = await provider.search(_full_query(time_range="day"))
        assert "time_range" in response.metadata.degraded_capabilities
        assert any("time range" in w for w in response.metadata.warnings)

    async def test_limit_truncates_client_side(self, make_provider):
        provider, _ = make_provider()
        response = await provider.search(_full_query(limit=1))
        assert len(response.results) == 1
        assert "limit" in response.metadata.degraded_capabilities
        assert any("truncated" in w for w in response.metadata.warnings)

    async def test_limit_larger_than_page_is_harmless(self, make_provider):
        provider, _ = make_provider()
        response = await provider.search(_full_query(limit=50))
        assert "limit" not in response.metadata.degraded_capabilities


# =============================================================== result normalization unit tests
class TestNormalizeResult:
    def test_non_dict_entry_is_dropped(self):
        assert _normalize_result("not a dict", 1, Vertical.WEB, "4get") is None
        assert _normalize_result(None, 1, Vertical.WEB, "4get") is None

    def test_missing_url_is_dropped(self):
        assert _normalize_result({"title": "t"}, 1, Vertical.WEB, "4get") is None

    def test_relative_url_is_dropped(self):
        result = _normalize_result({"url": "/relative", "title": "t"}, 1, Vertical.WEB, "4get")
        assert result is None

    def test_empty_title_is_dropped(self):
        result = _normalize_result(
            {"url": "https://x.example", "title": "  "}, 1, Vertical.WEB, "4get"
        )
        assert result is None

    def test_video_duration_is_not_mapped(self):
        result = _normalize_result(
            {
                "url": "https://video.example/v",
                "title": "v",
                "time": 245,
            },
            1,
            Vertical.VIDEOS,
            "4get",
        )
        assert result is not None
        assert "time" not in result.model_dump()
        assert "duration" not in result.model_dump()

    def test_relative_thumbnail_is_dropped(self):
        result = _normalize_result(
            {"url": "https://x.example", "title": "t", "image": "relative/image.jpg"},
            1,
            Vertical.IMAGES,
            "4get",
        )
        assert result is not None
        assert result.thumbnail_url is None

    def test_oversized_engine_is_truncated(self):
        result = _normalize_result(
            {"url": "https://x.example", "title": "t", "engine": "e" * 150},
            1,
            Vertical.WEB,
            "4get",
        )
        assert result is not None
        assert result.source == "e" * 100

    def test_music_snippet_composition(self):
        result = _normalize_result(
            {"url": "https://music.example", "title": "t", "artist": "A", "album": "B"},
            1,
            Vertical.MUSIC,
            "4get",
        )
        assert result is not None
        assert result.snippet == "A — B"

    def test_web_item_ignores_artist_album(self):
        result = _normalize_result(
            {"url": "https://x.example", "title": "t", "artist": "A", "album": "B"},
            1,
            Vertical.WEB,
            "4get",
        )
        assert result is not None
        assert result.snippet is None

    def test_epoch_string_date(self):
        result = _normalize_result(
            {"url": "https://x.example", "title": "t", "date": "1750000000"},
            1,
            Vertical.WEB,
            "4get",
        )
        assert result is not None
        assert result.published_at == datetime.fromtimestamp(1750000000, tz=UTC)


# ==================================================================== factory
class TestFactoryWiring:
    def _settings(self, block: dict | None) -> object:
        from app.core.config import Settings

        settings = Settings(api_key="test-key", server_api_key_required=False)
        object.__setattr__(settings, "raw_yaml_search", block)
        return settings

    def test_disabled_by_default_not_built(self):
        from app.search.providers import build_search_providers

        providers = build_search_providers(self._settings(None))
        assert [p.name for p in providers] == []

    def test_disabled_explicit_not_built(self):
        from app.search.providers import build_search_providers

        providers = build_search_providers(
            self._settings({"providers": {"4get": {"enabled": False, "base_url": BASE_URL}}})
        )
        assert providers == []

    async def test_enabled_builds_provider(self):
        from app.search.providers import build_search_providers

        providers = build_search_providers(
            self._settings({"providers": {"4get": {"enabled": True, "base_url": BASE_URL}}})
        )
        assert len(providers) == 1
        provider = providers[0]
        assert provider.name == "4get"
        assert isinstance(provider, SearchProvider)
        await provider.close()

    def test_enabled_without_base_url_raises(self):
        from app.search.providers import build_search_providers

        try:
            build_search_providers(self._settings({"providers": {"4get": {"enabled": True}}}))
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for enabled 4get without base_url")
