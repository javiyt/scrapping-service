"""Unit tests for the transcript provider contract and registry.

The :class:`app.transcript.TranscriptProvider` protocol is structural, so
tests use plain fakes.  The registry enforces provider identity rules
(ordered, unique, non-empty names).
"""

from __future__ import annotations

import pytest

from app.transcript.contract import TranscriptHealth, TranscriptProvider
from app.transcript.models import TranscriptMetadata, TranscriptResponse, TranscriptSegment
from app.transcript.registry import TranscriptProviderRegistry


class FakeTranscriptProvider:
    """In-memory provider satisfying the structural TranscriptProvider protocol."""

    def __init__(
        self,
        name: str = "fake",
        available: bool = True,
        reason: str | None = None,
        fetch_error: Exception | None = None,
        slow: bool = False,
    ) -> None:
        self._name = name
        self._available = available
        self._reason = reason
        self._fetch_error = fetch_error
        self._slow = slow
        self.fetch_calls: list[tuple[str, list[str]]] = []
        self.health_calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def health(self) -> TranscriptHealth:
        self.health_calls += 1
        if self._slow:
            await _sleep()
        return TranscriptHealth(available=self._available, reason=self._reason)

    async def fetch(self, video_id: str, languages: list[str]) -> TranscriptResponse:
        self.fetch_calls.append((video_id, list(languages)))
        if self._fetch_error is not None:
            raise self._fetch_error
        return TranscriptResponse.from_segments(
            video_id=video_id,
            segments=[
                TranscriptSegment(
                    start=0.0,
                    duration=1.0,
                    text=f"{self._name} transcript for {video_id} ({languages[0]})"
                    if languages
                    else f"{self._name} transcript for {video_id}",
                )
            ],
            provider=self._name,
            metadata=TranscriptMetadata(provider=self._name),
            language=languages[0] if languages else None,
        )


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(0)


# --------------------------------------------------------------------- contract


class TestTranscriptHealth:
    def test_defaults(self) -> None:
        health = TranscriptHealth(available=True)
        assert health.available is True
        assert health.reason is None

    def test_unavailable_with_reason(self) -> None:
        health = TranscriptHealth(available=False, reason="upstream offline")
        assert health.available is False
        assert health.reason == "upstream offline"

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(Exception):
            TranscriptHealth(available=True, bogus="x")  # type: ignore[call-arg]


class TestProviderProtocol:
    async def test_health_returns_model(self) -> None:
        provider: TranscriptProvider = FakeTranscriptProvider(name="fake")
        health = await provider.health()
        assert isinstance(health, TranscriptHealth)

    async def test_fetch_returns_normalized_response(self) -> None:
        provider = FakeTranscriptProvider(name="fake")
        response = await provider.fetch("dQw4w9WgXcQ", ["en"])
        assert response.provider == "fake"
        assert response.metadata.provider == "fake"
        assert len(response.segments) == 1


# --------------------------------------------------------------------- registry


class TestRegistry:
    def test_empty_registry(self) -> None:
        registry = TranscriptProviderRegistry([])
        assert registry.all() == []
        assert registry.names == []
        assert "x" not in registry
        assert registry.get("x") is None

    def test_register_and_get(self) -> None:
        alpha = FakeTranscriptProvider(name="alpha")
        beta = FakeTranscriptProvider(name="beta")
        registry = TranscriptProviderRegistry([alpha, beta])
        assert registry.get("alpha") is alpha
        assert registry.get("beta") is beta
        assert registry.names == ["alpha", "beta"]

    def test_preserves_order(self) -> None:
        registry = TranscriptProviderRegistry(
            [
                FakeTranscriptProvider(name="a"),
                FakeTranscriptProvider(name="b"),
                FakeTranscriptProvider(name="c"),
            ]
        )
        assert [provider.name for provider in registry.all()] == ["a", "b", "c"]

    def test_duplicate_names_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            TranscriptProviderRegistry(
                [FakeTranscriptProvider(name="x"), FakeTranscriptProvider(name="x")]
            )

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            TranscriptProviderRegistry([FakeTranscriptProvider(name="")])

    def test_whitespace_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            TranscriptProviderRegistry([FakeTranscriptProvider(name="   ")])
