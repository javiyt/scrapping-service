"""yt-dlp-backed YouTube transcript provider (ticket #147).

Adapts the pinned ``yt-dlp`` library to the provider-agnostic
:class:`~app.transcript.contract.TranscriptProvider` contract as the *last*
fallback in the transcript chain (``camofox -> youtube-transcript-api ->
yt-dlp``).  yt-dlp adapts to YouTube changes the fastest and returns video
metadata alongside the captions, so it makes a robust final backend.

Design
======

- :func:`parse_json3` / :func:`parse_webvtt` — turn the two caption
  formats yt-dlp exposes (``json3`` preferred, WebVTT fallback) into
  normalized :class:`~app.transcript.models.TranscriptSegment` objects.
  The VTT parser strips inline tags and header lines and de-duplicates
  the rolling-caption repeats that automatic captions produce.
- :class:`YtDlpTranscriptProvider` — a :class:`TranscriptProvider`
  implementation.  ``extract_info(url, download=False)`` runs in a worker
  thread under ``asyncio.wait_for(timeout)`` (yt-dlp is synchronous and
  blocking); the *caption track itself* is then downloaded with a plain
  ``httpx`` GET of the URL yt-dlp reported and parsed in-process.  No
  temporary files are ever created.

Configuration follows the project convention: provider-level options live
in the ``transcript.providers.yt-dlp`` YAML block, overridable with
``SCRAPER_TRANSCRIPT_YT_DLP_*`` environment variables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx
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
    "YtDlpTranscriptProvider",
    "YtDlpTranscriptProviderConfig",
    "build_ytdlp_transcript_provider",
    "parse_json3",
    "parse_webvtt",
]

logger = logging.getLogger("scraper-api.transcript.ytdlp")

#: Stable provider name (matches the ``provider_order`` entry in the router
#: config and the ``provider=`` request parameter).
NAME = "yt-dlp"

#: Environment-variable prefix for the provider-level transcript options.
ENV_PREFIX = "SCRAPER_TRANSCRIPT_YT_DLP_"


def _video_url(video_id: str) -> str:
    """The canonical watch URL built from a canonical video ID."""
    return f"https://www.youtube.com/watch?v={video_id}"


# ======================================================================
# Caption text parsing — json3 and WebVTT
# ======================================================================

#: Inline / timestamp tags inside WebVTT cue text (``<c>``, ``<00:00:01.000>``).
_VTT_TAG_RE = re.compile(r"<[^>]+>")
_VTT_CUE_TS_RE = re.compile(
    r"^(?P<start>(?:\d{1,2}:)?\d{1,2}:\d{2}\.\d{3})\s*-->\s*(?P<end>(?:\d{1,2}:)?\d{1,2}:\d{2}\.\d{3})"
)
#: Header metadata lines (``Kind:``, ``Language:``, ``Region:``).
_VTT_HEADER_LINE_RE = re.compile(r"^(?:kind|language|region)\s*:", re.IGNORECASE)


def _vtt_timestamp_to_seconds(timestamp: str) -> float | None:
    """Convert a VTT cue timestamp (``MM:SS.mmm`` or ``HH:MM:SS.mmm``) to seconds.

    Returns ``None`` for a malformed timestamp (wrong shape, or an
    out-of-range field such as ``99:99.5`` — never raises).
    """
    parts = timestamp.split(":")
    if len(parts) not in (2, 3):
        return None
    if len(parts) == 2:
        hours_v, minutes, frac = 0, *parts
    else:
        hours_v, minutes, frac = parts
    try:
        hours_v = int(hours_v)
        minutes_v = int(minutes)
        seconds_v = float(frac)
    except (TypeError, ValueError):
        return None
    if minutes_v >= 60 or not 0 <= seconds_v < 60:
        return None
    return hours_v * 3600 + minutes_v * 60 + seconds_v


def _parse_json3_bytes(payload: bytes) -> list[TranscriptSegment]:
    """Parse a ``json3`` (YouTube timedtext) payload into segments.

    A json3 document is::

        {"events": [{"tStartMs": 0, "dDurationMs": 2500,
                     "seg": [{"utf8": "héllo", "s": 0}]}]}

    Every ``seg`` item is one caption word.  Consecutive words are merged
    into a single segment per ``event`` (space-separated); the segment
    timing is the event's ``tStartMs`` / ``dDurationMs``.  Malformed
    events are skipped, never raised: a partly-degraded file still yields
    its usable part.  Returns an empty list when the payload is not valid
    json3 at all.
    """
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return []
    events = document.get("events") if isinstance(document, dict) else None
    if not isinstance(events, list):
        return []
    segments: list[TranscriptSegment] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        # Older timedtext payloads used "seg" (singular); newer ones use
        # "segs" (plural) — accept both.
        seg_items = event.get("segs")
        if not isinstance(seg_items, list):
            seg_items = event.get("seg")
        if not isinstance(seg_items, list):
            continue
        words: list[str] = []
        for item in seg_items:
            if not isinstance(item, dict):
                continue
            word = item.get("utf8")
            if isinstance(word, str) and word.strip():
                words.append(word.strip())
        if not words:
            continue
        try:
            start_ms = max(0, int(event.get("tStartMs", 0) or 0))
        except (TypeError, ValueError):
            continue
        duration_ms = event.get("dDurationMs")
        segments.append(
            TranscriptSegment(
                start=start_ms / 1000.0,
                duration=(duration_ms / 1000.0)
                if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool)
                else None,
                text=" ".join(words),
            )
        )
    return segments


def parse_json3(payload: str | bytes) -> list[TranscriptSegment]:
    """Parse a ``json3`` caption document (bytes or str) into segments."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return _parse_json3_bytes(payload)


def _shared_tail(current: str, nxt: str) -> str:
    """The longest suffix of ``current`` that is a prefix of ``nxt``.

    This is the *overlap* between two adjacent cues (the words the sliding
    caption window still shows from the previous cue).  Empty when there
    is no overlap.
    """
    max_len = min(len(current), len(nxt))
    for length in range(max_len, 0, -1):
        tail = current[-length:]
        if nxt.startswith(tail):
            return tail
    return ""


def _deduplicate_rolling(texts: list[str]) -> list[str]:
    """De-duplicate the rolling-caption repeats of automatic WebVTT captions.

    Automatic captions tile the transcript in ~3 s cue windows, so a
    phrase typically appears in *adjacent* cues: a cue that is a *prefix*
    of the next (the window grows as words arrive) and a cue whose tail is
    the *head* of the next (the window slides; the older words scroll out
    while newer words enter).  Verbatim repeats of an identical cue also
    occur.

    A cue is dropped when the *next* cue is its superset in the rolling
    sense: equal to it, or a prefix of it, or a suffix of it — in all
    three cases every word the earlier cue said is said again by the next
    cue, so the earlier one is the repeat.  A cue that is *neither* is
    kept: it either contains the next cue (a phrase scrolling *out* — it
    may carry words no other cue shows) or overlaps it only in the middle
    (a genuine coincidence of phrasing, not a rolling repeat).  Both
    "keep" cases lose no words: whatever the cue says, the *following*
    text of the transcript says it again, or nothing does.

    The comparison is scoped to *adjacent raw* cues: a cue that equals or
    contains a *far-apart* cue is a coincidence of phrasing, not a repeat.

    The result is an in-order subsequence of ``texts`` (adjacent verbatim
    survivors collapse into one, last copy winning); nothing the video
    actually said is ever lost.
    """
    if not texts:
        return []
    count = len(texts)
    keep = [True] * count
    for i in range(count):
        current = texts[i]
        if not current:
            keep[i] = False
            continue
        if i + 1 >= count:
            continue
        nxt = texts[i + 1]
        if not nxt:
            continue
        if nxt == current or current.startswith(nxt) or current.endswith(nxt):
            keep[i] = False  # the next cue re-states everything we said

    result = [text for text, ok in zip(texts, keep) if ok]
    deduped: list[str] = []
    for text in result:
        if deduped and deduped[-1] == text:
            deduped[-1] = text
        else:
            deduped.append(text)
    return deduped


def parse_webvtt(payload: str | bytes) -> list[TranscriptSegment]:
    """Parse a WebVTT caption document into normalized segments.

    Handles the features real-world YouTube captions use:

    - ``WEBVTT`` / ``Kind:`` / ``Language:`` / ``Region:`` header lines —
      dropped.
    - ``NOTE`` blocks — dropped.
    - numeric cue IDs and cue settings lines — dropped (their non-tag
      residue, if any, is treated as cue text).
    - inline ``<c>`` / ``<c color=...>`` / ``<00:00:01.000>`` tags —
      stripped from cue text.
    - ``MM:SS.mmm`` and ``HH:MM:SS.mmm`` cue timings (out-of-range parts
      make the cue unusable, never a crash).
    - the rolling-caption duplication that automatic captions produce
      (see :func:`_deduplicate_rolling`).

    Malformed cues (unparseable timestamps, empty text) are skipped, never
    raised: a partly-degraded caption file still yields the usable part.

    Each surviving cue becomes one segment; ``duration`` is the gap to the
    next surviving segment (``None`` for the last).
    """
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")

    cues: list[tuple[float, str]] = []  # (start, text)
    current_start: float | None = None
    current_parts: list[str] = []

    def _flush() -> None:
        nonlocal current_start, current_parts
        if current_start is not None:
            text = re.sub(r"\s+", " ", " ".join(current_parts)).strip()
            if text:
                cues.append((current_start, text))
        current_start = None
        current_parts = []

    for line in payload.splitlines():
        stripped = line.strip()
        if not stripped:
            _flush()
            continue
        if stripped == "WEBVTT" or stripped.startswith("WEBVTT"):
            continue
        if _VTT_HEADER_LINE_RE.match(stripped):
            continue
        if stripped.startswith("NOTE"):
            _flush()
            continue
        match = _VTT_CUE_TS_RE.match(stripped)
        if match is not None:
            _flush()
            current_start = _vtt_timestamp_to_seconds(match.group("start"))
            current_parts = []
            continue
        if current_start is None:
            continue  # a line outside any usable cue (header junk) — drop it
        cleaned = re.sub(r"\s+", " ", _VTT_TAG_RE.sub(" ", stripped)).strip()
        if cleaned:
            current_parts.append(cleaned)
    _flush()

    if not cues:
        return []

    # De-duplicate the rolling-caption repetition of automatic captions;
    # rebuild the kept cues in order (their own timings are preserved) and
    # derive each duration from the gap to the next kept cue.
    texts = [text for _, text in cues]
    kept = _deduplicate_rolling(texts)
    kept_count: dict[str, int] = {}
    for text in kept:
        kept_count[text] = kept_count.get(text, 0) + 1
    segments: list[TranscriptSegment] = []
    for start, text in cues:
        if kept_count.get(text, 0) <= 0:
            continue
        kept_count[text] -= 1
        segments.append(TranscriptSegment(start=start, duration=None, text=text))
    for index, segment in enumerate(segments):
        if index + 1 < len(segments):
            segment.duration = max(0.0, segments[index + 1].start - segment.start)
    return segments


# ======================================================================
# yt-dlp info handling
# ======================================================================


def _language_prefix(tag: str) -> str:
    """The base language of a caption tag (``en-US`` → ``en``, ``pt_BR`` → ``pt``)."""
    return tag.split("-")[0].split("_")[0]


def _requested_language_matches(available: str, requested: str) -> bool:
    """Whether an available caption language satisfies a requested one.

    Case-insensitive: exact matches always count, and ``en-US`` / ``pt_BR``
    satisfy ``en`` / ``pt`` (prefix match).  ``en`` does *not* satisfy
    ``de``.
    """
    available = available.strip().lower()
    requested = requested.strip().lower()
    if available == requested:
        return True
    if requested == _language_prefix(available):
        return True
    return available.startswith(requested + "-")  # e.g. en -> en-us


def _first_source_kind(sources: Any) -> str | None:
    """The best available caption format for a track (json3 first, then vtt).

    Accepts both yt-dlp shapes: ``{kind: url}`` and the modern
    ``[{"ext": kind, "url": ...}, ...]`` list.
    """
    formats: dict[str, Any] = {}
    if isinstance(sources, dict):
        for kind, url in sources.items():
            if url:
                formats[str(kind)] = url
    elif isinstance(sources, list):
        for entry in sources:
            if isinstance(entry, dict) and entry.get("url"):
                kind = str(entry.get("ext") or entry.get("format") or "vtt")
                formats[kind] = entry["url"]
    for kind in ("json3", "vtt"):
        if formats.get(kind):
            return kind
    for kind in formats:
        if formats.get(kind):
            return kind
    return None


def _run_coroutine_in_worker_thread(coroutine: Any) -> Any:
    """Drive a coroutine to completion from a non-async worker thread.

    Only reached when a faked :meth:`YoutubeDL.extract_info` is *async*
    (tests); it runs the coroutine on a private event loop.  Real yt-dlp
    is synchronous, so production code never takes this branch.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


def _select_track(info: dict[str, Any], languages: list[str]) -> tuple[str, str, bool] | None:
    """Select the caption track for ``languages`` from the yt-dlp info dict.

    Honours the requested language *order* (first requested language that
    the video offers wins) and prefers **manual** subtitles over automatic
    ones within each requested language; ``json3`` is preferred and
    WebVTT is the fallback.

    Returns ``(language, kind, is_generated)`` where ``kind`` is the chosen
    caption format key and ``is_generated`` is ``True`` for automatic
    captions, or ``None`` when the video offers no track matching any
    requested language.
    """
    manual = info.get("subtitles")
    automatic = info.get("automatic_captions")
    manual = manual if isinstance(manual, dict) else {}
    automatic = automatic if isinstance(automatic, dict) else {}

    for requested in languages:
        for table, is_generated in ((manual, False), (automatic, True)):
            for language, sources in table.items():
                if not _requested_language_matches(str(language), requested):
                    continue
                kind = _first_source_kind(sources)
                if kind:
                    return str(language), kind, is_generated
    return None


def _all_caption_languages(info: dict[str, Any]) -> list[str]:
    """Every caption language the video offers (manual + automatic, in order).

    Modern yt-dlp returns ``tracks[lang]`` as a *list* of format dicts;
    the (older) dict shape is tolerated for compatibility.  A language
    counts when it has at least one usable entry.
    """
    languages: list[str] = []
    for key in ("subtitles", "automatic_captions"):
        tracks = info.get(key)
        if not isinstance(tracks, dict):
            continue
        for language, formats in tracks.items():
            if language in languages:
                continue
            has = (
                formats
                if isinstance(formats, dict)
                else (isinstance(formats, list) and bool(formats))
            )
            if has:
                languages.append(str(language))
    return languages


def _track_formats(info: dict[str, Any], language: str, *, generated: bool) -> dict[str, str]:
    """The ``kind -> url`` map for one language's caption track.

    Accepts both yt-dlp shapes: ``tracks[lang] == {kind: url}`` and the
    modern ``tracks[lang] == [{"ext": kind, "url": ...}, ...]``.
    """
    key = "automatic_captions" if generated else "subtitles"
    tracks = info.get(key) or {}
    raw = tracks.get(language) if isinstance(tracks, dict) else None
    formats: dict[str, str] = {}
    if isinstance(raw, dict):
        for kind, url in raw.items():
            if isinstance(url, str) and url:
                formats[str(kind)] = url
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and isinstance(entry.get("url"), str) and entry.get("url"):
                kind = str(entry.get("ext") or entry.get("format") or "vtt")
                formats[kind] = entry["url"]
    return formats


# ======================================================================
# Error mapping
# ======================================================================

#: yt-dlp error-message markers (matched case-insensitively against the
#: DownloadError string) mapped onto the project transcript errors.  The
#: raw text is used for *classification only* — it is logged server-side
#: and never placed in a raised error message or ``details``.
_NO_CAPTIONS_MARKERS = ("no subtitles", "no captions", "could not find")
_VIDEO_GONE_MARKERS = (
    "private",
    "video unavailable",
    "not available",
    "unavailable",
    "removed",
    "does not exist",
    "live broadcast",
)
_BLOCK_MARKERS = (
    "blocked",
    "forbidden",
    "rate limit",
    "too many requests",
    "sign in",
    "signed-in",
    "authentication",
)


def _map_ytdlp_error(exc: Exception, *, video_id: str, languages: list[str]) -> Exception:
    """Map a yt-dlp failure onto the project transcript errors.

    Public error messages are canned and carry no upstream text.  The
    classification is best-effort (a ``DownloadError`` string is not a
    stable API); anything unclassified degrades to the catch-all
    ``TranscriptProviderUnavailableError`` (retryable: another provider
    may succeed).  Sign-in / block markers are checked *before* the
    video-gone markers because yt-dlp phrases them as "only available for
    signed-in users", which would otherwise read as video-gone.

    =============================================  ========================
    yt-dlp situation                               Project error
    =============================================  ========================
    timeout (ours, or ``E: Timed out``)           ``TranscriptProviderTimeoutError``
    (504)
    sign-in / IP block / 403-class markers        ``TranscriptProviderBlockedError``
    (403)
    private / deleted / unavailable video         ``VideoUnavailableError`` (404)
    no subtitles / no captions                    ``NoTranscriptAvailableError``
    (404)
    anything else                                 ``TranscriptProviderUnavailableError``
    (503)
    =============================================  ========================
    """
    details = {"provider": NAME, "video_id": video_id, "languages": list(languages)}
    text = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timed out" in text:
        return TranscriptProviderTimeoutError("The transcript fetch timed out", details=details)
    if any(marker in text for marker in _BLOCK_MARKERS):
        return TranscriptProviderBlockedError("The transcript request was blocked", details=details)
    if any(marker in text for marker in _VIDEO_GONE_MARKERS):
        return VideoUnavailableError("The video is not available", details=details)
    if any(marker in text for marker in _NO_CAPTIONS_MARKERS):
        return NoTranscriptAvailableError("The video has no captions", details=details)
    return TranscriptProviderUnavailableError("The transcript fetch failed", details=details)


# ======================================================================
# Provider configuration
# ======================================================================


class YtDlpTranscriptProviderConfig(BaseModel):
    """Provider-level options for the yt-dlp transcript provider.

    Parsed from the ``transcript.providers.yt-dlp`` YAML block, with
    ``SCRAPER_TRANSCRIPT_YT_DLP_*`` environment overrides::

        transcript:
          providers:
            yt-dlp:
              enabled: false
              timeout_seconds: 60
              proxy: null            # e.g. http://proxy.example:8080
              cookies: null          # optional cookies-file path (off by default)

    * ``timeout_seconds`` — total bound for one ``fetch()`` (metadata
      extraction *and* caption download), enforced with
      ``asyncio.wait_for``.
    * ``proxy`` — optional proxy URL for both the yt-dlp request and the
      caption-track download.  ``null`` (the default) means direct.
    * ``cookies`` — optional path to a Netscape cookies file.  Off by
      default: the provider is anonymous unless configured.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Enable the yt-dlp transcript provider (default off).",
    )
    timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        le=600,
        description=(
            "Total bound for one transcript fetch (metadata extraction + "
            "caption download), enforced with ``asyncio.wait_for``."
        ),
    )
    proxy: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional proxy URL for the yt-dlp request and the caption "
            "download (e.g. ``http://user:pass@host:port``).  Never used by "
            "default."
        ),
    )
    cookies: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional path to a Netscape-format cookies file.  Off by "
            "default — the provider is anonymous unless configured."
        ),
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> YtDlpTranscriptProviderConfig:
        """Build from the ``transcript.providers.yt-dlp`` block.

        Priority (highest to lowest):
          1. ``SCRAPER_TRANSCRIPT_YT_DLP_*`` environment variables
          2. The ``transcript.providers.yt-dlp`` YAML block
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
        for key in ("enabled", "timeout_seconds", "proxy", "cookies"):
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


class YtDlpTranscriptProvider:
    """Transcript provider backed by the ``yt-dlp`` library.

    Stateless and single-purpose.  Each ``fetch()``:

    1. Builds a :class:`yt_dlp.YoutubeDL` with ``quiet=True`` /
       ``no_warnings=True`` / no playlist / no downloads (``download=False``
       on ``extract_info`` — no media, no caption files are written), no
       cookies by default, and an optional proxy.
    2. Runs ``extract_info(url, download=False)`` in a worker thread
       (:func:`asyncio.to_thread`) bounded by ``asyncio.wait_for`` (the
       whole fetch — metadata *and* caption download — shares one
       ``timeout_seconds`` bound).
    3. Selects the caption track (requested-language order, manual before
       automatic, ``json3`` before ``vtt``) and downloads the reported
       URL with ``httpx`` — the parsed bytes are the only artifact; no
       temporary files exist at any point.

    ``health()`` performs no I/O: the only failure mode is yt-dlp being
    absent at import time, which is caught at construction (a missing
    dependency with ``enabled: true`` is a startup error, not a silent
    degradation).  When present the provider reports available.
    """

    def __init__(self, config: YtDlpTranscriptProviderConfig) -> None:
        self._config = config
        #: Languages of the in-flight fetch (set by :meth:`fetch` before
        #: ``_extract_info``); used only to hint yt-dlp's
        #: ``subtitleslangs`` — track selection happens in-process in
        #: :func:`_select_track`, so the hint is never load-bearing.
        self._languages: list[str] = []

    @property
    def name(self) -> str:
        return NAME

    @property
    def config(self) -> YtDlpTranscriptProviderConfig:
        return self._config

    async def close(self) -> None:
        """Lifecycle hook — nothing to close (stateless; the httpx client
        is per-request)."""

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health(self) -> TranscriptHealth:
        """Available when constructed (a missing ``yt_dlp`` import fails
        construction instead — see :func:`build_ytdlp_transcript_provider`).
        Never performs I/O and never raises beyond the router's probe bound."""
        return TranscriptHealth(available=True)

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    async def fetch(self, video_id: str, languages: list[str]) -> TranscriptResponse:
        """Fetch the transcript for ``video_id`` through yt-dlp.

        Raises the project transcript errors (see :func:`_map_ytdlp_error`)
        instead of broken results, per the provider contract.
        """
        if not languages:
            raise ValidationError("At least one language is required", details={"provider": NAME})
        self._languages = list(languages)
        details_base = {"provider": NAME, "video_id": video_id, "languages": list(languages)}

        # The whole fetch (metadata + caption download) shares one bound.
        try:
            return await asyncio.wait_for(
                self._fetch_inner(video_id, languages),
                timeout=self._config.timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "yt-dlp transcript for %s exceeded the %.0fs bound",
                video_id,
                self._config.timeout_seconds,
            )
            raise TranscriptProviderTimeoutError(
                "The transcript fetch timed out", details=details_base
            ) from None

    async def _fetch_inner(self, video_id: str, languages: list[str]) -> TranscriptResponse:
        """The unbounded fetch: metadata extraction, track selection, download."""
        # ----------------------------------------------------------- metadata
        try:
            info = await asyncio.to_thread(self._extract_info, video_id)
        except TimeoutError:
            raise  # the outer wait_for owns the timeout classification
        except Exception as exc:
            # DownloadError & friends — classified, raw text logged only.
            logger.warning("yt-dlp for %s failed: %s", video_id, exc)
            raise _map_ytdlp_error(exc, video_id=video_id, languages=languages) from exc

        if not isinstance(info, dict):
            logger.warning("yt-dlp for %s returned no info dict", video_id)
            raise TranscriptProviderInvalidResponseError(
                "The transcript backend returned no video metadata",
                details={"provider": NAME, "video_id": video_id, "languages": list(languages)},
            )

        # ---------------------------------------------------------- track
        track = _select_track(info, languages)
        if track is None:
            available = _all_caption_languages(info)
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

        language, kind, is_generated = track
        source = _track_formats(info, language, generated=is_generated).get(kind)
        if not isinstance(source, str) or not source:
            logger.warning(
                "yt-dlp reported no download URL for %s/%s on %s", language, kind, video_id
            )
            raise TranscriptProviderInvalidResponseError(
                "The transcript backend returned no caption data",
                details={"provider": NAME, "video_id": video_id, "language": language},
            )

        # ---------------------------------------------------------- download
        try:
            payload = await self._download_caption(source)
        except TimeoutError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Caption download for %s failed: %s", video_id, exc)
            raise TranscriptProviderUnavailableError(
                "The transcript download failed",
                details={"provider": NAME, "video_id": video_id},
            ) from exc

        # ---------------------------------------------------------- parse
        segments = parse_json3(payload) if kind == "json3" else parse_webvtt(payload)
        if not segments:
            logger.warning(
                "Caption payload for %s (%s/%s, %d bytes) parsed to no segments",
                video_id,
                language,
                kind,
                len(payload),
            )
            raise TranscriptProviderInvalidResponseError(
                "The caption file contained no usable segments",
                details={
                    "provider": NAME,
                    "video_id": video_id,
                    "language": language,
                    "kind": kind,
                },
            )

        title = info.get("title")
        return TranscriptResponse.from_segments(
            video_id=video_id,
            segments=segments,
            provider=self.name,
            metadata=TranscriptMetadata(provider=self.name),
            title=title if isinstance(title, str) and title else None,
            language=language,
            is_generated=is_generated,
        )

    # ------------------------------------------------------------- internals
    def _ytdlp_options(self) -> dict[str, Any]:
        """The ``extract_info`` options.

        Option names verified against the pinned yt-dlp release:
        ``quiet`` / ``no_warnings`` / ``skip_download`` / ``subtitleslangs``
        / ``writeautomaticsub`` (the current name — not the historical
        ``writeautomaticsubtitles``) / ``cookiefile`` / ``proxy`` /
        ``outtmpl``.  ``no_playlist`` and its deprecated alias
        ``noplaylist`` are both passed so older releases (which reject
        unknown keys) and newer ones (which warn on the alias) behave.
        ``outtmpl`` is a harmless no-op placeholder: with
        ``download=False`` nothing is written, so no temp files can exist.
        """
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "no_playlist": True,
            "noplaylist": True,  # deprecated alias — accepted by older releases
            "skip_download": True,
            "writeautomaticsub": True,  # expose automatic captions in the info dict
            "subtitleslangs": list(self._languages),
            "outtmpl": "memory:never",  # no media is ever downloaded
        }
        if self._config.cookies:
            options["cookiefile"] = self._config.cookies
        if self._config.proxy:
            options["proxy"] = self._config.proxy
        return options

    def _extract_info(self, video_id: str) -> dict[str, Any]:
        """Blocking: run ``extract_info(url, download=False)``.

        Runs inside :func:`asyncio.to_thread` (called from
        :meth:`_fetch_inner`); the ``with`` block guarantees the yt-dlp
        object is closed even when the task is cancelled on timeout.
        """
        import asyncio

        import yt_dlp

        with yt_dlp.YoutubeDL(self._ytdlp_options()) as ydl:
            result = ydl.extract_info(_video_url(video_id), download=False)
        if asyncio.iscoroutine(result):
            # Faked/async extract_info: bridge it back onto the running
            # loop.  (Real yt-dlp is synchronous; this branch exists so
            # tests can install an async fake.)
            return _run_coroutine_in_worker_thread(result)
        return result

    async def _download_caption(self, url: str) -> bytes:
        """Download the chosen caption track over plain HTTP (no temp file)."""
        async with httpx.AsyncClient(
            proxy=self._config.proxy,
            timeout=httpx.Timeout(self._config.timeout_seconds),
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content


# ======================================================================
# Construction
# ======================================================================


def build_ytdlp_transcript_provider(
    settings: Settings,
) -> YtDlpTranscriptProvider | None:
    """Build the yt-dlp transcript provider from settings.

    Returns ``None`` when the provider is not enabled.  When enabled, a
    missing ``yt-dlp`` installation raises :class:`ValueError` at startup
    (a configuration mistake the operator must fix) rather than silently
    degrading the chain.
    """
    config = YtDlpTranscriptProviderConfig.from_settings(settings)
    if not config.enabled:
        return None
    try:
        import yt_dlp
    except ImportError as exc:
        logger.error("transcript.providers.yt-dlp is enabled but yt-dlp is not installed: %s", exc)
        raise ValueError(
            "transcript.providers.yt-dlp is enabled but the 'yt-dlp' package is not "
            "installed (add yt-dlp to requirements.txt)"
        ) from exc
    logger.info(
        "yt-dlp transcript provider enabled (yt-dlp %s, timeout=%.0fs%s)",
        getattr(yt_dlp.version, "__version__", "unknown"),
        config.timeout_seconds,
        ", proxy configured" if config.proxy else "",
    )
    return YtDlpTranscriptProvider(config)
