"""Provider-agnostic YouTube transcript result cache.

A dedicated cache for **normalized** :class:`~app.transcript.models.
TranscriptResponse` objects, sitting **above** individual transcript
providers.  All registered providers (Camofox, ``youtube-transcript-api``,
``yt-dlp`` and others) share this one cache layer; nothing here depends on
any provider's internal cache semantics, and providers stay unaware of it.

Position in the stack::

    request ──> TranscriptCache ──> TranscriptRouter ──> TranscriptProvider
                      │               │
                      └─ SQLite transcript_cache table (its own table in
                         the shared cache database, kept by the same
                         maintenance loop as scrape/search entries)

Cache key
---------
Keys are SHA-256 digests of a canonical JSON encoding of the transcript
*semantics* (see :func:`build_key_material`):

- ``v`` — key schema version (lets the algorithm evolve without collisions)
- ``video_id`` — the canonical 11-character video ID (URL spelling never
  matters: ``watch?v=``, ``youtu.be`` and a bare ID all hash identically)
- ``languages`` — sorted, lowercased (language priority order is
  request-order, but the *set* of acceptable languages identifies the
  result for caching purposes)
- ``provider`` — requested provider or ``auto`` (whitespace-stripped)

TTL
---
An entry expires ``ttl`` seconds after it was stored, where ``ttl`` is the
request's ``cache_ttl_seconds`` when set, else the configured
``default_ttl_seconds`` (0 = entries never expire and are pruned only by
the size/entry-count cleanup phases).  Expired entries are *retained in the
database* and may be served under the stale-if-error rule below.

Stale-if-error
--------------
When a fresh run fails and there is a (possibly expired) cached entry for
the same key, the stale entry may be served **only** when the failure is
retryable, provider-side — the same error family the router treats as
fallback-eligible (provider unavailable / timeout / blocked / invalid
response) plus :class:`~app.core.errors.NoTranscriptProviderAvailableError`
raised because providers are *down* (all candidates skipped as unavailable,
or all eligible attempts failed retryably).  Invalid requests are never
masked by stale data: request validation errors, unconfigured providers and
empty registries propagate unchanged.  *Definitive* video errors (no
captions, language unavailable, video private) are likewise never masked.
A *valid* request with ``force_refresh`` also bypasses the stale path — the
caller explicitly asked for fresh data, so the error is reported.

Response metadata
-----------------
Served responses carry ``metadata.from_cache`` / ``metadata.stale`` plus
``metadata.cached_at`` (when the serving entry was created) and
``metadata.cache_age_seconds``; fresh responses keep all of them
``False`` / ``None``.  Stale serves additionally append a human-readable
warning (using the error type token only — never the raw provider message,
which may contain credential-bearing values).

Isolation (profiles and credentials)
------------------------------------
The key covers only the *transcript semantics* — never provider
credentials, cookies or profile identifiers.  Every planned transcript
provider is stateless (public YouTube data), so that is complete
isolation.  If a future provider uses credentials or profile-specific
behaviour (logged-in YouTube accounts), its key material must grow a
normalized *profile identity* component (a provider-defined opaque token —
**not** the credential itself) so that two profiles of the same backend can
never share entries.

Maintenance
-----------
The ``transcript_cache`` table lives in the same SQLite file as scrape and
search entries (one connection, WAL mode, one maintenance loop) but is a
separate table: the same cleanup limits apply to each table independently,
and statistics report each table separately.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import (
    NoTranscriptProviderAvailableError,
    ScraperError,
    ValidationError,
)
from app.metrics.prometheus import MetricsCollector, get_metrics
from app.transcript.models import TranscriptRequest, TranscriptResponse
from app.transcript.router import FALLBACK_ELIGIBLE_ERRORS, TranscriptRouter

logger = logging.getLogger("scraper-api.transcript.cache")

#: Skip reasons in ``NoTranscriptProviderAvailableError.details["candidates"]``
#: that indicate a *provider-side* availability problem (retryable), as
#: opposed to a request-/config-specific problem (not retryable).  The
#: router uses a fixed reason vocabulary for health-based skips.
_AVAILABILITY_SKIP_REASONS = frozenset(
    {
        "unavailable",
        "unavailable (probe failed)",
        "unhealthy",
        "disabled",
        "unconfigured",
        "unreachable",
    }
)

#: Transcript-cache configuration keys and their environment-variable
#: prefix (module-level: Pydantic treats underscore names as private).
_CACHE_CONFIG_KEYS = ("enabled", "default_ttl_seconds", "stale_if_error")
_CACHE_ENV_PREFIX = "SCRAPER_TRANSCRIPT_CACHE_"


# ==================================================================== config
class TranscriptCacheConfig(BaseModel):
    """Configuration for the transcript result cache.

    Parsed from the ``transcript.cache`` YAML block via
    :meth:`from_settings`, with ``SCRAPER_TRANSCRIPT_CACHE_*`` environment
    variables overriding YAML values (highest priority).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description="Master switch for the transcript result cache.",
    )
    default_ttl_seconds: int = Field(
        default=86400,
        ge=0,
        description=(
            "TTL applied to stored entries when the request does not set "
            "``cache_ttl_seconds`` (1 day: transcripts change rarely). "
            "0 means entries never expire on their own and are pruned only "
            "by the cleanup limits."
        ),
    )
    stale_if_error: bool = Field(
        default=True,
        description=(
            "Serve an expired cached entry when the live transcript fetch "
            "fails with a retryable provider error (never for invalid "
            "requests or definitive video errors)."
        ),
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> TranscriptCacheConfig:
        """Build from the ``transcript.cache`` block of the application settings.

        Priority (highest to lowest):
          1. ``SCRAPER_TRANSCRIPT_CACHE_*`` environment variables
          2. The ``transcript.cache`` YAML block
          3. Hard-coded defaults
        """
        raw_transcript: dict[str, Any] | None = getattr(settings, "raw_yaml_transcript", None)
        block = raw_transcript.get("cache") if isinstance(raw_transcript, dict) else None
        if block is None:
            block = {}
        if not isinstance(block, dict):
            raise ValueError("transcript.cache must be a mapping of configuration values")

        values: dict[str, Any] = {}
        for key in _CACHE_CONFIG_KEYS:
            env_value = os.environ.get(_CACHE_ENV_PREFIX + key.upper())
            yaml_value = block.get(key)
            if env_value is not None:
                values[key] = env_value
            elif yaml_value is not None:
                values[key] = yaml_value
        return cls(**values)


# ==================================================================== models
@dataclass
class TranscriptCacheEntry:
    """One ``transcript_cache`` row with the normalized response restored.

    Mirrors :class:`app.search.cache.SearchCacheEntry` for the transcript
    table.
    """

    cache_key: str
    response: TranscriptResponse
    fetched_at: datetime
    expires_at: datetime | None
    content_length: int

    @property
    def is_expired(self) -> bool:
        """``True`` when the entry's TTL has elapsed. Entries stored without
        an expiry bound (TTL 0) never expire."""
        if self.expires_at is None:
            return False
        if self.expires_at.tzinfo is None:
            expires_at = self.expires_at.replace(tzinfo=UTC)
        else:
            expires_at = self.expires_at
        return datetime.now(UTC) > expires_at

    def age_seconds(self, now: datetime | None = None) -> float:
        """Age of the entry in seconds (never negative)."""
        if now is None:
            now = datetime.now(UTC)
        fetched = self.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
        return max(0.0, (now - fetched).total_seconds())


# ==================================================================== keys
def normalize_languages_for_key(languages: list[str]) -> list[str]:
    """Normalize language tags for cache-key purposes.

    Tags are stripped, casefolded and sorted so that two requests that
    accept the same set of languages (in any order or casing) share one
    cache entry.
    """
    return sorted(tag.strip().casefold() for tag in languages if tag.strip())


def build_key_material(request: TranscriptRequest) -> dict[str, Any]:
    """Canonical key material for a transcript request (see module docstring)."""
    return {
        "v": 1,
        "video_id": request.video_id,
        "languages": normalize_languages_for_key(request.languages),
        "provider": request.provider.strip(),
    }


def make_transcript_cache_key(request: TranscriptRequest) -> str:
    """Deterministic SHA-256 cache key for a transcript request.

    Equivalent requests (same video, same language set, any JSON field
    order, any language casing/order) always map to the same 64-character
    hex key.
    """
    canonical = json.dumps(build_key_material(request), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ===================================================================== cache
class TranscriptCache:
    """Dedicated, provider-agnostic cache for normalized transcript responses.

    Wraps a :class:`app.transcript.router.TranscriptRouter`: every registered
    provider benefits from the same cache, and provider implementations stay
    cache-unaware.  Storage is the ``transcript_cache`` table of a shared
    :class:`app.cache.sqlite_cache.SqliteCache` instance.
    """

    def __init__(
        self,
        store: SqliteCache,
        router: TranscriptRouter,
        config: TranscriptCacheConfig | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._store = store
        self._router = router
        self._config = config or TranscriptCacheConfig()
        self._metrics = metrics if metrics is not None else get_metrics()

    # ---------------------------------------------------------------- access
    @property
    def config(self) -> TranscriptCacheConfig:
        return self._config

    @property
    def router(self) -> TranscriptRouter:
        return self._router

    # ------------------------------------------------------------------ main
    async def fetch(self, request: TranscriptRequest | dict[str, Any]) -> TranscriptResponse:
        """Serve a transcript backed by the result cache.

        ``request`` may be a :class:`TranscriptRequest` or a plain mapping;
        mappings are validated first, and invalid mappings raise
        :class:`~app.core.errors.ValidationError` *without* touching the
        cache (stale entries can never mask an invalid request).

        Behaviour:
          - valid + fresh entry in cache → serve it (``from_cache=True``)
          - cache disabled / forced refresh / expired entry + success →
            fresh run, result stored (subject to the per-request TTL)
          - expired entry + retryable provider failure → stale serve
            (``from_cache=True, stale=True``) when ``stale_if_error`` is on
          - expired entry + definitive video error or invalid request →
            error propagates
        """
        start = time.monotonic()
        query = self._coerce_request(request)

        if not self._config.enabled:
            return await self._router.route(query)

        cache_key = make_transcript_cache_key(query)
        cached: TranscriptCacheEntry | None = None
        if not query.force_refresh:
            cached = self._get(cache_key)
            if cached is not None and not cached.is_expired:
                self._metrics.inc("transcript_cache_hits_total")
                logger.debug(
                    "Transcript cache hit for %s (key=%s…)", query.video_id, cache_key[:16]
                )
                self._stamp_served(cached.response, cached, start, stale=False)
                return cached.response

        try:
            response = await self._router.route(query)
        except ScraperError as exc:
            self._metrics.inc("transcript_cache_errors_total")
            if (
                self._config.stale_if_error
                and cached is not None
                and not query.force_refresh
                and self._stale_eligible(exc)
            ):
                age = cached.age_seconds()
                logger.warning(
                    "Transcript failed with %s — serving stale entry for %s (%.0fs old)",
                    exc.error_type,
                    query.video_id,
                    age,
                )
                self._metrics.inc("transcript_cache_stale_hits_total")
                self._stamp_served(
                    cached.response,
                    cached,
                    start,
                    stale=True,
                    warning=(
                        f"stale cache entry served: live transcript failed with {exc.error_type}"
                    ),
                )
                return cached.response
            raise

        self._metrics.inc("transcript_cache_misses_total")
        self._put(cache_key, query, response)
        self._stamp_fresh(response)
        return response

    # ------------------------------------------------------------- validation
    @staticmethod
    def _coerce_request(request: TranscriptRequest | dict[str, Any]) -> TranscriptRequest:
        """Validate a request (dict or model) before any cache interaction."""
        if isinstance(request, TranscriptRequest):
            return request
        try:
            return TranscriptRequest.model_validate(request)
        except PydanticValidationError as exc:
            details = [
                {
                    "field": ".".join(str(part) for part in error.get("loc", ())),
                    "message": str(error.get("msg", "invalid value")),
                }
                for error in exc.errors()[:5]
            ]
            raise ValidationError(
                "Invalid transcript request", details={"errors": details}
            ) from exc

    # -------------------------------------------------------------- stale-if-error
    @staticmethod
    def _stale_eligible(exc: Exception) -> bool:
        """Whether a failed fresh run may be covered by a stale entry.

        Only *retryable, provider-side* failures qualify:

        - any error in :data:`app.transcript.router.FALLBACK_ELIGIBLE_ERRORS`
          (provider unavailable, provider timeout, provider blocked,
          invalid provider response)
        - ``NoTranscriptProviderAvailableError`` where the *providers are
          down* — either every candidate was skipped as unavailable, or
          every eligible attempt failed retryably (``details`` carries
          ``providers_attempted``)

        Never: request validation errors, unconfigured providers, empty
        registries, definitive video errors (no captions, language
        unavailable, video private — none of which are even
        :class:`ScraperError`-family retryable failures), or non-ScraperError
        exceptions.  Serving stale data for any of those would hide an
        invalid request or a fact about the video.
        """
        if isinstance(exc, (ValidationError, NoTranscriptProviderAvailableError)):
            if isinstance(exc, NoTranscriptProviderAvailableError):
                details = exc.details
                if details.get("providers_attempted"):
                    # All eligible providers failed retryably → providers are
                    # having a bad moment; stale is the right mitigation.
                    return True
                candidates = details.get("candidates")
                if candidates:
                    # Every candidate must be skipped for an availability
                    # reason.  Anything else means *this request or the
                    # configuration* cannot be satisfied, which stale data
                    # must not mask.
                    return all(
                        str(entry.get("reason", "")).lower() in _AVAILABILITY_SKIP_REASONS
                        or str(entry.get("reason", "")).startswith("unavailable")
                        for entry in candidates
                        if isinstance(entry, dict)
                    )
            return False
        if isinstance(exc, FALLBACK_ELIGIBLE_ERRORS):
            return True
        return False

    # ------------------------------------------------------------- metadata
    @staticmethod
    def _stamp_served(
        response: TranscriptResponse,
        entry: TranscriptCacheEntry,
        start: float,
        *,
        stale: bool,
        warning: str | None = None,
    ) -> None:
        metadata = response.metadata
        metadata.from_cache = True
        metadata.stale = stale
        metadata.cached_at = entry.fetched_at.isoformat()
        metadata.cache_age_seconds = round(entry.age_seconds(), 3)
        metadata.elapsed_ms = max(0, int((time.monotonic() - start) * 1000))
        if warning is not None and warning not in metadata.warnings:
            metadata.warnings.append(warning)

    @staticmethod
    def _stamp_fresh(response: TranscriptResponse) -> None:
        """Normalize metadata on a freshly-executed response.

        The router leaves ``from_cache`` / ``stale`` at their ``False``
        defaults; the explicit stamp also neutralizes a provider that
        (incorrectly) set them itself.
        """
        metadata = response.metadata
        metadata.from_cache = False
        metadata.stale = False
        metadata.cached_at = None
        metadata.cache_age_seconds = None

    # --------------------------------------------------------------- storage
    def _ttl_seconds(self, query: TranscriptRequest) -> int:
        if query.cache_ttl_seconds is not None:
            return query.cache_ttl_seconds
        return self._config.default_ttl_seconds

    def _get(self, cache_key: str) -> TranscriptCacheEntry | None:
        """Read + deserialize one entry.

        Storage failures degrade to a cache miss (the transcript still runs
        live); corrupt entries are discarded rather than surfacing a 500.
        """
        try:
            row = self._store.get_transcript_entry(cache_key)
        except Exception:  # noqa: BLE001 - storage must never break a request
            logger.warning(
                "Transcript cache read failed for %s… — treating as miss", cache_key[:16]
            )
            return None
        if row is None:
            return None
        try:
            response = TranscriptResponse.model_validate_json(row["response"])
        except Exception:  # noqa: BLE001 - corrupt entry: discard, never 500
            logger.warning("Discarding corrupt transcript cache entry %s…", cache_key[:16])
            self._discard(cache_key)
            return None
        return TranscriptCacheEntry(
            cache_key=row["cache_key"],
            response=response,
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            content_length=row["content_length"],
        )

    def _put(self, cache_key: str, query: TranscriptRequest, response: TranscriptResponse) -> None:
        """Store a fresh response.  A storage failure degrades to an uncached
        fresh response (the result is still returned to the caller)."""
        ttl = self._ttl_seconds(query)
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl) if ttl > 0 else None
        response_json = response.model_dump_json()
        request_json = json.dumps(build_key_material(query), sort_keys=True, separators=(",", ":"))
        try:
            self._store.set_transcript_entry(
                cache_key=cache_key,
                request=request_json,
                response=response_json,
                fetched_at=now,
                expires_at=expires_at,
                content_length=len(response_json.encode("utf-8")),
            )
        except Exception:  # noqa: BLE001 - cache write must never break a request
            logger.warning(
                "Transcript cache write failed for %s… — returning uncached response",
                cache_key[:16],
            )

    def _discard(self, cache_key: str) -> None:
        try:
            self._store.delete_transcript_entry(cache_key)
        except Exception:  # noqa: BLE001 - best effort
            logger.warning("Failed to discard corrupt transcript cache entry %s…", cache_key[:16])
