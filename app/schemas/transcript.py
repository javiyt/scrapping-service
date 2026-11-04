"""Public request and response schemas for the transcript endpoints.

These schemas are the **service-owned** contract of ``GET /v1/transcript``.
They are provider-agnostic by design:

- No Camofox / ``youtube-transcript-api`` / ``yt-dlp`` parameter names
  appear; the request is the canonical video URL/ID plus preferred
  languages.
- :meth:`TranscriptQueryParams.to_domain` maps the public query parameters
  onto the internal :class:`app.transcript.models.TranscriptRequest` that
  the :class:`app.transcript.cache.TranscriptCache` (and thus the router
  and the providers) consumes.
- :class:`TranscriptApiResponse` is the project-owned response envelope
  (video ID, title, language, is_generated, provider, segments, text,
  total words, metadata); raw provider payloads never appear.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.transcript.models import (
    TranscriptRequest,
    TranscriptResponse,
    normalize_languages,
    parse_video_id,
)


# ================================================================== request
class TranscriptQueryParams(BaseModel):
    """Query parameters for ``GET /v1/transcript``.

    ``url`` (or ``video_id``) identifies the video; ``lang`` is a
    comma-separated list of preferred caption languages in priority order
    (e.g. ``es,en``); ``provider`` targets one provider explicitly or
    ``auto`` (the default) for the ordered fallback chain.
    """

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
        description=(
            "YouTube video URL (``watch?v=``, ``youtu.be/``, ``/shorts/``, "
            "``/embed/``) or a bare 11-character video ID."
        ),
    )
    video_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{11}$",
        description="A bare 11-character YouTube video ID. Ignored when ``url`` is set.",
    )
    lang: str = Field(
        default="",
        max_length=256,
        description=(
            "Comma-separated preferred caption languages in priority order, "
            "BCP-47 style (e.g. ``es,en``). The first language the video "
            "offers is served."
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
        description="If ``true``, bypass the cache and query the provider fresh.",
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("url must not be empty")
        return v

    def to_domain(self) -> TranscriptRequest:
        """Map the public parameters onto the internal domain request.

        Raises:
            app.core.errors.ValidationError: if no video is identified or
                the video is not a YouTube reference (non-YouTube hosts,
                unparseable URLs), or if the language list is invalid
                (empty, malformed tag, duplicates).
        """
        raw = self.url if self.url is not None else self.video_id
        if raw is None:
            from app.core.errors import ValidationError

            raise ValidationError(
                "Either 'url' or 'video_id' is required",
                details={"field": "url"},
            )
        video_id = parse_video_id(raw)

        languages = (
            normalize_languages([tag for tag in self.lang.split(",") if tag.strip()])
            if self.lang.strip()
            else ["en"]
        )

        return TranscriptRequest(
            video_id=video_id,
            url=self.url,
            languages=languages,
            provider=self.provider.strip() or "auto",
            cache_ttl_seconds=self.cache_ttl_seconds,
            force_refresh=self.force_refresh,
        )


# ================================================================ responses
class TranscriptApiSegment(BaseModel):
    """One normalized caption segment in the public response shape."""

    model_config = ConfigDict(extra="forbid")

    start: float = Field(..., ge=0, description="Segment start time in seconds.")
    duration: float | None = Field(
        default=None, ge=0, description="Segment duration in seconds, when known."
    )
    text: str = Field(..., description="Segment text.")


class TranscriptApiMetadata(BaseModel):
    """Metadata attached to every transcript response."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(..., description="Provider that produced this transcript.")
    providers_attempted: list[str] = Field(
        default_factory=list,
        description="Providers attempted in order, including failed ones.",
    )
    fallback_occurred: bool = Field(
        default=False,
        description="True when the router fell back after a retryable failure.",
    )
    elapsed_ms: int = Field(
        default=0, ge=0, description="Time spent serving this response, in milliseconds."
    )
    from_cache: bool = Field(
        default=False,
        description="True when the response was served from the transcript cache.",
    )
    stale: bool = Field(
        default=False,
        description=(
            "True when a (possibly expired) cached entry was served because "
            "the live fetch failed with a retryable provider error."
        ),
    )
    cached_at: str | None = Field(
        default=None, description="When the serving cache entry was created, if from cache."
    )
    cache_age_seconds: float | None = Field(
        default=None, ge=0, description="Age of the serving cache entry, if from cache."
    )
    warnings: list[str] = Field(
        default_factory=list, description="Human-readable warnings (fallbacks, stale serves)."
    )
    auth_profile: str | None = Field(
        default=None, description="Authenticated profile name, only when exposed."
    )


class TranscriptApiResponse(BaseModel):
    """Normalized transcript response — the public contract of ``/v1/transcript``."""

    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(..., description="Canonical 11-character YouTube video ID.")
    title: str | None = Field(default=None, description="Video title, when known.")
    language: str | None = Field(
        default=None, description="Language of the served captions, when known."
    )
    is_generated: bool | None = Field(
        default=None,
        description=(
            "True for auto-generated captions, false for manual captions, "
            "null when the provider does not report it."
        ),
    )
    provider: str = Field(..., description="Provider that produced this transcript.")
    segments: list[TranscriptApiSegment] = Field(
        default_factory=list, description="Normalized caption segments in temporal order."
    )
    text: str = Field(..., description="The full transcript as plain text.")
    total_words: int = Field(..., ge=0, description="Word count of the transcript text.")
    metadata: TranscriptApiMetadata = Field(
        ..., description="Response metadata (provider, timing, cache, warnings)."
    )

    @classmethod
    def from_domain(cls, response: TranscriptResponse) -> TranscriptApiResponse:
        """Map the domain response onto the public response shape."""
        return cls(
            video_id=response.video_id,
            title=response.title,
            language=response.language,
            is_generated=response.is_generated,
            provider=response.provider,
            segments=[
                TranscriptApiSegment.model_validate(segment.model_dump())
                for segment in response.segments
            ],
            text=response.text,
            total_words=response.total_words,
            metadata=TranscriptApiMetadata(
                provider=response.metadata.provider,
                providers_attempted=response.metadata.providers_attempted,
                fallback_occurred=response.metadata.fallback_occurred,
                elapsed_ms=response.metadata.elapsed_ms,
                from_cache=response.metadata.from_cache,
                stale=response.metadata.stale,
                cached_at=response.metadata.cached_at,
                cache_age_seconds=response.metadata.cache_age_seconds,
                warnings=response.metadata.warnings,
            ),
        )

    def with_auth_profile(self, auth_profile: str | None) -> TranscriptApiResponse:
        """Return a copy with the authenticated profile name in metadata."""
        return self.model_copy(
            update={
                "metadata": TranscriptApiMetadata(
                    **self.metadata.model_dump(),
                    auth_profile=auth_profile,
                )
            }
        )
