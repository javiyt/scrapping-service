"""Concrete transcript provider implementations (and their construction).

The service owns the transcript contract (see :mod:`app.transcript`); this
package holds concrete backends that adapt to it.  :func:`build_transcript_providers`
constructs the configured, enabled providers from application settings.

Currently implemented backends (in registration order):

* :class:`app.transcript.providers.camofox.CamofoxTranscriptProvider`
  (ticket #145) — backed by Camofox's ``POST /youtube/transcript``.  It
  consumes the *shared* Camofox client (the same one the search provider
  and the browser API use) when the application passes it in; otherwise it
  builds its own from the ``camofox`` section, degrading to an
  unavailable-but-registered provider when Camofox is disabled.
* :class:`app.transcript.providers.youtube_transcript_api.YouTubeTranscriptApiProvider`
  (ticket #146) — the ``youtube-transcript-api`` library, the *first*
  non-Camofox fallback: plain HTTP, synchronous (run in worker threads).
* :class:`app.transcript.providers.ytdlp.YtDlpTranscriptProvider`
  (ticket #147) — the yt-dlp library, the *last* fallback in the chain.
  Stateless: one ``extract_info(download=False)`` in a worker thread plus
  an HTTP download of the chosen caption track; no temp files, no cookies
  by default.

``build_transcript_providers`` is called once at startup by
:func:`app.transcript.router.build_transcript_router` (the application
lifespan), *after* the shared Camofox client has been resolved; the client
may be passed through via :func:`build_transcript_providers`'s ``client``
parameter.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.transcript.contract import TranscriptProvider
from app.transcript.providers.camofox import build_camofox_transcript_provider
from app.transcript.providers.youtube_transcript_api import build_youtube_transcript_api_provider
from app.transcript.providers.ytdlp import build_ytdlp_transcript_provider


def build_transcript_providers(
    settings: Settings, client: Any | None = None
) -> list[TranscriptProvider]:
    """Build all configured transcript providers that are enabled.

    ``client`` is the shared Camofox client (``None`` when the ``camofox``
    section is disabled/unconfigured) — providers that consume it (Camofox)
    reuse it instead of opening a second HTTP client.
    """
    providers: list[TranscriptProvider] = []

    # #145 — Camofox transcript provider.
    camofox = build_camofox_transcript_provider(settings, client=client)
    if camofox is not None:
        providers.append(camofox)

    # #146 — youtube-transcript-api provider (first non-Camofox fallback).
    yta = build_youtube_transcript_api_provider(settings)
    if yta is not None:
        providers.append(yta)

    # #147 — yt-dlp transcript provider (last fallback in the chain).
    ytdlp = build_ytdlp_transcript_provider(settings)
    if ytdlp is not None:
        providers.append(ytdlp)

    return providers
