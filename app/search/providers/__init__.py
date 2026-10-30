"""Search provider implementations and construction.

The service owns the search contract (see :mod:`app.search`); this package
holds concrete backends that adapt to it.  :func:`build_search_providers`
constructs the configured, enabled providers from application settings —
currently the only backend is SearXNG.
"""

from app.core.config import Settings
from app.search.contract import SearchProvider
from app.search.providers.searxng import SearxngConfig, SearxngProvider

__all__ = ["SearxngConfig", "SearxngProvider", "build_search_providers"]


def build_search_providers(settings: Settings) -> list[SearchProvider]:
    """Build all configured search providers that are enabled.

    New backends are added by extending this factory; the rest of the
    service only ever sees the :class:`SearchProvider` contract.
    """
    providers: list[SearchProvider] = []

    searxng_config = SearxngConfig.from_settings(settings)
    if searxng_config.enabled:
        providers.append(SearxngProvider(searxng_config))

    return providers
