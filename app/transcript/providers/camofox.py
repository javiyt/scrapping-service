"""Camofox-backed YouTube transcript provider.

Adapts the external Camofox browser service (``POST /youtube/transcript``)
to the provider-agnostic :class:`~app.transcript.contract.
TranscriptProvider` contract.  Camofox drives ``yt-dlp`` (fast, no browser)
when available and otherwise falls back to a slower browser-intercept
method; this provider treats both as the same black box and only consumes
the normalized result.

Responsibilities:

- :func:`parse_transcript_segments` — turn Camofox's ``[mm:ss]`` /
  ``[hh:mm:ss]`` plain-text transcript into normalized
  :class:`~app.transcript.models.TranscriptSegment` objects.  Multi-line
  caption entries (a line without a timestamp continues the previous
  segment) and malformed lines (which are dropped, never raised) are
  handled.
- :class:`CamofoxTranscriptProvider` — a :class:`TranscriptProvider`
  implementation.  ``health()`` reuses the shared Camofox health probe
  (``available=False`` when Camofox is disabled/unconfigured, without
  any upstream text).  ``fetch()`` maps upstream failures onto the
  project transcript errors.

Configuration follows the project convention: the *connection* settings
(base URL, keys, per-request timeout) live in the shared top-level
``camofox`` section (parsed into a :class:`app.camofox.CamofoxConfig`);
this provider reads its own policy options (``enabled``, the transcript
``timeout_seconds``, ``enabled_health_probe``) from the
``transcript.providers.camofox`` YAML block, overridable with
``SCRAPER_TRANSCRIPT_CAMOFOX_*`` environment variables.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.camofox.config import CamofoxConfig
from app.camofox.types import CamofoxHealthStatus
from app.core.config import Settings
from app.core.errors import (
    CamofoxAuthError,
    CamofoxError,
    CamofoxInvalidResponseError,
    CamofoxTimeoutError,
    CamofoxUnavailableError,
    NoTranscriptAvailableError,
    TranscriptLanguageUnavailableError,
    TranscriptProviderBlockedError,
    TranscriptProviderInvalidResponseError,
    TranscriptProviderTimeoutError,
    TranscriptProviderUnavailableError,
    ValidationError,
    VideoUnavailableError,
)
from app.transcript.contract import TranscriptHealth
from app.transcript.models import (
    TranscriptMetadata,
    TranscriptResponse,
    TranscriptSegment,
)

__all__ = [
    "CamofoxTranscriptProvider",
    "CamofoxTranscriptProviderConfig",
    "NAME",
    "parse_transcript_segments",
]

logger = logging.getLogger("scraper-api.transcript.camofox")

NAME = "camofox"

#: Environment-variable prefix for the provider-level transcript options.
ENV_PREFIX = "SCRAPER_TRANSCRIPT_CAMOFOX_"

#: Transcript text lines are ``[mm:ss]`` or ``[hh:mm:ss]`` followed by text.
_TIMESTAMP_LINE = re.compile(r"^\[(?P<ts>\d{1,3}(?::\d{1,2}){1,2})\]\s*")

#: Upstream (Camofox) error-message markers, matched case-insensitively.
_NO_CAPTIONS_MARKERS = ("no captions", "no subtitles", "could not find", "no transcript")
_VIDEO_GONE_MARKERS = ("private", "deleted", "not available", "unavailable", "removed")
_BLOCK_MARKERS = ("blocked", "forbidden", "rate limit", "too many requests")


# ==========================================================================
# ``[mm:ss]`` transcript parsing
# ==========================================================================


def _timestamp_to_seconds(timestamp: str) -> float | None:
    """Convert a ``[mm:ss]`` / ``[hh:mm:ss]`` timestamp to seconds.

    Returns ``None`` for a malformed timestamp (wrong number of parts).
    """
    parts = timestamp.split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        minutes, seconds = numbers
    elif len(numbers) == 3:
        hours, minutes, seconds = numbers
    else:
        return None
    if seconds >= 60 or minutes >= 60:
        return None
    return (hours if len(numbers) == 3 else 0) * 3600 + minutes * 60 + seconds


def parse_transcript_segments(text: str) -> list[TranscriptSegment]:
    """Parse a Camofox transcript into normalized segments.

    The transcript is a plain text of lines, each either::

        [mm:ss] some caption text
        [1:02:03] some caption text        (hours form for long videos)

    or a *continuation* line (no timestamp), which belongs to the previous
    timestamped entry: a single caption can span multiple lines.  Each
    timestamped entry becomes one :class:`TranscriptSegment`; its
    ``duration`` is the gap to the next segment (``None`` for the last).

    Malformed lines (timestamps that cannot be parsed, text before the
    first timestamp) are skipped — they never fail the whole transcript.
    """
    segments: list[TranscriptSegment] = []
    current_parts: list[str] = []
    current_start: float | None = None

    def _flush() -> None:
        if current_start is None:
            return
        text_body = " ".join(part for part in current_parts if part)
        if not text_body.strip():
            return
        segments.append(TranscriptSegment(start=current_start, duration=None, text=text_body))

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _TIMESTAMP_LINE.match(stripped)
        if match is None:
            # Continuation of the previous caption (multi-line entry).
            if current_start is not None:
                current_parts.append(stripped)
            # Otherwise: a line with no timestamp before any caption
            # started — malformed; skip it.
            continue
        _flush()
        current_start = _timestamp_to_seconds(match.group("ts"))
        if current_start is None:
            # Unparseable timestamp (e.g. 4 parts, 90:99) — skip the entry.
            current_start = None
            current_parts = []
            continue
        current_parts = [stripped[match.end() :].strip()]

    _flush()

    for index, segment in enumerate(segments):
        if index + 1 < len(segments):
            segment.duration = max(0.0, segments[index + 1].start - segment.start)
    return segments


# ==========================================================================
# Provider configuration
# ==========================================================================


class CamofoxTranscriptProviderConfig(BaseModel):
    """Provider-level options for the Camofox transcript provider.

    Parsed from the ``transcript.providers.camofox`` YAML block, with
    ``SCRAPER_TRANSCRIPT_CAMOFOX_*`` environment overrides::

        transcript:
          providers:
            camofox:
              enabled: true
              timeout_seconds: 120
              health_probe: true

    Connection settings (base URL, keys, ``camofox.timeout_seconds``) live
    in the shared ``camofox`` section; ``timeout_seconds`` here only
    *overrides* that shared timeout for transcript requests.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Enable the Camofox transcript provider.  Requires the shared "
            "``camofox`` section to be enabled (or a client supplied)."
        ),
    )
    timeout_seconds: float | None = Field(
        default=None,
        gt=0,
        le=1800,
        description=(
            "Per-request timeout for ``POST /youtube/transcript``, "
            "overriding the shared ``camofox.timeout_seconds``.  Camofox's "
            "own handler timeout defaults to 30 s but the browser-fallback "
            "path is slower, so a larger value is recommended.  ``None`` "
            "uses the shared timeout."
        ),
    )
    health_probe: bool = Field(
        default=True,
        description=(
            "Probe ``GET /health`` for ``health()``.  ``False`` reports the "
            "provider as available without a probe (for a trusted, well "
            "monitored deployment); the router still bounds it."
        ),
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> CamofoxTranscriptProviderConfig:
        """Build from the ``transcript.providers.camofox`` block.

        Priority (highest to lowest):
          1. ``SCRAPER_TRANSCRIPT_CAMOFOX_*`` environment variables
          2. The ``transcript.providers.camofox`` YAML block
          3. Hard-coded defaults
        """
        raw_transcript: dict[str, Any] | None = getattr(settings, "raw_yaml_transcript", None)
        providers = raw_transcript.get("providers") if isinstance(raw_transcript, dict) else None
        block = providers.get(NAME) if isinstance(providers, dict) else None
        if block is None:
            block = {}
        if not isinstance(block, dict):
            raise ValueError(
                f"transcript.providers.{NAME} must be a mapping of configuration values"
            )

        values: dict[str, Any] = {}
        for key in ("enabled", "timeout_seconds", "health_probe"):
            env_value = os.environ.get(ENV_PREFIX + key.upper())
            yaml_value = block.get(key)
            if env_value is not None and str(env_value) != "":
                values[key] = env_value
            elif yaml_value is not None:
                values[key] = yaml_value
        return cls(**values)


# ==========================================================================
# Error mapping
# ==========================================================================


def _map_failure(exc: Exception, *, video_id: str, languages: list[str]) -> Exception:
    """Map a Camofox/transcript failure onto the project transcript errors.

    Mapping (public messages never carry upstream text beyond safe
    markers; the raw upstream error is logged, not raised):

    ===============================  =======================================
    Upstream situation               Project error
    ===============================  =======================================
    client timeout / upstream        ``TranscriptProviderTimeoutError``
    timeout (504-class)              (504)
    401 / 403 (our credentials)      ``TranscriptProviderBlockedError``
    connection refused / 5xx /       ``TranscriptProviderUnavailableError``
    provider crash (500)             (503)
    invalid / wrong-shape JSON       ``TranscriptProviderInvalidResponseError``
    (502)
    upstream error: no captions      ``NoTranscriptAvailableError`` (404)
    upstream error: no captions in   ``TranscriptLanguageUnavailableError``
    the requested language           (404)
    upstream error: private/deleted  ``VideoUnavailableError`` (404)
    anything else                    ``TranscriptProviderUnavailableError`` (503)
    ===============================  =======================================
    """
    details = {"provider": NAME, "video_id": video_id, "languages": list(languages)}

    if isinstance(exc, CamofoxTimeoutError):
        return TranscriptProviderTimeoutError("Camofox transcript fetch timed out", details=details)
    if isinstance(exc, CamofoxAuthError):
        return TranscriptProviderBlockedError(
            "Camofox rejected the credentials for the transcript request",
            details=details,
        )
    if isinstance(exc, CamofoxInvalidResponseError):
        return TranscriptProviderInvalidResponseError(
            "Camofox returned an invalid transcript response", details=details
        )
    if isinstance(exc, CamofoxUnavailableError):
        return TranscriptProviderUnavailableError(
            "Camofox could not complete the transcript fetch", details=details
        )
    if isinstance(
        exc,
        (
            NoTranscriptAvailableError,
            TranscriptLanguageUnavailableError,
            VideoUnavailableError,
        ),
    ):
        return exc
    return TranscriptProviderUnavailableError("Camofox transcript fetch failed", details=details)


def _map_upstream_error(
    message: str | None, code: int | None, *, video_id: str, languages: list[str]
) -> Exception:
    """Map an upstream ``status == "error"`` result to a project error.

    The verified Camofox shapes are:

    - no captions:  ``{"status":"error","code":404,"message":"No captions
      available for this video"}``
    - (undocumented) captions exist but not in the requested language:
      the server falls back to the first available track, so we detect an
      *unexpected* returned language and treat it as language-unavailable.
    - private / deleted video: yt-dlp surfaces it; we classify by marker.
    """
    details = {"provider": NAME, "video_id": video_id, "languages": list(languages)}
    text = (message or "").lower()

    if any(marker in text for marker in _NO_CAPTIONS_MARKERS) or code == 404:
        return NoTranscriptAvailableError("The video has no captions", details=details)
    if any(marker in text for marker in _VIDEO_GONE_MARKERS):
        return VideoUnavailableError("The video is not available", details=details)
    if any(marker in text for marker in _BLOCK_MARKERS):
        return TranscriptProviderBlockedError("The transcript request was blocked", details=details)
    return TranscriptProviderUnavailableError(
        "Camofox could not fetch the transcript", details=details
    )


# ==========================================================================
# Provider
# ==========================================================================


class CamofoxTranscriptProvider:
    """Transcript provider backed by the external Camofox service.

    A *session-less* consumer of the shared :class:`app.camofox.CamofoxClient`
    (transcripts are a server-side endpoint, so no browser session or tab is
    involved).  The client may be ``None`` when Camofox is disabled / not
    configured: the provider then reports itself unavailable and never
    performs I/O, so the router skips it in auto mode and explicit
    ``provider=camofox`` requests fail with a clean unavailable error.
    """

    def __init__(
        self,
        client: Any | None,
        config: CamofoxTranscriptProviderConfig,
        *,
        default_timeout_seconds: float | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._default_timeout_seconds = default_timeout_seconds

    @property
    def name(self) -> str:
        return NAME

    @property
    def client(self) -> Any | None:
        """The shared Camofox client (``None`` when unconfigured).

        Exposed for the application lifespan, which closes the shared
        client on shutdown.
        """
        return self._client

    async def close(self) -> None:
        """Lifecycle hook — the shared client is closed by the application,
        never by this provider (a provider must not close shared state)."""

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> TranscriptHealth:
        """Current availability: the shared Camofox health probe.

        Returns ``available=False`` (never raises) with a credential-safe
        reason: a disabled/unconfigured client, or a failed probe.

        When the probe succeeds the result is treated as *available* even if
        the server reports its *browser* as not running: transcripts are a
        server-side endpoint (it drives ``yt-dlp`` when available) and do
        not need a live browser.  A ``running=False`` browser is only
        surfaced in the reason so the router can expose it as a warning.
        """
        if self._client is None:
            return TranscriptHealth(available=False, reason="camofox is disabled or not configured")
        if not self._config.health_probe:
            return TranscriptHealth(available=True)
        try:
            status: CamofoxHealthStatus = await self._client.probe_health()
        except Exception:  # noqa: BLE001 - a probe must never raise
            return TranscriptHealth(available=False, reason="camofox health probe failed")
        if not status.available:
            # A refused connection / bad payload means the service itself is
            # down — transcript fetches would fail.  (The provider will
            # report the real failure when fetch() is called.)
            return TranscriptHealth(available=False, reason=status.reason or "camofox unreachable")
        if status.browser_connected is False:
            # The service answers but its browser is not up; transcripts
            # still work through the yt-dlp path.  Available, with a hint.
            return TranscriptHealth(
                available=True, reason="camofox browser not connected (yt-dlp path only)"
            )
        return TranscriptHealth(available=True)

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def fetch(self, video_id: str, languages: list[str]) -> TranscriptResponse:
        """Fetch the transcript for ``video_id`` through Camofox.

        Raises the project transcript errors (see :func:`_map_failure`)
        instead of broken results, per the provider contract.
        """
        if not languages:
            raise ValidationError("At least one language is required", details={"provider": NAME})
        if self._client is None:
            raise TranscriptProviderUnavailableError(
                "The Camofox service is not configured", details={"provider": NAME}
            )

        url = f"https://www.youtube.com/watch?v={video_id}"
        timeout = self._config.timeout_seconds or self._default_timeout_seconds

        try:
            result = await self._client.youtube_transcript(url, languages, timeout_seconds=timeout)
        except CamofoxError as exc:
            logger.warning("Camofox transcript for %s failed: %s", video_id, exc.message)
            raise _map_failure(exc, video_id=video_id, languages=languages) from exc
        except Exception as exc:  # noqa: BLE001 - never leak a raw exception
            logger.exception("Unexpected failure during the Camofox transcript fetch")
            raise _map_failure(exc, video_id=video_id, languages=languages) from exc

        if not result.ok:
            # Upstream error result (e.g. no captions).  The upstream
            # message is logged for diagnosis but never raised.
            logger.warning(
                "Camofox transcript for %s returned an upstream error: code=%s message=%s",
                video_id,
                result.code,
                result.message,
            )
            raise _map_upstream_error(
                result.message, result.code, video_id=video_id, languages=languages
            )

        # The server falls back to the first available caption track when
        # the requested language is not offered (undocumented behaviour);
        # detect the mismatch so the caller can retry with the real one.
        returned_language = result.language
        if returned_language and returned_language.lower() != languages[0]:
            raise TranscriptLanguageUnavailableError(
                f"Captions are not available in '{languages[0]}'",
                details={
                    "provider": NAME,
                    "video_id": video_id,
                    "requested": list(languages),
                    "available": [returned_language],
                },
            )

        segments = parse_transcript_segments(result.transcript or "")
        if not segments:
            raise TranscriptProviderInvalidResponseError(
                "The Camofox transcript contained no usable segments",
                details={"provider": NAME, "video_id": video_id},
            )

        return TranscriptResponse.from_segments(
            video_id=video_id,
            segments=segments,
            provider=self.name,
            metadata=TranscriptMetadata(provider=self.name),
            title=result.video_title,
            # Report the language only when the backend actually provides
            # one, otherwise ``None`` (never invent data).  ``is_generated``
            # is not reported by this endpoint at all.
            language=returned_language,
            is_generated=None,
        )


# ==========================================================================
# Construction
# ==========================================================================


def build_camofox_transcript_provider(
    settings: Settings,
    client: Any | None = None,
) -> CamofoxTranscriptProvider | None:
    """Build the Camofox transcript provider from settings.

    Returns ``None`` when the provider is not enabled — the caller simply
    skips registration.  When ``client`` is not supplied the shared Camofox
    client is built from the ``camofox`` config section; a provider that is
    enabled without a usable client degrades to an *unavailable* provider
    (registered but health-unavailable) rather than a startup failure, so
    one misconfigured backend never blocks the service from booting.
    """
    config = CamofoxTranscriptProviderConfig.from_settings(settings)
    if not config.enabled:
        return None
    client_config = CamofoxConfig.from_settings(settings)
    if client is None:
        if not client_config.enabled or not client_config.base_url:
            client = None
        else:
            from app.camofox.client import CamofoxClient

            client = CamofoxClient(client_config)
    provider_timeout = config.timeout_seconds
    if provider_timeout is None and client_config.enabled:
        provider_timeout = client_config.timeout_seconds
    return CamofoxTranscriptProvider(
        client,
        config,
        default_timeout_seconds=provider_timeout,
    )
