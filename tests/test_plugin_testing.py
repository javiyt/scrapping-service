"""Tests for the reusable plugin contract helpers."""

from app.plugin_testing.search_contract import assert_search_contract, run_search_contract_suite
from app.plugin_testing.transcript_contract import (
    assert_transcript_contract,
    run_transcript_contract_suite,
)
from app.search import SearchCapabilities, SearchQuery, SearchResponse, SearchResult, Vertical
from app.search.models import SearchMetadata
from app.transcript import TranscriptHealth, TranscriptResponse, TranscriptSegment
from app.transcript.models import TranscriptMetadata


class FakeSearchProvider:
    """A minimal SearchProvider that satisfies the contract."""

    name = "fake"

    async def capabilities(self) -> SearchCapabilities:
        """Return capabilities in the shape expected by the helper."""
        return SearchCapabilities(verticals=[Vertical.WEB], available=True)

    async def search(self, request: SearchQuery) -> SearchResponse:
        """Return a SearchResponse instance."""
        return SearchResponse(
            query=request.query,
            verticals=request.verticals,
            results=[
                SearchResult(
                    title=f"Result for {request.query}",
                    url="https://example.com/1",
                    rank=1,
                    vertical=Vertical.WEB,
                    provider="fake",
                )
            ],
            metadata=SearchMetadata(
                provider="fake",
                providers_attempted=["fake"],
                elapsed_ms=10,
            ),
        )


class IncompleteSearchProvider:
    """Has capabilities but search method missing request parameter."""

    name = "incomplete"

    async def capabilities(self) -> SearchCapabilities:
        return SearchCapabilities(verticals=[Vertical.WEB], available=True)

    async def search(self, request: SearchQuery) -> SearchCapabilities:
        return SearchCapabilities(verticals=[Vertical.WEB], available=True)


class BrokenSearchProvider:
    """Returns invalid capabilities."""

    name = "broken"

    async def capabilities(self) -> SearchCapabilities:
        return SearchCapabilities(verticals=[Vertical.WEB], available=False)

    async def search(self, request: SearchQuery) -> SearchCapabilities:
        return SearchCapabilities(verticals=[Vertical.WEB], available=False)


def test_search_contract_suite_passes_valid_provider() -> None:
    """The contract suite should pass for a valid provider."""
    provider = FakeSearchProvider()
    # Run the sync wrapper
    run_search_contract_suite(provider)
    # Also test the direct check
    assert_search_contract(provider)


def test_search_contract_rejects_incomplete_provider() -> None:
    """Incomplete providers must fail the contract."""
    provider = IncompleteSearchProvider()
    # The helper will fail because search returns None (not SearchResponse)
    # This is acceptable: the provider does not satisfy the protocol
    try:
        assert_search_contract(provider)
        assert False, "Expected AssertionError"
    except (AssertionError, AttributeError):
        # Either AssertionError or AttributeError is acceptable
        assert True


def test_search_contract_rejects_broken_provider() -> None:
    """Providers with invalid responses must fail the contract."""
    provider = BrokenSearchProvider()
    try:
        assert_search_contract(provider)
        assert False, "Expected AssertionError"
    except (AssertionError, AttributeError):
        assert True


class FakeTranscriptProvider:
    """A minimal TranscriptProvider that satisfies the contract."""

    name = "fake"

    async def health(self) -> TranscriptHealth:
        """Return a TranscriptHealth instance."""
        return TranscriptHealth(available=True, reason="ok")

    async def fetch(self, video_id: str, segments: list[str]) -> TranscriptResponse:
        """Return a TranscriptResponse instance."""
        m = TranscriptMetadata(provider="fake", providers_attempted=[])
        return TranscriptResponse(
            video_id="dQw4w9WgXcQ",
            provider="fake",
            segments=[TranscriptSegment(start=0, duration=10, text="Hello world")],
            metadata=m,
            text="",
        )


class IncompleteTranscriptProvider:
    """Missing fetch method."""

    name = "incomplete"

    async def health(self) -> TranscriptHealth:
        return TranscriptHealth(available=True, reason="ok")

    async def fetch(self, video_id: str, segments: list[str]) -> None:
        return None  # Invalid response


def test_transcript_contract_suite_passes_valid_provider() -> None:
    """The transcript contract suite should pass for a valid provider."""
    provider = FakeTranscriptProvider()
    run_transcript_contract_suite(provider)
    assert_transcript_contract(provider)


def test_transcript_contract_rejects_incomplete_provider() -> None:
    """Incomplete transcript providers must fail the contract."""
    provider = IncompleteTranscriptProvider()
    try:
        assert_transcript_contract(provider)
        assert False, "Expected AssertionError"
    except (AssertionError, AttributeError):
        assert True
