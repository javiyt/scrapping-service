"""Unit tests for the transcript domain models.

Covers:

- :func:`parse_video_id` (every accepted URL form, bare IDs, rejections).
- :func:`normalize_languages` (casefolding, dedup, validation).
- :class:`TranscriptRequest` / :class:`TranscriptResponse` field validation
  and the :meth:`TranscriptResponse.from_segments` helpers.

The :mod:`app.transcript.models` contract is provider-agnostic, so these
tests exercise it directly — no providers, no I/O.
"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.transcript.models import (
    LANGUAGE_PATTERN,
    VIDEO_ID_PATTERN,
    TranscriptMetadata,
    TranscriptRequest,
    TranscriptResponse,
    TranscriptSegment,
    normalize_languages,
    parse_video_id,
)

# ------------------------------------------------------------------- video ID


class TestParseVideoId:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ&t=42",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/v/dQw4w9WgXcQ",
            "https://www.youtube.com/e/dQw4w9WgXcQ",
            "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "dQw4w9WgXcQ",  # bare ID
        ],
    )
    def test_accepts_youtube_forms(self, url: str) -> None:
        assert parse_video_id(url) == "dQw4w9WgXcQ"

    @pytest.mark.parametrize(
        "url",
        [
            "https://vimeo.com/123456789",
            "https://example.com/watch?v=dQw4w9WgXcQ",
            "ftp://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/results?search_query=dolphins",
            "https://www.youtube.com/@somechannel",
            "https://www.youtube.com/watch",  # no id
            "https://www.youtube.com/shorts/short",  # id too short
            "",
            "   ",
        ],
    )
    def test_rejects_non_youtube_and_invalid(self, url: str) -> None:
        with pytest.raises(ValidationError):
            parse_video_id(url)

    def test_bare_id_with_wrong_length_rejected(self) -> None:
        with pytest.raises(ValidationError):
            parse_video_id("dQw4w9WgXc")  # 10 chars

    def test_whitespace_is_stripped(self) -> None:
        assert parse_video_id("  dQw4w9WgXcQ  ") == "dQw4w9WgXcQ"
        assert parse_video_id("  https://youtu.be/dQw4w9WgXcQ  ") == "dQw4w9WgXcQ"


# ----------------------------------------------------------------- languages


class TestNormalizeLanguages:
    def test_casefolds_and_preserves_order(self) -> None:
        assert normalize_languages(["ES", "en-US", "de"]) == ["es", "en-us", "de"]

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValidationError):
            normalize_languages([])

    def test_rejects_malformed_tags(self) -> None:
        for bad in ["", "   ", "not a tag", "1234", "en-", "es-"]:
            with pytest.raises(ValidationError):
                normalize_languages([bad])

    def test_rejects_duplicates(self) -> None:
        with pytest.raises(ValidationError):
            normalize_languages(["es", "ES"])

    def test_valid_bcp47_tags(self) -> None:
        assert normalize_languages(["en", "en-US", "pt-BR", "fr"]) == ["en", "en-us", "pt-br", "fr"]

    def test_language_pattern_matches_bcp47(self) -> None:
        assert LANGUAGE_PATTERN.match("en")
        assert LANGUAGE_PATTERN.match("en-US")
        assert LANGUAGE_PATTERN.match("pt-BR-Porto")
        assert not LANGUAGE_PATTERN.match("1en")
        assert not LANGUAGE_PATTERN.match("en-")


# ------------------------------------------------------------------- request


def _request(**overrides: object) -> TranscriptRequest:
    base: dict = {"video_id": "dQw4w9WgXcQ", "languages": ["en"]}
    base.update(overrides)
    return TranscriptRequest(**base)


class TestTranscriptRequest:
    def test_defaults(self) -> None:
        request = _request()
        assert request.provider == "auto"
        assert request.cache_ttl_seconds is None
        assert request.force_refresh is False
        assert request.url is None

    def test_invalid_video_id_rejected(self) -> None:
        with pytest.raises(Exception):
            _request(video_id="not-a-video-id")

    def test_rejects_invalid_language(self) -> None:
        with pytest.raises(Exception):
            _request(languages=["not!!a-language"])

    def test_rejects_duplicate_languages(self) -> None:
        with pytest.raises(Exception):
            _request(languages=["en", "en"])

    def test_rejects_empty_languages(self) -> None:
        with pytest.raises(Exception):
            _request(languages=[])

    def test_rejects_out_of_range_ttl(self) -> None:
        with pytest.raises(Exception):
            _request(cache_ttl_seconds=1)
        with pytest.raises(Exception):
            _request(cache_ttl_seconds=99999999)

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(Exception):
            _request(extra_field="nope")  # type: ignore[call-arg]


# ------------------------------------------------------------------ response


def _metadata(provider: str = "fake") -> TranscriptMetadata:
    return TranscriptMetadata(provider=provider)


def _segment(start: float, text: str, duration: float | None = None) -> TranscriptSegment:
    return TranscriptSegment(start=start, duration=duration, text=text)


class TestTranscriptResponse:
    def test_from_segments_derives_text_and_words(self) -> None:
        response = TranscriptResponse.from_segments(
            video_id="dQw4w9WgXcQ",
            segments=[
                _segment(0.0, "Hello  world", 2.0),
                _segment(2.0, "how are you?"),
            ],
            provider="fake",
            metadata=_metadata(),
        )
        assert response.text == "Hello world how are you?"
        assert response.total_words == 5
        assert response.provider == "fake"
        assert response.metadata.provider == "fake"

    def test_explicit_text_is_used(self) -> None:
        response = TranscriptResponse.from_segments(
            video_id="dQw4w9WgXcQ",
            segments=[_segment(0.0, "x")],
            provider="fake",
            metadata=_metadata(),
            text="custom text",
        )
        assert response.text == "custom text"
        assert response.total_words == 2

    def test_empty_segments(self) -> None:
        response = TranscriptResponse.from_segments(
            video_id="dQw4w9WgXcQ",
            segments=[],
            provider="fake",
            metadata=_metadata(),
        )
        assert response.text == ""
        assert response.total_words == 0

    def test_invalid_video_id_rejected(self) -> None:
        with pytest.raises(Exception):
            TranscriptResponse.from_segments(
                video_id="badid",
                segments=[],
                provider="fake",
                metadata=_metadata(),
            )

    def test_video_id_pattern(self) -> None:
        assert VIDEO_ID_PATTERN.match("dQw4w9WgXcQ")
        assert VIDEO_ID_PATTERN.match("aA098765432")
        assert not VIDEO_ID_PATTERN.match("short")


# --------------------------------------------------------------- round-trip


class TestModelRoundTrip:
    def test_response_serializes_and_validates(self) -> None:
        response = TranscriptResponse.from_segments(
            video_id="dQw4w9WgXcQ",
            segments=[_segment(0.5, "hi", 1.5), _segment(2.0, "there")],
            provider="fake",
            metadata=_metadata(),
            title="A title",
            language="en",
            is_generated=True,
        )
        restored = TranscriptResponse.model_validate_json(response.model_dump_json())
        assert restored == response

    def test_segment_rejects_negative_start(self) -> None:
        with pytest.raises(Exception):
            TranscriptSegment(start=-1, text="x")

    def test_metadata_defaults(self) -> None:
        metadata = _metadata()
        assert metadata.providers_attempted == []
        assert metadata.fallback_occurred is False
        assert metadata.from_cache is False
        assert metadata.stale is False
        assert metadata.cached_at is None
        assert metadata.cache_age_seconds is None
        assert metadata.warnings == []
