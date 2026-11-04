"""Concrete transcript provider implementations (and their construction).

The service owns the transcript contract (see :mod:`app.transcript`); this
package holds concrete backends that adapt to it.  :func:`build_transcript_providers`
constructs the configured, enabled providers from application settings.

Currently enabled backend:

* :class:`app.transcript.providers.camofox.CamofoxTranscriptProvider`
  (ticket #145) — backed by Camofox's ``POST /youtube/transcript``.  It
  consumes the *shared* Camofox client (the same one the search provider
  and the browser API use) when the application passes it in; otherwise it
  builds its own from the ``camofox`` section, degrading to an
  unavailable-but-registered provider when Camofox is disabled.

``build_transcript_providers`` is called once at startup by
:func:`app.transcript.router.build_transcript_router` (the application
lifespan), *after* the shared Camofox client has been resolved; the client
may be passed through via :func:`build_transcript_providers`'s ``client``
parameter.  Later backends (``youtube-transcript-api`` #146, ``yt-dlp``
#147) are added here by extending the factory.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.transcript.contract import TranscriptProvider
from app.transcript.providers.camofox import build_camofox_transcript_provider


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

    # Future backends:
    #   #146 youtube-transcript-api, #147 yt-dlp
    return providers
