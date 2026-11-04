"""Provider-agnostic YouTube transcript domain.

Public contract:

- Domain models: :class:`TranscriptRequest`, :class:`TranscriptSegment`,
  :class:`TranscriptMetadata`, :class:`TranscriptResponse`.
- Video identification: :func:`parse_video_id`,
  :func:`normalize_languages`.
- Provider contract: :class:`TranscriptProvider`,
  :class:`TranscriptHealth`.
- Registry and routing: :class:`TranscriptProviderRegistry`,
  :class:`TranscriptRouter`, :class:`TranscriptRouterConfig`,
  :func:`build_transcript_router`.
- Result cache (above the providers): :class:`TranscriptCache`,
  :class:`TranscriptCacheConfig`, :class:`TranscriptCacheEntry`,
  :func:`make_transcript_cache_key`, :func:`build_transcript_cache`.

Transcript-specific errors live in :mod:`app.core.errors`.
"""

from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.metrics.prometheus import MetricsCollector
from app.transcript.cache import (
    TranscriptCache,
    TranscriptCacheConfig,
    TranscriptCacheEntry,
    build_key_material,
    make_transcript_cache_key,
)
from app.transcript.contract import TranscriptHealth, TranscriptProvider
from app.transcript.models import (
    TranscriptMetadata,
    TranscriptRequest,
    TranscriptResponse,
    TranscriptSegment,
    normalize_languages,
    parse_video_id,
)
from app.transcript.providers import build_transcript_providers
from app.transcript.registry import TranscriptProviderRegistry
from app.transcript.router import (
    AUTO_PROVIDER,
    TranscriptRouter,
    TranscriptRouterConfig,
    build_transcript_router,
)

__all__ = [
    "AUTO_PROVIDER",
    "TranscriptCache",
    "TranscriptCacheConfig",
    "TranscriptCacheEntry",
    "TranscriptHealth",
    "TranscriptMetadata",
    "TranscriptProvider",
    "TranscriptProviderRegistry",
    "TranscriptRequest",
    "TranscriptResponse",
    "TranscriptRouter",
    "TranscriptRouterConfig",
    "TranscriptSegment",
    "build_key_material",
    "build_transcript_cache",
    "build_transcript_providers",
    "build_transcript_router",
    "make_transcript_cache_key",
    "normalize_languages",
    "parse_video_id",
]


def build_transcript_cache(
    settings: Settings,
    router: TranscriptRouter,
    store: SqliteCache,
    metrics: MetricsCollector | None = None,
) -> TranscriptCache:
    """Build the provider-agnostic transcript result cache from settings.

    The cache wraps ``router`` (all providers share it) and stores
    normalized responses in the ``transcript_cache`` table of the shared
    ``store``.  ``metrics`` overrides the global metrics collector (used by
    tests).
    """
    return TranscriptCache(
        store=store,
        router=router,
        config=TranscriptCacheConfig.from_settings(settings),
        metrics=metrics,
    )
