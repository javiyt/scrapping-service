"""Camofox search provider — Google, Reddit and YouTube through Camofox macros.

- :class:`CamofoxSearchConfig` — provider-level configuration (the shared
  ``camofox`` client section lives in :mod:`app.camofox`).
- :class:`CamofoxSearchProvider` — the :class:`SearchProvider` implementation.
- :mod:`mapping` — the only place where Camofox macro names exist
  (``@google_search`` / ``@reddit_search`` / ``@youtube_search``); they never
  reach the public Search API.
"""

from app.search.providers.camofox.config import (
    NAME,
    CamofoxSearchConfig,
    resolve_camofox_client,
)
from app.search.providers.camofox.provider import CamofoxSearchProvider

__all__ = [
    "NAME",
    "CamofoxSearchConfig",
    "CamofoxSearchProvider",
    "resolve_camofox_client",
]
