"""Tests for the SearchProvider contract using fake providers.

The protocol is structural, so these tests use plain classes (no base
class) to prove that the contract is easy to fake in unit tests.
"""

import pytest

from app.core.errors import (
    InvalidProviderResponseError,
    NoProviderAvailableError,
    ScraperError,
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
    UnsupportedSearchCapabilityError,
)
from app.search import SearchCapabilities, SearchProvider, SearchQuery, SearchResponse
from app.search.models import SafeSearch, SearchMetadata, SearchResult, Vertical


class FakeProvider:
    """A minimal in-memory provider satisfying the SearchProvider protocol."""

    def __init__(
        self,
        name: str = "fake",
        available: bool = True,
        verticals: list[Vertical] | None = None,
        results: list[SearchResult] | None = None,
        error: Exception | None = None,
        response: SearchResponse | None = None,
    ) -> None:
        self._name = name
        self._available = available
        self._verticals = verticals or [Vertical.WEB]
        self._results = results or []
        self._error = error
        self._response = response
        self.requests: list[SearchQuery] = []

    @property
    def name(self) -> str:
        return self._name

    async def capabilities(self) -> SearchCapabilities:
        return SearchCapabilities(verticals=self._verticals, available=self._available)

    async def search(self, request: SearchQuery) -> SearchResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        if self._response is not None:
            return self._response
        results = self._results or [
            SearchResult(
                title=f"Result {i} for {request.query}",
                url=f"https://example.com/{i}",
                rank=i,
                vertical=vertical,
                provider=self._name,
            )
            for i, vertical in enumerate(request.verticals, start=1)
        ]
        return SearchResponse(
            query=request.query,
            verticals=request.verticals,
            results=results,
            metadata=SearchMetadata(
                provider=self._name,
                providers_attempted=[self._name],
                elapsed_ms=10,
            ),
        )


class IncompleteProvider:
    """Only has a name — missing capabilities() and search()."""

    @property
    def name(self) -> str:
        return "incomplete"


# ==================================================================== contract
def test_fake_provider_satisfies_protocol():
    assert isinstance(FakeProvider(), SearchProvider)


def test_incomplete_provider_does_not_satisfy_protocol():
    assert not isinstance(IncompleteProvider(), SearchProvider)


async def test_provider_search_returns_normalized_response():
    provider = FakeProvider(name="fake")
    request = SearchQuery(query="dolphins", verticals=["web"], language="en")

    response = await provider.search(request)

    assert isinstance(response, SearchResponse)
    assert response.query == "dolphins"
    assert response.metadata.provider == "fake"
    assert response.metadata.providers_attempted == ["fake"]
    assert len(response.results) == 1
    assert response.results[0].provider == "fake"
    assert provider.requests == [request]


async def test_provider_capabilities_report_health():
    provider = FakeProvider(name="fake", available=False, verticals=[Vertical.WEB, Vertical.NEWS])
    caps = await provider.capabilities()
    assert caps.verticals == [Vertical.WEB, Vertical.NEWS]
    assert caps.available is False


async def test_provider_errors_propagate_as_domain_errors():
    provider = FakeProvider(error=SearchProviderTimeoutError("search timed out"))
    with pytest.raises(SearchProviderTimeoutError):
        await provider.search(SearchQuery(query="q"))


async def test_capability_negotiation_drives_degraded_metadata():
    """A provider that lacks a requested vertical reports it as degraded."""
    provider = FakeProvider(name="fake", verticals=[Vertical.WEB])
    request = SearchQuery(query="q", verticals=["web", "news"], safe_search=SafeSearch.OFF)

    caps = await provider.capabilities()
    degraded = caps.degradations(request)

    assert "vertical:news" in degraded
    # A well-behaved provider surfaces the degradation in the response metadata.
    response = SearchResponse(
        query=request.query,
        verticals=[Vertical.WEB],
        results=[],
        metadata=SearchMetadata(
            provider=provider.name,
            degraded_capabilities=degraded,
            warnings=[f"capability not supported: {token}" for token in degraded],
        ),
    )
    assert response.metadata.degraded_capabilities == ["vertical:news"]
    assert len(response.metadata.warnings) == 1


# ================================================================== search errors
def test_search_error_types():
    cases = [
        (SearchProviderUnavailableError("backend down"), "search_provider_unavailable", 503),
        (SearchProviderTimeoutError("timed out"), "search_provider_timeout", 504),
        (
            UnsupportedSearchCapabilityError("no provider for maps"),
            "unsupported_search_capability",
            400,
        ),
        (InvalidProviderResponseError("unparseable payload"), "invalid_provider_response", 502),
        (NoProviderAvailableError("no providers configured"), "no_provider_available", 503),
    ]
    for exc, error_type, status in cases:
        assert exc.error_type == error_type
        assert exc.status_code == status
        d = exc.to_dict()
        assert d["error"]["type"] == error_type
        assert d["error"]["message"] == exc.message
        assert d["error"]["details"] == {}


def test_search_errors_accept_details():
    exc = InvalidProviderResponseError("bad payload", {"provider": "fake"})
    assert exc.details == {"provider": "fake"}
    assert exc.to_dict()["error"]["details"] == {"provider": "fake"}


def test_search_errors_are_scraper_errors():
    for exc in (
        SearchProviderUnavailableError("x"),
        SearchProviderTimeoutError("x"),
        UnsupportedSearchCapabilityError("x"),
        InvalidProviderResponseError("x"),
        NoProviderAvailableError("x"),
    ):
        assert isinstance(exc, ScraperError)
