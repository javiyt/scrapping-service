"""Provider-agnostic search domain.

Public contract:

- Domain models: ``SearchQuery``, ``SearchResult``, ``SearchMetadata``,
  ``SearchResponse``, ``SearchCapabilities``.
- Enums: ``Vertical``, ``TimeRange``, ``SafeSearch``.
- Provider contract: ``SearchProvider``.
- Registry and routing: ``SearchProviderRegistry``, ``SearchRouter``,
  ``SearchRouterConfig``, ``build_search_router``.
- Result cache (above the providers): ``SearchCache``,
  ``SearchCacheConfig``, ``SearchCacheEntry``, ``make_search_cache_key``,
  ``normalize_query``, ``build_search_cache``.

Search-specific errors live in :mod:`app.core.errors`.
"""

from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.metrics.prometheus import MetricsCollector
from app.search.cache import (
    SearchCache,
    SearchCacheConfig,
    SearchCacheEntry,
    build_key_material,
    make_search_cache_key,
    normalize_query,
)
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
    "SearchCache",
    "SearchCacheConfig",
    "SearchCacheEntry",
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
    "build_key_material",
    "build_search_cache",
    "build_search_router",
    "make_search_cache_key",
    "normalize_query",
]


def build_search_router(
    settings: Settings,
    metrics: MetricsCollector | None = None,
) -> SearchRouter:
    """Build the search router from application settings.

    Constructs the enabled providers (via :func:`build_search_providers`),
    registers them, applies the ``search`` routing configuration
    (``default_provider`` / ``provider_order`` / ``probe_timeout_seconds``)
    and returns a ready-to-route :class:`SearchRouter`.

    ``metrics`` overrides the global metrics collector (used by tests); the
    router records per-provider attempt/success/error counters on it.

    Raises:
        ValueError: on configuration errors (duplicate provider names, a
            ``default_provider`` that is not registered, ...).
    """
    registry = SearchProviderRegistry(build_search_providers(settings))
    return SearchRouter(registry, SearchRouterConfig.from_settings(settings), metrics=metrics)


def build_search_cache(
    settings: Settings,
    router: SearchRouter,
    store: SqliteCache,
    metrics: MetricsCollector | None = None,
) -> SearchCache:
    """Build the provider-agnostic search result cache from settings.

    The cache wraps ``router`` (all providers share it) and stores normalized
    responses in the ``search_cache`` table of the shared ``store``.  ``metrics``
    overrides the global metrics collector (used by tests).
    """
    return SearchCache(
        store=store,
        router=router,
        config=SearchCacheConfig.from_settings(settings),
        metrics=metrics,
    )
