"""Provider-agnostic search domain.

Public contract:

- Domain models: ``SearchQuery``, ``SearchResult``, ``SearchMetadata``,
  ``SearchResponse``, ``SearchCapabilities``.
- Enums: ``Vertical``, ``TimeRange``, ``SafeSearch``.
- Provider contract: ``SearchProvider``.
- Registry and routing: ``SearchProviderRegistry``, ``SearchRouter``,
  ``SearchRouterConfig``, ``build_search_router``.

Search-specific errors live in :mod:`app.core.errors`.
"""

from app.core.config import Settings
from app.search.contract import SearchProvider
from app.search.models import (
    SafeSearch,
    SearchCapabilities,
    SearchMetadata,
    SearchQuery,
    SearchResponse,
    SearchResult,
    TimeRange,
    Vertical,
)
from app.search.providers import build_search_providers
from app.search.registry import SearchProviderRegistry
from app.search.router import (
    AUTO_PROVIDER,
    SearchRouter,
    SearchRouterConfig,
)

__all__ = [
    "AUTO_PROVIDER",
    "SafeSearch",
    "SearchCapabilities",
    "SearchMetadata",
    "SearchProvider",
    "SearchProviderRegistry",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
    "SearchRouter",
    "SearchRouterConfig",
    "TimeRange",
    "Vertical",
    "build_search_router",
]


def build_search_router(settings: Settings) -> SearchRouter:
    """Build the search router from application settings.

    Constructs the enabled providers (via :func:`build_search_providers`),
    registers them, applies the ``search`` routing configuration
    (``default_provider`` / ``provider_order`` / ``probe_timeout_seconds``)
    and returns a ready-to-route :class:`SearchRouter`.

    Raises:
        ValueError: on configuration errors (duplicate provider names, a
            ``default_provider`` that is not registered, ...).
    """
    registry = SearchProviderRegistry(build_search_providers(settings))
    return SearchRouter(registry, SearchRouterConfig.from_settings(settings))
