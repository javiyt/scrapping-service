"""Concrete transcript provider implementations (and their construction).

The service owns the transcript contract (see :mod:`app.transcript`); this
package holds concrete backends that adapt to it (Camofox,
``youtube-transcript-api``, ``yt-dlp`` — each added by its own follow-up
issue).  :func:`build_transcript_providers` constructs the configured,
enabled providers from application settings.

The foundation (the ticket this package mirrors ``app.search`` for)
ships **no concrete provider**: the factory returns an empty list until a
provider issue lands, so the service boots cleanly and the router reports
``no_transcript_provider_available``.
"""

from __future__ import annotations

from app.core.config import Settings
from app.transcript.contract import TranscriptProvider


def build_transcript_providers(settings: Settings) -> list[TranscriptProvider]:
    """Build all configured transcript providers that are enabled.

    New backends are added by extending this factory; the rest of the
    service only ever sees the :class:`TranscriptProvider` contract.
    Providers read their per-provider block from the ``transcript`` YAML
    section (``transcript.providers.<name>``), with ``SCRAPER_TRANSCRIPT_*``
    environment overrides, following the ``app.search.providers``
    convention.
    """
    providers: list[TranscriptProvider] = []
    # Concrete providers are added in follow-up issues:
    #   #145 camofox, #146 youtube-transcript-api, #147 yt-dlp
    return providers
