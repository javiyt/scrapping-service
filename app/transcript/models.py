"""Provider-agnostic YouTube transcript domain models.

This module defines the stable, provider-agnostic transcript contract owned
by the service:

- :class:`TranscriptRequest` — normalized transcript request (video ID +
  preferred languages + routing hints).
- :class:`TranscriptSegment` — a single normalized caption segment.
- :class:`TranscriptMetadata` — response metadata (provider, timing, cache).
- :class:`TranscriptResponse` — the normalized transcript envelope.

Design principles (mirroring :mod:`app.search.models`):

- The service owns the public transcript contract.
- Providers adapt to this contract; clients never adapt to providers.
- Raw provider request/response objects are never exposed.
- Unsupported features are represented explicitly through the errors in
  :mod:`app.core.errors` (no captions, language unavailable, ...).
- Transcript is separate from scraping and from search.

Video identification
-------------------
:class:`parse_video_id` accepts every common YouTube URL form plus a bare
11-character video ID and rejects anything that is not YouTube with
:class:`~app.core.errors.ValidationError`.

All models reject unknown fields so raw provider payloads can never leak
into the domain.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import ValidationError

# ================================================================== video ID
#: Canonical YouTube video ID: 11 characters from the URL-safe alphabet.
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")

#: BCP-47 style language tag (e.g. ``es``, ``en-US``).
LANGUAGE_PATTERN = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$")

#: YouTube hosts that may legally carry a video reference (with or without
#: ``www.`` / other subdomains).
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
        "youtu.be",
        "i.ytimg.com",  # thumbnail-only host — accepted, but has no video path
    }
)

#: Path forms on youtube.com-style hosts that carry a video reference.
_PATH_VIDEO_ID = re.compile(r"^/(?:v|e|embed|shorts)/([A-Za-z0-9_-]{11})(?:/|$|\?)")


def parse_video_id(url_or_id: str) -> str:
    """Extract and validate a YouTube video ID from a URL or a bare ID.

    Accepted forms:

    - ``https://www.youtube.com/watch?v=VIDEO_ID`` (and any extra query params)
    - ``https://youtu.be/VIDEO_ID``
    - ``https://www.youtube.com/shorts/VIDEO_ID``
    - ``https://www.youtube.com/embed/VIDEO_ID``
    - ``https://www.youtube.com/v/VIDEO_ID``, ``.../e/VIDEO_ID``
    - a bare 11-character video ID (``[A-Za-z0-9_-]{11}``)

    Raises:
        ValidationError: if the input is not a YouTube URL (other hosts,
            ``/live`` without a ``v=`` param, ``/results``/channel URLs,
            non-http schemes) or contains no valid 11-character video ID.
    """
    text = url_or_id.strip()
    if not text:
        raise ValidationError(
            "No video URL or ID provided",
            details={"field": "url"},
        )

    # Bare video ID (no scheme, no slash).
    if "/" not in text and "://" not in text:
        if VIDEO_ID_PATTERN.match(text):
            return text
        raise ValidationError(
            "Not a valid YouTube video ID or URL",
            details={"field": "url", "reason": "invalid video id"},
        )

    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise ValidationError(
            "Only http(s) YouTube URLs are supported",
            details={"field": "url", "reason": "unsupported scheme"},
        )

    host = parsed.netloc.lower().split(":")[0]
    # Accept any youtube*.com / youtu.be host.
    if not (
        host == "youtu.be"
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtube-nocookie.com"
        or host.endswith(".youtube-nocookie.com")
    ):
        raise ValidationError(
            "The URL does not point at YouTube",
            details={"field": "url", "reason": "non-youtube host", "host": host},
        )

    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0].split("?")[0]
        if not VIDEO_ID_PATTERN.match(video_id):
            raise ValidationError(
                "Could not extract a video ID from the URL",
                details={"field": "url", "reason": "no video id"},
            )
        return video_id

    # youtube.com-style hosts: the ID may live in the path or in a query param.
    match = _PATH_VIDEO_ID.match(parsed.path)
    if match:
        return match.group(1)

    for param in ("v", "i"):
        values = parse_qs(parsed.query).get(param)
        if values and VIDEO_ID_PATTERN.match(values[0]):
            return values[0]

    raise ValidationError(
        "Could not extract a video ID from the URL",
        details={"field": "url", "reason": "no video id"},
    )


# ================================================================ language
def normalize_languages(raw: list[str]) -> list[str]:
    """Validate, deduplicate and normalize the requested language list.

    Language tags are casefolded (``ES`` → ``es``, ``EN-US`` → ``en-us``)
    and duplicates (case-insensitive) are removed, preserving order.

    Raises:
        ValidationError: on an empty list, an invalid tag, or duplicates.
    """
    if not raw:
        raise ValidationError(
            "At least one language is required",
            details={"field": "lang"},
        )
    seen: set[str] = set()
    normalized: list[str] = []
    for tag in raw:
        tag = tag.strip()
        if not tag or not LANGUAGE_PATTERN.match(tag):
            raise ValidationError(
                f"Invalid language tag: '{tag}'",
                details={"field": "lang", "language": tag},
            )
        lowered = tag.casefold()
        if lowered in seen:
            raise ValidationError(
                f"Duplicate language: '{tag}'",
                details={"field": "lang", "language": lowered},
            )
        seen.add(lowered)
        normalized.append(lowered)
    return normalized


# ============================================================== TranscriptSegment
class TranscriptSegment(BaseModel):
    """One normalized caption segment.

    Times are in seconds.  ``duration`` is optional: providers that only
    report a start offset leave it ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    start: float = Field(..., ge=0, description="Segment start time in seconds.")
    duration: float | None = Field(
        default=None,
        ge=0,
        description="Segment duration in seconds, when the provider reports it.",
    )
    text: str = Field(..., description="Segment text (raw caption text).")


# ================================================================ TranscriptMetadata
class TranscriptMetadata(BaseModel):
    """Metadata attached to every transcript response."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Name of the provider that produced this response.",
    )
    providers_attempted: list[str] = Field(
        default_factory=list,
        description="Providers attempted in order, including failed ones.",
    )
    fallback_occurred: bool = Field(
        default=False,
        description=(
            "``True`` when the router fell back to another provider after a "
            "retryable failure. ``False`` when the selected provider answered "
            "on the first attempt (always ``False`` for explicit-provider "
            "requests, which never fall back)."
        ),
    )
    elapsed_ms: int = Field(default=0, ge=0, description="Serving time in milliseconds.")
    from_cache: bool = Field(
        default=False,
        description="``True`` when the response was served from cache.",
    )
    stale: bool = Field(
        default=False,
        description=(
            "``True`` when a (possibly expired) cached entry was served "
            "because the live fetch failed with a retryable provider error."
        ),
    )
    cached_at: str | None = Field(
        default=None,
        description=(
            "ISO-8601 timestamp of the cached entry serving this response, "
            "when ``from_cache`` is ``True``. ``None`` for fresh responses."
        ),
    )
    cache_age_seconds: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Age of the cached entry in seconds when ``from_cache`` is "
            "``True``. ``None`` for fresh responses."
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Human-readable warnings (fallbacks, stale serves, degraded "
            "request parts such as an unsupported language)."
        ),
    )


# ================================================================ TranscriptRequest
class TranscriptRequest(BaseModel):
    """Normalized, provider-agnostic transcript request.

    Internal canonical form consumed by the router, cache and providers.
    The video is referenced by its canonical 11-character ID: providers
    never see the caller's URL, and two URL spellings of the same video
    share one canonical identity.
    """

    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(
        ...,
        pattern=VIDEO_ID_PATTERN,
        description="Canonical 11-character YouTube video ID.",
    )
    url: str | None = Field(
        default=None,
        max_length=2048,
        description="The original URL as provided by the caller (for logging only).",
    )
    languages: list[str] = Field(
        ...,
        min_length=1,
        max_length=20,
        description=(
            "Preferred caption languages in priority order (BCP-47 style, "
            "lowercased). The first language the video offers is served."
        ),
    )
    provider: str = Field(
        default="auto",
        min_length=1,
        max_length=100,
        description=(
            "Provider name to use, or ``auto`` (the default) to let the "
            "service pick an available provider in fallback order."
        ),
    )
    cache_ttl_seconds: int | None = Field(
        default=None,
        ge=60,
        le=2592000,
        description="Per-request cache TTL in seconds (60 s – 30 d).",
    )
    force_refresh: bool = Field(
        default=False,
        description="If ``True``, bypass the cache and query the provider fresh.",
    )

    @field_validator("languages")
    @classmethod
    def _validate_languages(cls, v: list[str]) -> list[str]:
        for tag in v:
            if not tag or not LANGUAGE_PATTERN.match(tag):
                raise ValueError(f"Invalid language tag: '{tag}'")
            if len(tag) > 35:
                raise ValueError("Language tag too long")
        if len(set(v)) != len(v):
            raise ValueError("languages must not contain duplicates")
        return v


# ================================================================ TranscriptResponse
class TranscriptResponse(BaseModel):
    """Normalized transcript response envelope (the public contract)."""

    model_config = ConfigDict(extra="forbid")

    def clone(self) -> TranscriptResponse:
        """Return a deep copy so in-place metadata stamping never aliases."""
        return TranscriptResponse.model_validate(self.model_dump())

    video_id: str = Field(
        ...,
        pattern=VIDEO_ID_PATTERN,
        description="Canonical 11-character YouTube video ID.",
    )
    title: str | None = Field(
        default=None,
        description="Video title, when the provider reports it.",
    )
    language: str | None = Field(
        default=None,
        description=(
            "Language of the served captions (the language actually used). "
            "``None`` when the provider does not report one."
        ),
    )
    is_generated: bool | None = Field(
        default=None,
        description=(
            "``True`` for auto-generated captions, ``False`` for manual ones, "
            "``None`` when the provider does not report it."
        ),
    )
    provider: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Name of the provider that produced this transcript.",
    )
    segments: list[TranscriptSegment] = Field(
        default_factory=list,
        description="Normalized caption segments in temporal order.",
    )
    text: str = Field(
        default="",
        description="The full transcript as plain text (segments joined).",
    )
    total_words: int = Field(
        default=0,
        ge=0,
        description="Word count of ``text``.",
    )
    metadata: TranscriptMetadata = Field(
        ...,
        description="Response metadata (provider, timing, cache, warnings).",
    )

    @classmethod
    def from_segments(
        cls,
        *,
        video_id: str,
        segments: list[TranscriptSegment],
        provider: str,
        metadata: TranscriptMetadata,
        title: str | None = None,
        language: str | None = None,
        is_generated: bool | None = None,
        text: str | None = None,
    ) -> TranscriptResponse:
        """Build a response, deriving ``text``/``total_words`` if not given.

        ``text`` defaults to the segment texts joined with single spaces
        (newlines preserved inside segments); ``total_words`` counts the
        whitespace-split words of ``text``.
        """
        if text is None:
            text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
            text = re.sub(r"[ \t]+", " ", text).strip()
        return cls(
            video_id=video_id,
            title=title,
            language=language,
            is_generated=is_generated,
            provider=provider,
            segments=segments,
            text=text,
            total_words=len(text.split()) if text else 0,
            metadata=metadata,
        )
