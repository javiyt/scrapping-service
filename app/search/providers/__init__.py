"""Search provider implementations and construction.

The service owns the search contract (see :mod:`app.search`); this package
holds concrete backends that adapt to it.  :func:`build_search_providers`
constructs the configured, enabled providers from application settings —
currently SearXNG and Camofox.
"""

from app.core.config import Settings
from app.search.contract import SearchProvider
from app.search.providers.camofox import (
    CamofoxSearchConfig,
    CamofoxSearchProvider,
    resolve_camofox_client,
)
from app.search.providers.searxng import SearxngConfig, SearxngProvider

__all__ = [
    "CamofoxSearchConfig",
    "CamofoxSearchProvider",
    "SearxngConfig",
    "SearxngProvider",
    "build_search_providers",
]


def build_search_providers(settings: Settings) -> list[SearchProvider]:
    """Build all configured search providers that are enabled.

    New backends are added by extending this factory; the rest of the
    service only ever sees the :class:`SearchProvider` contract.

    The Camofox provider reuses the shared Camofox client: the factory
    builds the provider (and an injected client) only when the top-level
    ``camofox`` section is enabled.
    """
    providers: list[SearchProvider] = []

    searxng_config = SearxngConfig.from_settings(settings)
    if searxng_config.enabled:
        providers.append(SearxngProvider(searxng_config))

    camofox_config = CamofoxSearchConfig.from_settings(settings)
    if camofox_config.enabled:
        client_config, client = resolve_camofox_client(settings)
        if client is None:
            raise ValueError(
                "search provider 'camofox' is enabled but the camofox section "
                "is disabled; enable the camofox section (base_url, keys) to use it"
            )
        providers.append(CamofoxSearchProvider(camofox_config, client=client))

    return providers
