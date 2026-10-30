"""Tests for the provider-agnostic search domain models."""

import pytest
from pydantic import ValidationError

from app.search.models import (
    SafeSearch,
    SearchCapabilities,
    SearchMetadata,
    SearchQuery,
    SearchResponse,
    SearchResult,
    TimeRange,
    Vertical,
)


# ================================================================ SearchQuery
class TestSearchQuery:
    def test_defaults(self):
        req = SearchQuery(query="dolphins")
        assert req.query == "dolphins"
        assert req.verticals == [Vertical.WEB]
        assert req.language is None
        assert req.time_range is None
        assert req.safe_search == SafeSearch.MODERATE
        assert req.page == 1
        assert req.limit is None
        assert req.sources == []
        assert req.provider == "auto"
        assert req.cache_ttl_seconds is None
        assert req.force_refresh is False

    def test_full_request(self):
        req = SearchQuery(
            query="orca migration",
            verticals=["news", "images"],
            language="en-US",
            time_range="week",
            safe_search="strict",
            page=3,
            limit=20,
            sources=["bing", "duckduckgo"],
            provider="example",
            cache_ttl_seconds=3600,
            force_refresh=True,
        )
        assert req.verticals == [Vertical.NEWS, Vertical.IMAGES]
        assert req.time_range == TimeRange.WEEK
        assert req.safe_search == SafeSearch.STRICT
        assert req.page == 3
        assert req.limit == 20
        assert req.provider == "example"

    @pytest.mark.parametrize(
        "vertical",
        ["web", "news", "images", "videos", "social", "it", "science", "maps", "music", "files"],
    )
    def test_vertical_vocabulary(self, vertical):
        req = SearchQuery(query="q", verticals=[vertical])
        assert req.verticals == [Vertical(vertical)]

    @pytest.mark.parametrize("bad", ["sports", "Web", "", 1, None])
    def test_invalid_vertical_rejected(self, bad):
        with pytest.raises(ValidationError):
            SearchQuery(query="q", verticals=[bad])

    def test_empty_verticals_rejected(self):
        with pytest.raises(ValidationError):
            SearchQuery(query="q", verticals=[])

    def test_duplicate_verticals_rejected(self):
        with pytest.raises(ValidationError):
            SearchQuery(query="q", verticals=["web", "web"])

    def test_query_is_required(self):
        with pytest.raises(ValidationError):
            SearchQuery()  # type: ignore[call-arg]

    def test_empty_query_rejected(self):
        with pytest.raises(ValidationError):
            SearchQuery(query="")

    @pytest.mark.parametrize("language", ["en", "en-US", "pt-BR", "zh-Hant"])
    def test_valid_language(self, language):
        assert SearchQuery(query="q", language=language).language == language

    @pytest.mark.parametrize("language", ["1", "english", "en_US", "e", ""])
    def test_invalid_language_rejected(self, language):
        with pytest.raises(ValidationError):
            SearchQuery(query="q", language=language)

    @pytest.mark.parametrize("time_range", ["day", "week", "month", "year"])
    def test_valid_time_range(self, time_range):
        assert SearchQuery(query="q", time_range=time_range).time_range == TimeRange(time_range)

    @pytest.mark.parametrize("time_range", ["hour", "2days", ""])
    def test_invalid_time_range_rejected(self, time_range):
        with pytest.raises(ValidationError):
            SearchQuery(query="q", time_range=time_range)

    @pytest.mark.parametrize("safe_search", ["off", "moderate", "strict"])
    def test_valid_safe_search(self, safe_search):
        assert SearchQuery(query="q", safe_search=safe_search).safe_search == SafeSearch(
            safe_search
        )

    @pytest.mark.parametrize("safe_search", ["loose", "ON"])
    def test_invalid_safe_search_rejected(self, safe_search):
        with pytest.raises(ValidationError):
            SearchQuery(query="q", safe_search=safe_search)

    def test_page_bounds(self):
        assert SearchQuery(query="q", page=1).page == 1
        assert SearchQuery(query="q", page=100).page == 100
        with pytest.raises(ValidationError):
            SearchQuery(query="q", page=0)
        with pytest.raises(ValidationError):
            SearchQuery(query="q", page=101)

    def test_limit_bounds(self):
        assert SearchQuery(query="q", limit=1).limit == 1
        assert SearchQuery(query="q", limit=100).limit == 100
        with pytest.raises(ValidationError):
            SearchQuery(query="q", limit=0)
        with pytest.raises(ValidationError):
            SearchQuery(query="q", limit=101)

    def test_provider_must_not_be_empty(self):
        with pytest.raises(ValidationError):
            SearchQuery(query="q", provider="")

    def test_cache_ttl_bounds(self):
        assert SearchQuery(query="q", cache_ttl_seconds=60).cache_ttl_seconds == 60
        assert SearchQuery(query="q", cache_ttl_seconds=2592000).cache_ttl_seconds == 2592000
        with pytest.raises(ValidationError):
            SearchQuery(query="q", cache_ttl_seconds=59)
        with pytest.raises(ValidationError):
            SearchQuery(query="q", cache_ttl_seconds=2592001)

    def test_sources_validation(self):
        req = SearchQuery(query="q", sources=["bing", "google"])
        assert req.sources == ["bing", "google"]
        with pytest.raises(ValidationError):
            SearchQuery(query="q", sources=["bing", "bing"])
        with pytest.raises(ValidationError):
            SearchQuery(query="q", sources=["bing", ""])
        with pytest.raises(ValidationError):
            SearchQuery(query="q", sources=["s" * 101])

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            SearchQuery(query="q", searxng_raw={"q": "q"})


# ================================================================ SearchResult
class TestSearchResult:
    def test_minimal_result(self):
        result = SearchResult(
            title="Orca study", url="https://example.com/orca", provider="example"
        )
        assert result.rank == 1
        assert result.vertical == Vertical.WEB
        assert result.snippet is None
        assert result.source is None
        assert result.thumbnail_url is None
        assert result.published_at is None
        assert result.score is None

    def test_full_result(self):
        result = SearchResult(
            title="Orca migration spotted",
            url="https://news.example.com/orcas",
            snippet="Researchers tracked a new pod.",
            rank=2,
            vertical="news",
            source="example-news",
            provider="example",
            thumbnail_url="https://img.example.com/orca.jpg",
            published_at="2024-05-01T12:30:00Z",
            score=0.93,
        )
        assert result.vertical == Vertical.NEWS
        assert result.published_at is not None
        assert result.score == 0.93

    def test_url_requires_http_scheme(self):
        with pytest.raises(ValidationError):
            SearchResult(title="t", url="example.com/no-scheme", provider="example")
        with pytest.raises(ValidationError):
            SearchResult(title="t", url="ftp://example.com/file", provider="example")

    def test_title_and_provider_required(self):
        with pytest.raises(ValidationError):
            SearchResult(title="", url="https://example.com", provider="example")
        with pytest.raises(ValidationError):
            SearchResult(title="t", url="https://example.com", provider="")

    def test_rank_must_be_positive(self):
        with pytest.raises(ValidationError):
            SearchResult(title="t", url="https://example.com", provider="example", rank=0)

    def test_invalid_thumbnail_rejected(self):
        with pytest.raises(ValidationError):
            SearchResult(
                title="t", url="https://example.com", provider="example", thumbnail_url="not-a-url"
            )

    def test_raw_provider_keys_rejected(self):
        """Raw provider dictionaries (with backend-specific keys) must not be embeddable."""
        with pytest.raises(ValidationError):
            SearchResult(
                title="t",
                url="https://example.com",
                provider="example",
                eng="bing",  # backend-specific key
            )

    def test_json_roundtrip_only_domain_fields(self):
        result = SearchResult(
            title="t",
            url="https://example.com",
            provider="example",
            published_at="2024-05-01T12:30:00Z",
        )
        dump = result.model_dump()
        assert set(dump) == {
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


# ============================================================== SearchMetadata
class TestSearchMetadata:
    def test_defaults(self):
        metadata = SearchMetadata(provider="example")
        assert metadata.providers_attempted == []
        assert metadata.fallback_occurred is False
        assert metadata.elapsed_ms == 0
        assert metadata.from_cache is False
        assert metadata.stale is False
        assert metadata.warnings == []
        assert metadata.degraded_capabilities == []
        assert metadata.page is None
        assert metadata.total_pages is None
        assert metadata.total_results is None

    def test_provider_required(self):
        with pytest.raises(ValidationError):
            SearchMetadata(provider="")  # type: ignore[call-arg]

    def test_pagination_fields(self):
        metadata = SearchMetadata(provider="example", page=2, total_pages=5, total_results=120)
        assert metadata.page == 2
        assert metadata.total_pages == 5
        assert metadata.total_results == 120

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            SearchMetadata(provider="example", raw_backend_meta={"x": 1})


# ============================================================== SearchResponse
class TestSearchResponse:
    def test_empty_response(self):
        response = SearchResponse(
            query="dolphins",
            verticals=[Vertical.WEB],
            metadata=SearchMetadata(provider="example", elapsed_ms=42),
        )
        assert response.results == []

    def test_response_with_results(self):
        result = SearchResult(
            title="Orca study", url="https://example.com/orca", rank=1, provider="example"
        )
        response = SearchResponse(
            query="dolphins",
            verticals=[Vertical.WEB],
            results=[result],
            metadata=SearchMetadata(
                provider="example",
                providers_attempted=["example"],
                elapsed_ms=120,
                from_cache=False,
                degraded_capabilities=["vertical:news"],
                warnings=["vertical 'news' not supported by provider 'example'"],
            ),
        )
        assert response.results[0].title == "Orca study"
        assert response.metadata.degraded_capabilities == ["vertical:news"]
        # The whole envelope serializes to plain domain data.
        dump = response.model_dump()
        assert "searxng" not in str(dump).lower()

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            SearchResponse(
                query="q",
                metadata=SearchMetadata(provider="example"),
                backend_raw={},
            )


# =========================================================== SearchCapabilities
class TestSearchCapabilities:
    def test_defaults(self):
        caps = SearchCapabilities(verticals=[Vertical.WEB])
        assert caps.time_ranges == []
        assert caps.supports_language is False
        assert caps.supports_safe_search is False
        assert caps.supports_paging is False
        assert caps.supports_source_selection is False
        assert caps.max_results is None
        assert caps.available is True

    def test_full_capabilities(self):
        caps = SearchCapabilities(
            verticals=["web", "news", "images"],
            time_ranges=["day", "week"],
            supports_language=True,
            supports_safe_search=True,
            supports_paging=True,
            supports_source_selection=True,
            max_results=30,
            available=False,
        )
        assert caps.verticals == [Vertical.WEB, Vertical.NEWS, Vertical.IMAGES]
        assert caps.time_ranges == [TimeRange.DAY, TimeRange.WEEK]
        assert caps.max_results == 30
        assert caps.available is False

    def test_verticals_required(self):
        with pytest.raises(ValidationError):
            SearchCapabilities(verticals=[])  # type: ignore[call-arg]

    def test_duplicate_verticals_rejected(self):
        with pytest.raises(ValidationError):
            SearchCapabilities(verticals=["web", "web"])

    def test_degradations_report_unsupported_features(self):
        caps = SearchCapabilities(verticals=[Vertical.WEB])
        request = SearchQuery(
            query="q",
            verticals=["web", "news"],
            language="en",
            time_range="day",
            safe_search="strict",
            page=2,
            limit=50,
            sources=["bing"],
        )
        degraded = caps.degradations(request)
        assert "vertical:news" in degraded
        assert "time_range" in degraded
        assert "language" in degraded
        assert "safe_search" in degraded
        assert "paging" in degraded
        assert "sources" in degraded

    def test_degradations_report_clamped_limit(self):
        caps = SearchCapabilities(verticals=[Vertical.WEB], max_results=10)
        assert caps.degradations(SearchQuery(query="q", limit=50, safe_search="off")) == ["limit"]

    def test_no_degradations_when_fully_supported(self):
        caps = SearchCapabilities(
            verticals=[Vertical.WEB, Vertical.NEWS],
            time_ranges=[TimeRange.DAY, TimeRange.WEEK],
            supports_language=True,
            supports_safe_search=True,
            supports_paging=True,
            supports_source_selection=True,
            max_results=100,
        )
        request = SearchQuery(
            query="q",
            verticals=["web", "news"],
            language="en",
            time_range="day",
            safe_search="strict",
            page=2,
            limit=50,
            sources=["bing"],
        )
        assert caps.degradations(request) == []

    def test_no_degradations_for_defaults(self):
        """A bare request against an all-capable provider degrades nothing."""
        caps = SearchCapabilities(
            verticals=[Vertical.WEB],
            supports_safe_search=True,
        )
        assert caps.degradations(SearchQuery(query="q")) == []
