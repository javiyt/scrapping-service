"""Provider-agnostic search result cache.

A dedicated cache for **normalized** :class:`~app.search.models.SearchResponse`
objects, sitting **above** individual search providers.  All registered
providers (SearXNG today, Camofox and others tomorrow) share this one cache
layer; nothing here depends on any provider's internal cache semantics, and
providers stay unaware of it.

Position in the stack::

    request ──> SearchCache ──> SearchRouter ──> SearchProvider
                      │               │
                      └─ SQLite search_cache table (its own table in the
                         shared cache database, kept by the same maintenance
                         loop as scrape entries)

Cache key
---------
Keys are SHA-256 digests of a canonical JSON encoding of the search
*semantics* (see :func:`build_key_material`):

- ``v`` — key schema version (lets the algorithm evolve without collisions)
- ``query`` — normalized: trimmed, whitespace-collapsed, casefolded
- ``provider`` — requested provider or ``auto`` (whitespace-stripped)
- ``verticals`` — sorted vertical values
- ``language`` / ``time_range`` / ``safe_search`` — as requested (or ``None``)
- ``page`` / ``limit`` — as requested (or ``None``)
- ``sources`` — sorted, lowercased (source names are engine identifiers and
  are matched case-insensitively by current providers)

Because the material is a sorted, canonically-serialized mapping, two
requests with equivalent semantics always produce the same key, no matter
in which order JSON object fields were supplied.

TTL
---
An entry expires ``ttl`` seconds after it was stored, where ``ttl`` is the
request's ``cache_ttl_seconds`` when set, else the configured
``default_ttl_seconds`` (0 = entries never expire and are pruned only by the
size/entry-count cleanup phases).  Expired entries are *retained in the
database* and may be served under the stale-if-error rule below; the
maintenance loop removes them after the normal expiry grace period.

Stale-if-error
--------------
When a fresh run fails and there is a (possibly expired) cached entry for the
same key, the stale entry may be served **only** when the failure is
retryable, provider-side — the same error family the router treats as
fallback-eligible (provider unavailable / timeout / invalid response) plus
``NoProviderAvailableError`` raised because providers are *down* (all
candidates skipped as unavailable, or all eligible attempts failed
transiently).  Invalid requests are never masked by stale data: request
validation errors, capability mismatches, unconfigured providers and empty
registries propagate unchanged.  A *valid* request with
``force_refresh`` also bypasses the stale path — the caller explicitly asked
for fresh data, so the error is reported.

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
The key covers only the *search semantics* — never provider credentials,
API keys, cookies or profile identifiers.  Today every registered provider
is stateless, so that is complete isolation.  If a future provider uses
credentials or profile-specific behaviour (proxies, logged-in sessions,
account-bound result sets), its key material must grow a normalized
*profile identity* component (a provider-defined opaque token — **not** the
credential itself) so that two profiles of the same engine can never share
entries.  Until such a component exists, a profile-bound provider must not
register with this shared cache, or it must run through a cache instance
configured with a fixed per-profile namespace.

Maintenance
-----------
The ``search_cache`` table lives in the same SQLite file as scrape entries
(one connection, WAL mode, one maintenance loop) but is a separate table:
the same cleanup limits apply to each table independently, and statistics
report each table separately.  Nothing in the scrape table is ever touched
by search-cache operations, and vice versa.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import (
    NoProviderAvailableError,
    ScraperError,
    UnsupportedSearchCapabilityError,
    ValidationError,
)
from app.metrics.prometheus import MetricsCollector, get_metrics
from app.search.models import SearchQuery, SearchResponse
from app.search.router import FALLBACK_ELIGIBLE_ERRORS, SearchRouter

logger = logging.getLogger("scraper-api.search.cache")

#: Skip reasons in ``NoProviderAvailableError.details["candidates"]`` that
#: indicate a *provider-side* availability problem (retryable), as opposed
#: to a request-specific capability mismatch (not retryable).
_AVAILABILITY_SKIP_REASONS = {"unavailable", "unavailable (probe failed)"}

#: Search-cache configuration keys and their environment-variable prefix
#: (module-level: Pydantic treats underscore names as private attributes).
_CACHE_CONFIG_KEYS = ("enabled", "default_ttl_seconds", "stale_if_error")
_CACHE_ENV_PREFIX = "SCRAPER_SEARCH_CACHE_"


# ==================================================================== config
class SearchCacheConfig(BaseModel):
    """Configuration for the search result cache.

    Parsed from the ``search.cache`` YAML block via
    :meth:`from_settings`, with ``SCRAPER_SEARCH_CACHE_*`` environment
    variables overriding YAML values (highest priority).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description="Master switch for the search result cache.",
    )
    default_ttl_seconds: int = Field(
        default=900,
        ge=0,
        description=(
            "TTL applied to stored entries when the request does not set "
            "``cache_ttl_seconds``. 0 means entries never expire on their "
            "own and are pruned only by the cleanup limits."
        ),
    )
    stale_if_error: bool = Field(
        default=True,
        description=(
            "Serve an expired cached entry when the live search fails with a "
            "retryable provider error (never for invalid requests)."
        ),
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> SearchCacheConfig:
        """Build from the ``search.cache`` block of the application settings.

        Priority (highest to lowest):
          1. ``SCRAPER_SEARCH_CACHE_*`` environment variables
          2. The ``search.cache`` YAML block
          3. Hard-coded defaults
        """
        raw_search: dict[str, Any] | None = getattr(settings, "raw_yaml_search", None)
        block = raw_search.get("cache") if isinstance(raw_search, dict) else None
        if block is None:
            block = {}
        if not isinstance(block, dict):
            raise ValueError("search.cache must be a mapping of configuration values")

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
class SearchCacheEntry:
    """One ``search_cache`` row with the normalized response restored.

    Mirrors :class:`app.cache.models.CacheEntry` for the search table.
    """

    cache_key: str
    response: SearchResponse
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
_WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_query(query: str) -> str:
    """Normalize a query string for cache-key purposes.

    Leading/trailing whitespace is trimmed, internal whitespace runs are
    collapsed to single spaces, and the result is casefolded — matching how
    search engines and providers treat text queries (whitespace- and
    case-insensitive).
    """
    return _WHITESPACE_PATTERN.sub(" ", query.strip()).casefold()


def build_key_material(request: SearchQuery) -> dict[str, Any]:
    """Canonical key material for a search request (see module docstring)."""
    return {
        "v": 1,
        "query": normalize_query(request.query),
        "provider": request.provider.strip(),
        "verticals": sorted(vertical.value for vertical in request.verticals),
        "language": request.language,
        "time_range": request.time_range.value if request.time_range is not None else None,
        "safe_search": request.safe_search.value,
        "page": request.page,
        "limit": request.limit,
        "sources": sorted(source.lower() for source in request.sources),
    }


def make_search_cache_key(request: SearchQuery) -> str:
    """Deterministic SHA-256 cache key for a search request.

    Equivalent requests (same semantics, any JSON field order, any query
    casing/whitespace) always map to the same 64-character hex key.
    """
    canonical = json.dumps(build_key_material(request), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ===================================================================== cache
class SearchCache:
    """Dedicated, provider-agnostic cache for normalized search responses.

    Wraps a :class:`app.search.SearchRouter`: every registered provider
    benefits from the same cache, and provider implementations stay
    cache-unaware.  Storage is the ``search_cache`` table of a shared
    :class:`app.cache.sqlite_cache.SqliteCache` instance.
    """

    def __init__(
        self,
        store: SqliteCache,
        router: SearchRouter,
        config: SearchCacheConfig | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._store = store
        self._router = router
        self._config = config or SearchCacheConfig()
        self._metrics = metrics if metrics is not None else get_metrics()

    # ---------------------------------------------------------------- access
    @property
    def config(self) -> SearchCacheConfig:
        return self._config

    @property
    def router(self) -> SearchRouter:
        return self._router

    # ------------------------------------------------------------------ main
    async def search(self, request: SearchQuery | dict[str, Any]) -> SearchResponse:
        """Serve a search backed by the result cache.

        ``request`` may be a :class:`SearchQuery` or a plain mapping;
        mappings are validated first, and invalid mappings raise
        :class:`~app.core.errors.ValidationError` *without* touching the
        cache (stale entries can never mask an invalid request).

        Behaviour:
          - valid + fresh entry in cache → serve it (``from_cache=True``)
          - cache disabled / forced refresh / expired entry + success →
            fresh run, result stored (subject to the per-request TTL)
          - expired entry + retryable provider failure → stale serve
            (``from_cache=True, stale=True``) when ``stale_if_error`` is on
          - expired entry + deterministic/invalid failure → error propagates
        """
        start = time.monotonic()
        query = self._coerce_request(request)

        if not self._config.enabled:
            return await self._router.route(query)

        cache_key = make_search_cache_key(query)
        cached: SearchCacheEntry | None = None
        if not query.force_refresh:
            cached = self._get(cache_key)
            if cached is not None and not cached.is_expired:
                self._metrics.inc("search_cache_hits_total")
                logger.debug("Search cache hit for %r (key=%s…)", query.query, cache_key[:16])
                self._stamp_served(cached.response, cached, start, stale=False)
                return cached.response

        try:
            response = await self._router.route(query)
        except ScraperError as exc:
            self._metrics.inc("search_cache_errors_total")
            if (
                self._config.stale_if_error
                and cached is not None
                and not query.force_refresh
                and self._stale_eligible(exc)
            ):
                age = cached.age_seconds()
                logger.warning(
                    "Search failed with %s — serving stale entry for %r (%.0fs old)",
                    exc.error_type,
                    query.query,
                    age,
                )
                self._metrics.inc("search_cache_stale_hits_total")
                self._stamp_served(
                    cached.response,
                    cached,
                    start,
                    stale=True,
                    warning=(f"stale cache entry served: live search failed with {exc.error_type}"),
                )
                return cached.response
            raise

        self._metrics.inc("search_cache_misses_total")
        self._put(cache_key, query, response)
        self._stamp_fresh(response)
        return response

    # ------------------------------------------------------------- validation
    @staticmethod
    def _coerce_request(request: SearchQuery | dict[str, Any]) -> SearchQuery:
        """Validate a request (dict or model) before any cache interaction."""
        if isinstance(request, SearchQuery):
            return request
        try:
            return SearchQuery.model_validate(request)
        except PydanticValidationError as exc:
            details = [
                {
                    "field": ".".join(str(part) for part in error.get("loc", ())),
                    "message": str(error.get("msg", "invalid value")),
                }
                for error in exc.errors()[:5]
            ]
            raise ValidationError("Invalid search request", details={"errors": details}) from exc

    # -------------------------------------------------------------- stale-if-error
    @staticmethod
    def _stale_eligible(exc: Exception) -> bool:
        """Whether a failed fresh run may be covered by a stale entry.

        Only *retryable, provider-side* failures qualify:

        - any error in :data:`app.search.router.FALLBACK_ELIGIBLE_ERRORS`
          (provider unavailable, provider timeout, invalid provider response)
        - ``NoProviderAvailableError`` where the *providers are down* —
          either every explicit/auto candidate was skipped as unavailable,
          or every eligible attempt failed transiently (``details`` carries
          ``providers_attempted``)

        Never: request validation errors, capability mismatches,
        unconfigured providers, empty registries, or non-ScraperError
        exceptions.  Serving stale data for any of those would hide an
        invalid request or a configuration problem.
        """
        if isinstance(exc, (ValidationError, UnsupportedSearchCapabilityError)):
            # Deterministic, request-specific failures (also covers the
            # unconfigured-provider case, which raises ValidationError).
            return False
        if isinstance(exc, FALLBACK_ELIGIBLE_ERRORS):
            return True
        if isinstance(exc, NoProviderAvailableError):
            details = exc.details
            if details.get("providers_attempted"):
                # All eligible providers failed transiently → providers are
                # having a bad moment; stale is the right mitigation.
                return True
            candidates = details.get("candidates")
            if candidates:
                # Every candidate must be skipped for an availability reason.
                # Any "capability mismatch" among them means *this request*
                # cannot be satisfied, which stale data must not mask.
                return all(
                    entry.get("reason") in _AVAILABILITY_SKIP_REASONS
                    for entry in candidates
                    if isinstance(entry, dict)
                )
            # {"reason": "empty registry"} or anything unexpected → not
            # a transient provider failure.
            return False
        return False

    # ------------------------------------------------------------- metadata
    @staticmethod
    def _stamp_served(
        response: SearchResponse,
        entry: SearchCacheEntry,
        start: float,
        *,
        stale: bool,
        warning: str | None = None,
    ) -> None:
        metadata = response.metadata
        metadata.from_cache = True
        metadata.stale = stale
        metadata.cached_at = entry.fetched_at
        metadata.cache_age_seconds = round(entry.age_seconds(), 3)
        metadata.elapsed_ms = max(0, int((time.monotonic() - start) * 1000))
        if warning is not None and warning not in metadata.warnings:
            metadata.warnings.append(warning)

    @staticmethod
    def _stamp_fresh(response: SearchResponse) -> None:
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
    def _ttl_seconds(self, query: SearchQuery) -> int:
        if query.cache_ttl_seconds is not None:
            return query.cache_ttl_seconds
        return self._config.default_ttl_seconds

    def _get(self, cache_key: str) -> SearchCacheEntry | None:
        """Read + deserialize one entry.

        Storage failures degrade to a cache miss (the search still runs
        live); corrupt entries are discarded rather than surfacing a 500.
        """
        try:
            row = self._store.get_search_entry(cache_key)
        except Exception:  # noqa: BLE001 - storage must never break a search
            logger.warning("Search cache read failed for %s… — treating as miss", cache_key[:16])
            return None
        if row is None:
            return None
        try:
            response = SearchResponse.model_validate_json(row["response"])
        except Exception:  # noqa: BLE001 - corrupt entry: discard, never 500
            logger.warning("Discarding corrupt search cache entry %s…", cache_key[:16])
            self._discard(cache_key)
            return None
        return SearchCacheEntry(
            cache_key=row["cache_key"],
            response=response,
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            content_length=row["content_length"],
        )

    def _put(self, cache_key: str, query: SearchQuery, response: SearchResponse) -> None:
        """Store a fresh response.  A storage failure degrades to an uncached
        fresh response (the result is still returned to the caller)."""
        ttl = self._ttl_seconds(query)
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl) if ttl > 0 else None
        response_json = response.model_dump_json()
        request_json = json.dumps(build_key_material(query), sort_keys=True, separators=(",", ":"))
        try:
            self._store.set_search_entry(
                cache_key=cache_key,
                request=request_json,
                response=response_json,
                fetched_at=now,
                expires_at=expires_at,
                content_length=len(response_json.encode("utf-8")),
            )
        except Exception:  # noqa: BLE001 - cache write must never break a search
            logger.warning(
                "Search cache write failed for %s… — returning uncached response",
                cache_key[:16],
            )

    def _discard(self, cache_key: str) -> None:
        try:
            self._store.delete_search_entry(cache_key)
        except Exception:  # noqa: BLE001 - best effort
            logger.warning("Failed to discard corrupt search cache entry %s…", cache_key[:16])
