"""Browser-backend adapters and the backend-selection orchestrator.

* :mod:`.botasaurus` — the *primary* backend: a thin adapter over the existing
  :class:`app.scraper.browser_fetcher.BrowserFetcher` (Botasaurus Driver).
* :mod:`.camofox` — the *optional fallback* backend, backed by the external
  Camofox REST service.
* :class:`BrowserBackendOrchestrator` — the concrete selection/fallback
  engine the scraper service uses to satisfy the
  :class:`app.scraper.browser_backend.BrowserBackend` contract: try the
  primary, then each fallback exactly once (no retry loops), and record
  bounded backend metrics.
"""

from app.scraper.browser_backend import BrowserBackend, BrowserBackendResult
from app.scraper.browser_backends.botasaurus import BotasaurusBackend
from app.scraper.browser_backends.camofox import (
    CAMOFOX_NAME,
    CamofoxBrowserBackend,
    build_camofox_backend,
)
from app.scraper.browser_backends.orchestrator import BrowserBackendOrchestrator
from app.scraper.browser_fetcher import BrowserFetcher

__all__ = [
    "BrowserBackend",
    "BrowserBackendResult",
    "BotasaurusBackend",
    "CamofoxBrowserBackend",
    "BrowserBackendOrchestrator",
    "CAMOFOX_NAME",
    "build_botasaurus_backend",
    "build_camofox_backend",
]


def build_botasaurus_backend(
    fetcher_args: dict,
    fetcher: BrowserFetcher | None = None,
) -> BotasaurusBackend:
    """Build the primary Botasaurus backend.

    If *fetcher* is supplied (typically the shared pool fetcher) it is wrapped
    directly; otherwise a new :class:`BrowserFetcher` is created from
    *fetcher_args* on first use.
    """
    if fetcher is not None:
        return BotasaurusBackend.fetcher_only(fetcher)
    return BotasaurusBackend(fetcher_args)
