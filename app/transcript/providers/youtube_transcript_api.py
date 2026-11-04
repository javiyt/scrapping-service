"""``youtube-transcript-api``-backed YouTube transcript provider (ticket #146).

Adapts the ``youtube-transcript-api`` library to the provider-agnostic
:class:`~app.transcript.contract.TranscriptProvider` contract as the
*first* non-Camofox fallback in the transcript chain
(``camofox -> youtube-transcript-api -> yt-dlp``).  The library is a
plain-HTTP implementation (no browser, no cookies required), which makes
it a cheap in-process middle fallback between the external Camofox
service and the heavier yt-dlp backend.

Design
======

- :class:`YouTubeTranscriptApiProvider` — a :class:`TranscriptProvider`
  implementation.  The library is *synchronous* (``requests``-based), so
  every call it makes — ``list()`` and ``Transcript.fetch()`` — runs in a
  worker thread via :func:`asyncio.to_thread`, bounded by
  ``asyncio.wait_for(timeout)``; the event loop is never blocked.
- Transcript selection honours the requested language *order* and prefers
  **manually created** transcripts over generated ones within each
  requested language (``find_manually_created_transcript`` /
  ``find_generated_transcript``); ``language`` and ``is_generated`` come
  from the library's transcript metadata.
- Library exceptions map onto the project transcript errors
  (see :func:`_map_yta_error`): blocks are *retryable* (the router falls
  back); transcript/video absence is *definitive*.

Configuration follows the project convention: provider-level options live
in the ``transcript.providers.youtube-transcript-api`` YAML block,
overridable with ``SCRAPER_TRANSCRIPT_YT_TRANS_API_*`` environment
variables.  Optional proxy support goes through the library's
``GenericProxyConfig``; the service runs on a home IP so blocking is
expected to be rare — the proxy is off by default.

Unsupported (upstream, documented): cookie authentication for
age-restricted videos is broken in ``youtube-transcript-api`` (upstream
issue jdepoix/youtube-transcript-api#133 — the library cannot reliably
send cookies to the caption endpoint), so age-restricted videos are not
supported by this provider; they surface as
:class:`~app.core.errors.VideoUnavailableError` and the router falls back.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.core.errors import (
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
from app.transcript.models import TranscriptMetadata, TranscriptResponse, TranscriptSegment

__all__ = [
    "NAME",
    "YouTubeTranscriptApiProvider",
    "YouTubeTranscriptApiProviderConfig",
    "build_youtube_transcript_api_provider",
]

logger = logging.getLogger("scraper-api.transcript.youtube-transcript-api")

#: Stable provider name (matches the ``provider_order`` entry in the router
#: config and the ``provider=`` request parameter).
NAME = "youtube-transcript-api"

#: Environment-variable prefix for the provider-level transcript options.
ENV_PREFIX = "SCRAPER_TRANSCRIPT_YT_TRANS_API_"


#: ``youtube-transcript-api`` is imported lazily (inside the library
#: accessors below) so the module — and the rest of the service — imports
#: cleanly when the optional dependency is absent.  The construction
#: factory verifies the import when the provider is *enabled*.
def _yta() -> Any:
    """Import and return the ``youtube_transcript_api`` module."""
    import youtube_transcript_api

    return youtube_transcript_api


# ======================================================================
# Library accessors (kept small so tests can fake them at module level)
# ======================================================================


def _api() -> Any:
    """The ``YouTubeTranscriptApi`` class."""
    return _yta().YouTubeTranscriptApi


def _generic_proxy_config(http_url: str | None, https_url: str | None) -> Any:
    """The library's generic proxy config (both URLs optional)."""
    return _yta().proxies.GenericProxyConfig(http_url=http_url, https_url=https_url)


def _error_name(exc: BaseException, *candidates: str) -> bool:
    """Whether ``exc`` is (an instance of) one of the named library errors."""
    yta = _yta()
    for name in candidates:
        error_cls = getattr(yta, name, None)
        if isinstance(error_cls, type) and isinstance(exc, error_cls):
            return True
    return False


def _transcript_list_attributes(transcript_list: Any) -> dict[str, Any]:
    """The raw manuscript structures of a ``TranscriptList`` object.

    Accepts both real library objects (private ``_``-suffixed dicts) and
    fakes in tests that expose the same attributes without the underscore.
    """

    def _attr(name: str) -> Any:
        value = getattr(transcript_list, f"_{name}", None)
        if value is None:
            value = getattr(transcript_list, name, None)
        return value

    return {
        "video_id": getattr(transcript_list, "video_id", None),
        "manual": _attr("manually_created_transcripts"),
        "generated": _attr("generated_transcripts"),
    }


def _transcript_attributes(transcript: Any) -> dict[str, Any]:
    """The metadata of a ``Transcript`` object (language, kind, fetcher)."""
    return {
        "video_id": getattr(transcript, "video_id", None),
        "language": getattr(transcript, "language", None),
        "language_code": getattr(transcript, "language_code", None),
        "is_generated": getattr(transcript, "is_generated", None),
        "fetch": getattr(transcript, "fetch", None),
    }


def _snippet_attributes(snippet: Any) -> dict[str, Any]:
    """One caption snippet's fields (``FetchedTranscriptSnippet``)."""
    return {
        "text": getattr(snippet, "text", None),
        "start": getattr(snippet, "start", None),
        "duration": getattr(snippet, "duration", None),
    }


# ======================================================================
# Selection
# ======================================================================


def _available_languages(transcript_list: Any) -> list[str]:
    """Every caption language the video offers (manual + generated, in order)."""
    structures = _transcript_list_attributes(transcript_list)
    languages: list[str] = []
    for table_name in ("manual", "generated"):
        table = structures[table_name]
        if not isinstance(table, dict):
            continue
        for language_code in table:
            if language_code not in languages:
                languages.append(str(language_code))
    return languages


def _select_transcript(transcript_list: Any, languages: list[str]) -> Any | None:
    """Select the transcript for ``languages`` from a ``TranscriptList``.

    Honours the requested language *order* (the first requested language
    the video offers wins) and prefers **manually created** transcripts
    over generated ones *within* each requested language:

    * :meth:`find_manually_created_transcript` with the *full* requested
      list is tried first, then :meth:`find_generated_transcript` with
      the same list.  Together these reproduce the library's
      ``find_transcript`` semantics (per-language manual-first) while
      keeping the language order intact.

    Returns the chosen ``Transcript`` object, or ``None`` when the video
    offers no track in any requested language (the caller raises the
    appropriate project error using :func:`_available_languages`).
    """
    for finder in ("find_manually_created_transcript", "find_generated_transcript"):
        find = getattr(transcript_list, finder, None)
        if find is None:  # pragma: no cover - defensive: fakes are complete
            continue
        try:
            return find(languages)
        except Exception as exc:  # noqa: BLE001 - the library raises its own error types
            if _error_name(exc, "NoTranscriptFound"):
                continue  # no transcript of this kind in any requested language
            raise
    return None


# ======================================================================
# Error mapping
# ======================================================================


def _map_yta_error(exc: Exception, *, video_id: str, languages: list[str]) -> Exception:
    """Map a ``youtube-transcript-api`` exception onto the project errors.

    Public error messages are canned and carry no upstream text.

    ==============================================  ========================
    library exception                               Project error
    ==============================================  ========================
    ``RequestBlocked`` / ``IpBlocked``              ``TranscriptProviderBlockedError``
    (retryable — the router falls back)
    ``TranscriptsDisabled`` / ``NoTranscriptFound`` ``NoTranscriptAvailableError``
    (definitive — no other provider helps)
    ``VideoUnavailable`` / ``VideoUnplayable`` /
    ``AgeRestricted``                               ``VideoUnavailableError``
    (definitive)
    timeout (ours)                                  ``TranscriptProviderTimeoutError``
    anything else                                   ``TranscriptProviderUnavailableError``
    (retryable)
    ==============================================  ========================

    ``NoTranscriptFound`` carries the ``TranscriptList`` (its available
    languages) in the exception, so the *missing-language* case
    (transcripts exist, just not in the requested ones) is surfaced as
    :class:`TranscriptLanguageUnavailableError` with the available list.
    """
    details = {"provider": NAME, "video_id": video_id, "languages": list(languages)}

    if isinstance(exc, TimeoutError):
        return TranscriptProviderTimeoutError("The transcript fetch timed out", details=details)

    yta = _yta()

    if _error_name(exc, "RequestBlocked", "IpBlocked"):
        return TranscriptProviderBlockedError("The transcript request was blocked", details=details)

    if _error_name(exc, "VideoUnavailable", "VideoUnplayable", "AgeRestricted"):
        return VideoUnavailableError("The video is not available", details=details)

    if isinstance(exc, getattr(yta, "NoTranscriptFound", ()) or Exception):
        transcript_data = getattr(exc, "_transcript_data", None)
        if transcript_data is None:
            transcript_data = getattr(exc, "transcript_data", None)
        available = _available_languages(transcript_data) if transcript_data is not None else []
        if available:
            return TranscriptLanguageUnavailableError(
                f"Captions are not available in any of {list(languages)}",
                details={
                    **details,
                    "requested": list(languages),
                    "available": available,
                },
            )
        return NoTranscriptAvailableError("The video has no captions", details=details)

    if _error_name(exc, "TranscriptsDisabled"):
        return NoTranscriptAvailableError("The video has no captions", details=details)

    return TranscriptProviderUnavailableError("The transcript fetch failed", details=details)


# ======================================================================
# Provider configuration
# ======================================================================


class YouTubeTranscriptApiProviderConfig(BaseModel):
    """Provider-level options for the ``youtube-transcript-api`` provider.

    Parsed from the ``transcript.providers.youtube-transcript-api`` YAML
    block, with ``SCRAPER_TRANSCRIPT_YT_TRANS_API_*`` environment
    overrides::

        transcript:
          providers:
            youtube-transcript-api:
              enabled: false
              timeout_seconds: 30
              proxy: null            # e.g. http://proxy.example:8080

    * ``timeout_seconds`` — total bound for one ``fetch()`` (transcript
      list *and* caption retrieval), enforced with
      ``asyncio.wait_for``.
    * ``proxy`` — optional proxy URL (http/https; the same URL is used for
      both schemes) passed to the library as a ``GenericProxyConfig``.
      Off by default: the service runs on a home IP, so blocking is
      expected to be rare.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Enable the ``youtube-transcript-api`` transcript provider (default off).",
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=600,
        description=(
            "Total bound for one transcript fetch (transcript list + caption "
            "retrieval), enforced with ``asyncio.wait_for``."
        ),
    )
    proxy: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional proxy URL for the library's HTTP calls (e.g. "
            "``http://user:pass@host:port``).  Never used by default."
        ),
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> YouTubeTranscriptApiProviderConfig:
        """Build from the ``transcript.providers.youtube-transcript-api`` block.

        Priority (highest to lowest):
          1. ``SCRAPER_TRANSCRIPT_YT_TRANS_API_*`` environment variables
          2. The ``transcript.providers.youtube-transcript-api`` YAML block
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
        for key in ("enabled", "timeout_seconds", "proxy"):
            env_value = os.environ.get(ENV_PREFIX + key.upper())
            yaml_value = block.get(key)
            if env_value is not None and str(env_value) != "":
                values[key] = env_value
            elif yaml_value is not None:
                values[key] = yaml_value
        return cls(**values)


# ======================================================================
# Provider
# ======================================================================


class YouTubeTranscriptApiProvider:
    """Transcript provider backed by the ``youtube-transcript-api`` library.

    Stateless and single-purpose.  Each ``fetch()``:

    1. Builds a :class:`YouTubeTranscriptApi` (with the optional
       ``GenericProxyConfig`` from the settings; no cookies — cookie
       support for age-restricted videos is broken upstream and
       documented as unsupported).
    2. Runs ``list(video_id)`` in a worker thread
       (:func:`asyncio.to_thread`) — the library is synchronous and
       blocking, so *nothing* it does runs on the event loop.
    3. Selects the transcript (requested-language order, manual before
       generated) and runs ``transcript.fetch()`` in a worker thread.
    4. Maps the library's ``FetchedTranscript`` snippets onto normalized
       :class:`~app.transcript.models.TranscriptSegment` objects.

    All library I/O (steps 2–3) shares one ``timeout_seconds`` bound
    enforced with :func:`asyncio.wait_for`; a bound-exceeding fetch
    raises :class:`TranscriptProviderTimeoutError`.

    ``health()`` performs no I/O: the only failure mode is the library
    being absent at import time, which is caught at construction (a
    missing dependency with ``enabled: true`` is a startup error, not a
    silent degradation).  When present the provider reports available.
    """

    def __init__(self, config: YouTubeTranscriptApiProviderConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return NAME

    @property
    def config(self) -> YouTubeTranscriptApiProviderConfig:
        return self._config

    async def close(self) -> None:
        """Lifecycle hook — nothing to close (stateless; the library's HTTP
        session is per-fetch)."""

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> TranscriptHealth:
        """Available when constructed (a missing library import fails
        construction instead — see :func:`build_youtube_transcript_api_provider`).
        Never performs I/O and never raises beyond the router's probe bound."""
        return TranscriptHealth(available=True)

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def fetch(self, video_id: str, languages: list[str]) -> TranscriptResponse:
        """Fetch the transcript for ``video_id`` through
        ``youtube-transcript-api``.

        Raises the project transcript errors (see :func:`_map_yta_error`)
        instead of broken results, per the provider contract.
        """
        if not languages:
            raise ValidationError("At least one language is required", details={"provider": NAME})

        details_base = {"provider": NAME, "video_id": video_id, "languages": list(languages)}
        try:
            return await asyncio.wait_for(
                self._fetch_inner(video_id, languages),
                timeout=self._config.timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "youtube-transcript-api fetch for %s exceeded the %.0fs bound",
                video_id,
                self._config.timeout_seconds,
            )
            raise TranscriptProviderTimeoutError(
                "The transcript fetch timed out", details=details_base
            ) from None

    async def _fetch_inner(self, video_id: str, languages: list[str]) -> TranscriptResponse:
        """The fetch, unbounded (the outer :func:`asyncio.wait_for` owns the timeout)."""
        # ---------------------------------------------------------- transcript list
        try:
            transcript_list = await asyncio.to_thread(self._list_transcripts, video_id)
        except Exception as exc:
            raise _map_yta_error(exc, video_id=video_id, languages=languages) from exc

        transcript = _select_transcript(transcript_list, languages)
        if transcript is None:
            available = _available_languages(transcript_list)
            if available:
                raise TranscriptLanguageUnavailableError(
                    f"Captions are not available in any of {list(languages)}",
                    details={
                        "provider": NAME,
                        "video_id": video_id,
                        "requested": list(languages),
                        "available": available,
                    },
                )
            raise NoTranscriptAvailableError(
                "The video has no captions",
                details={"provider": NAME, "video_id": video_id, "languages": list(languages)},
            )

        metadata_attrs = _transcript_attributes(transcript)
        language_code = metadata_attrs["language_code"]
        is_generated = metadata_attrs["is_generated"]

        # ----------------------------------------------------------- caption content
        fetch = metadata_attrs["fetch"]
        if not callable(fetch):  # pragma: no cover - defensive: fakes are complete
            raise TranscriptProviderInvalidResponseError(
                "The transcript backend returned no caption data",
                details={
                    "provider": NAME,
                    "video_id": video_id,
                    "language": language_code,
                },
            )
        try:
            fetched = await asyncio.to_thread(fetch)
        except Exception as exc:
            raise _map_yta_error(exc, video_id=video_id, languages=languages) from exc

        snippets = [
            TranscriptSegment(
                start=float(snippet["start"]),
                duration=(float(snippet["duration"]) if snippet["duration"] is not None else None),
                text=str(snippet["text"]),
            )
            for snippet in (_snippet_attributes(item) for item in fetched)
            if snippet["text"] is not None and snippet["start"] is not None
        ]
        if not snippets:
            logger.warning("Transcript for %s parsed to no usable segments", video_id)
            raise TranscriptProviderInvalidResponseError(
                "The caption data contained no usable segments",
                details={"provider": NAME, "video_id": video_id, "language": language_code},
            )

        return TranscriptResponse.from_segments(
            video_id=video_id,
            segments=snippets,
            provider=self.name,
            metadata=TranscriptMetadata(provider=self.name),
            language=str(language_code) if language_code else None,
            is_generated=bool(is_generated) if is_generated is not None else None,
        )

    # ------------------------------------------------------------- internals
    def _build_api(self) -> Any:
        """A fresh :class:`YouTubeTranscriptApi` with the configured proxy."""
        api_cls = _api()
        if self._config.proxy:
            return api_cls(
                proxy_config=_generic_proxy_config(self._config.proxy, self._config.proxy)
            )
        return api_cls()

    def _list_transcripts(self, video_id: str) -> Any:
        """Blocking: ``api.list(video_id)`` — runs in a worker thread.

        The library's ``list()`` does not take a language argument; the
        language selection happens afterwards in :func:`_select_transcript`.
        """
        return self._build_api().list(video_id)


# ======================================================================
# Construction
# ======================================================================


def build_youtube_transcript_api_provider(
    settings: Settings,
) -> YouTubeTranscriptApiProvider | None:
    """Build the ``youtube-transcript-api`` transcript provider from settings.

    Returns ``None`` when the provider is not enabled.  When enabled, a
    missing ``youtube-transcript-api`` installation raises
    :class:`ValueError` at startup (a configuration mistake the operator
    must fix) rather than silently degrading the chain.
    """
    config = YouTubeTranscriptApiProviderConfig.from_settings(settings)
    if not config.enabled:
        return None
    try:
        _yta()
    except ImportError as exc:
        logger.error(
            "transcript.providers.%s is enabled but youtube-transcript-api is not installed: %s",
            NAME,
            exc,
        )
        raise ValueError(
            f"transcript.providers.{NAME} is enabled but the 'youtube-transcript-api' "
            "package is not installed (add youtube-transcript-api to requirements.txt)"
        ) from exc
    logger.info(
        "youtube-transcript-api transcript provider enabled (timeout=%.0fs%s)",
        config.timeout_seconds,
        ", proxy configured" if config.proxy else "",
    )
    return YouTubeTranscriptApiProvider(config)
