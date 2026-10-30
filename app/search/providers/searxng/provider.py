"""SearXNG implementation of the :class:`SearchProvider` contract.

This adapter translates normalized search requests
(:class:`app.search.models.SearchQuery`) into SearXNG's JSON search API and
normalizes SearXNG's JSON responses back into project-owned domain types.
Clients never see SearXNG's raw API contract:

- ``/config`` is used to discover the instance's enabled categories and
  engines (never a fixed installation is assumed); the discovery result is
  cached for ``SearxngConfig.config_cache_ttl_seconds``.
- Verticals are mapped to SearXNG categories, generic sources are validated
  against the configured engines, and anything the instance cannot honor is
  reported through ``SearchMetadata.degraded_capabilities`` / ``warnings``.
- Failures raise the search error types from :mod:`app.core.errors`
  (unavailable, timeout, invalid provider response) — never broken results.

Request mapping (normalized → SearXNG ``/search`` query parameters):

============================================  ==========================
Normalized                                    SearXNG parameter
============================================  ==========================
``query``                                     ``q``
``verticals`` (enabled subset)                ``categories`` (comma list)
``sources`` (validated engines)               ``engines`` (comma list)
``language`` / configured default             ``language``
``time_range``                                ``time_range``
``safe_search``                               ``safesearch`` (0/1/2)
``page``                                      ``pageno``
— (always)                                    ``format=json``
============================================  ==========================

Result normalization (SearXNG result dict → :class:`SearchResult`):

====================================  ==========================
SearXNG field                         Domain field
====================================  ==========================
``title``                             ``title``
``url`` (must be absolute http(s))    ``url``
``content``                           ``snippet``
position in provider ordering         ``rank``
``category``                          ``vertical`` (WEB fallback)
``engine`` / first of ``engines``     ``source``
``img_src`` / ``thumbnail``           ``thumbnail_url``
``publishedDate`` (ISO 8601)          ``published_at``
====================================  ==========================

SearXNG's ``score`` is intentionally **not** mapped: it is a per-engine
heuristic with no consistent scale, and must not be used as a
cross-provider relevance score.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import (
    InvalidProviderResponseError,
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
)
from app.search.models import (
    SearchCapabilities,
    SearchMetadata,
    SearchQuery,
    SearchResponse,
    SearchResult,
    TimeRange,
    Vertical,
)
from app.search.providers.searxng.config import NAME, SearxngConfig
from app.search.providers.searxng.mapping import (
    SAFE_SEARCH_LEVELS,
    TIME_RANGE_PARAMS,
    VERTICAL_TO_CATEGORY,
    category_to_vertical,
)

logger = logging.getLogger("scraper-api.search.searxng")

# httpx exceptions we treat as definite provider timeouts (vs. other errors).
_HTTPX_TIMEOUT_EXCEPTIONS = (
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.TimeoutException,
)

_HTTP_URL_PREFIXES = ("http://", "https://")


# ================================================================= discovery
class SearxngEngine(BaseModel):
    """One engine entry from SearXNG's ``/config`` endpoint."""

    model_config = ConfigDict(extra="ignore")

    name: str
    categories: list[str] = Field(default_factory=list)
    shortcut: str | None = None
    enabled: bool = False
    paging: bool = False
    language_support: bool = False
    safesearch: int = 0
    time_range_support: bool = False


class SearxngInstance(BaseModel):
    """Discovered state of a SearXNG instance (from ``/config``).

    ``/config`` reports *all* configured categories, but only the union of
    categories covered by **enabled** engines is actually searchable; the
    same applies to engines.
    """

    model_config = ConfigDict(extra="ignore")

    instance_name: str | None = None
    version: str | None = None
    categories: list[str] = Field(default_factory=list)
    engines: list[SearxngEngine] = Field(default_factory=list)
    safe_search: int | None = None
    fetched_at: float = Field(default=0.0, description="monotonic timestamp of the fetch")

    @classmethod
    def from_api_payload(cls, payload: dict[str, Any]) -> SearxngInstance:
        return cls(
            instance_name=payload.get("instance_name"),
            version=payload.get("version"),
            categories=list(payload.get("categories") or []),
            engines=[
                SearxngEngine(**engine)
                for engine in (payload.get("engines") or [])
                if isinstance(engine, dict)
            ],
            safe_search=payload.get("safe_search"),
            fetched_at=time.monotonic(),
        )

    @property
    def enabled_categories(self) -> set[str]:
        """Categories covered by at least one enabled engine."""
        categories: set[str] = set()
        for engine in self.engines:
            if engine.enabled:
                categories.update(engine.categories)
        return categories

    @property
    def enabled_engines(self) -> dict[str, SearxngEngine]:
        """Enabled engines keyed by (lower-cased) name."""
        return {engine.name.lower(): engine for engine in self.engines if engine.enabled}


# ==================================================================== provider
class SearxngProvider:
    """SearXNG backend for the provider-agnostic search contract.

    Satisfies the structural :class:`app.search.SearchProvider` protocol
    (``name`` + ``capabilities()`` + ``search()``).
    """

    def __init__(
        self,
        config: SearxngConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(
                base_url=config.base_url or "",
                timeout=httpx.Timeout(config.timeout_seconds),
                headers={"Accept": "application/json"},
                follow_redirects=False,
            )
        self._client = client
        self._instance: SearxngInstance | None = None
        self._instance_fetched_at: float = 0.0
        self._discovery_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return NAME

    async def close(self) -> None:
        """Release the HTTP client (only closes internally owned clients)."""
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ discovery
    async def _fetch_instance(self) -> SearxngInstance:
        """Fetch and parse ``/config``. Raises on any failure."""
        try:
            response = await self._client.get("/config")
        except _HTTPX_TIMEOUT_EXCEPTIONS as exc:
            raise SearchProviderTimeoutError(
                f"SearXNG /config request timed out after {self._config.timeout_seconds}s",
                details={
                    "base_url": self._config.base_url,
                    "timeout_seconds": self._config.timeout_seconds,
                },
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderUnavailableError(
                f"SearXNG instance at {self._config.base_url} is unreachable: {exc}",
                details={"base_url": self._config.base_url},
            ) from exc

        if response.status_code != 200:
            raise SearchProviderUnavailableError(
                f"SearXNG /config returned HTTP {response.status_code}",
                details={
                    "base_url": self._config.base_url,
                    "status_code": response.status_code,
                    "body_preview": response.text[:300],
                },
            )

        payload = _parse_json_payload(response, "/config")
        return SearxngInstance.from_api_payload(payload)

    async def _get_instance(self) -> SearxngInstance:
        """Return the discovered instance state, refreshing when the cache is stale.

        The discovery result is cached for ``config_cache_ttl_seconds`` (0 =
        always re-fetch) so that searches do not pay a ``/config`` round-trip
        each time.  When a refresh fails but a stale cache exists, the stale
        cache is served (with a warning) — a transient ``/config`` outage
        must not take the provider down.  Without any cache, failures
        propagate as :class:`SearchProviderUnavailableError` /
        :class:`SearchProviderTimeoutError`.
        """
        ttl = self._config.config_cache_ttl_seconds
        if self._instance is not None and (time.monotonic() - self._instance_fetched_at) < max(
            ttl, 0
        ):
            return self._instance

        async with self._discovery_lock:
            # Re-check under the lock: a concurrent call may have refreshed.
            if self._instance is not None and (time.monotonic() - self._instance_fetched_at) < max(
                ttl, 0
            ):
                return self._instance
            try:
                self._instance = await self._fetch_instance()
                self._instance_fetched_at = time.monotonic()
            except (SearchProviderUnavailableError, SearchProviderTimeoutError) as exc:
                if self._instance is not None:
                    logger.warning(
                        "SearXNG /config refresh failed (%s) — using cached config from %.0fs ago",
                        exc.message,
                        time.monotonic() - self._instance_fetched_at,
                    )
                    return self._instance
                raise
            return self._instance

    # ------------------------------------------------------------------ capabilities
    async def capabilities(self) -> SearchCapabilities:
        """Report what this SearXNG instance can do right now.

        Categories come from the discovered enabled engines.  When the
        instance cannot be reached, the full static vertical mapping is
        advertised with ``available=False`` so callers can still see the
        provider's intended scope while knowing it is down.
        """
        time_ranges = list(TimeRange)
        supports_language = True
        supports_safe_search = True
        supports_paging = True
        supports_source_selection = True

        try:
            instance = await self._get_instance()
        except (SearchProviderUnavailableError, SearchProviderTimeoutError):
            return SearchCapabilities(
                verticals=list(VERTICAL_TO_CATEGORY),
                time_ranges=time_ranges,
                supports_language=supports_language,
                supports_safe_search=supports_safe_search,
                supports_paging=supports_paging,
                supports_source_selection=supports_source_selection,
                available=False,
            )

        enabled_categories = instance.enabled_categories
        verticals = [
            vertical
            for vertical in VERTICAL_TO_CATEGORY
            if VERTICAL_TO_CATEGORY[vertical] in enabled_categories
        ] or [Vertical.WEB]

        # Time-range filtering only applies per-engine; advertise the ranges
        # when at least one enabled engine supports them.
        if not any(engine.time_range_support for engine in instance.engines if engine.enabled):
            time_ranges = []
            supports_paging = False

        return SearchCapabilities(
            verticals=verticals,
            time_ranges=time_ranges,
            supports_language=supports_language,
            supports_safe_search=supports_safe_search,
            supports_paging=supports_paging,
            supports_source_selection=supports_source_selection,
            available=True,
        )

    # ------------------------------------------------------------------ search
    async def search(self, request: SearchQuery) -> SearchResponse:
        """Execute ``request`` against the SearXNG instance and normalize the results."""
        start = time.monotonic()

        # Discovery first: a missing/unreachable instance is a provider
        # failure, never a silently empty result set.
        instance = await self._get_instance()

        params: dict[str, Any] = {
            "q": request.query,
            "format": "json",
            "pageno": request.page,
            "safesearch": SAFE_SEARCH_LEVELS[request.safe_search],
        }

        degraded: list[str] = []
        warnings: list[str] = []

        # ---- verticals -> categories
        searched_verticals: list[Vertical] = []
        categories: list[str] = []
        for vertical in request.verticals:
            category = VERTICAL_TO_CATEGORY.get(vertical, "general")
            if category in instance.enabled_categories:
                searched_verticals.append(vertical)
                if category not in categories:
                    categories.append(category)
            else:
                degraded.append(f"vertical:{vertical.value}")
                warnings.append(
                    f"vertical '{vertical.value}' skipped: the SearXNG instance has no "
                    f"enabled engines for category '{category}'"
                )
        if categories:
            params["categories"] = ",".join(categories)
        else:
            # Nothing mappable: SearXNG falls back to its default category
            # (general/web). Keep the degradation warnings above.
            searched_verticals = [Vertical.WEB]

        # ---- sources -> engines (validated against /config)
        if request.sources:
            engine_names: list[str] = []
            unknown_sources: list[str] = []
            shortcuts = {
                engine.shortcut.lower(): engine.name
                for engine in instance.engines
                if engine.enabled and engine.shortcut
            }
            for source in request.sources:
                key = source.lower()
                if key in instance.enabled_engines:
                    engine_names.append(instance.enabled_engines[key].name)
                elif key in shortcuts:
                    engine_names.append(shortcuts[key])
                else:
                    unknown_sources.append(source)
            if unknown_sources:
                degraded.append("sources")
                warnings.append("unknown SearXNG engine(s) ignored: " + ", ".join(unknown_sources))
            if engine_names:
                params["engines"] = ",".join(dict.fromkeys(engine_names))

        # ---- language (request, else configured default)
        language = request.language or self._config.default_language
        if language:
            params["language"] = language

        # ---- time range
        if request.time_range is not None:
            params["time_range"] = TIME_RANGE_PARAMS[request.time_range]
            # Surface the limitation when none of the engines covering the
            # searched categories can actually apply a time filter.
            searched_categories = set(categories) if categories else {"general"}
            if not any(
                engine.enabled
                and engine.time_range_support
                and set(engine.categories) & searched_categories
                for engine in instance.engines
            ):
                degraded.append("time_range")
                warnings.append(
                    "time range requested but no enabled SearXNG engine covering the "
                    "searched verticals supports time-range filtering"
                )

        # ---- HTTP request
        try:
            response = await self._client.get("/search", params=params)
        except _HTTPX_TIMEOUT_EXCEPTIONS as exc:
            raise SearchProviderTimeoutError(
                f"SearXNG /search request timed out after {self._config.timeout_seconds}s",
                details={
                    "base_url": self._config.base_url,
                    "timeout_seconds": self._config.timeout_seconds,
                },
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchProviderUnavailableError(
                f"SearXNG instance at {self._config.base_url} is unreachable: {exc}",
                details={"base_url": self._config.base_url},
            ) from exc

        if response.status_code >= 400:
            raise SearchProviderUnavailableError(
                f"SearXNG /search returned HTTP {response.status_code}",
                details={
                    "base_url": self._config.base_url,
                    "status_code": response.status_code,
                    "body_preview": response.text[:300],
                },
            )

        payload = _parse_json_payload(response, "/search")

        # ---- normalize results
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise InvalidProviderResponseError(
                "SearXNG /search response is missing the 'results' list",
                details={"body_preview": response.text[:300]},
            )

        results: list[SearchResult] = []
        skipped = 0
        for rank, raw in enumerate(raw_results, start=1):
            result = _normalize_result(raw, rank, self.name)
            if result is None:
                skipped += 1
            else:
                results.append(result)
        if skipped:
            warnings.append(f"dropped {skipped} malformed SearXNG result(s)")
        if not results and raw_results:
            raise InvalidProviderResponseError(
                "SearXNG /search returned results, but none could be normalized",
                details={"raw_count": len(raw_results)},
            )

        # ---- limit (SearXNG page size is server-controlled; truncate client-side)
        if request.limit is not None and len(results) > request.limit:
            results = results[: request.limit]
            degraded.append("limit")
            warnings.append(
                f"truncated results to {request.limit} (SearXNG page size is set on the instance)"
            )

        # ---- informational bits from the payload
        unresponsive = payload.get("unresponsive_engines")
        if isinstance(unresponsive, list):
            names = [str(entry[0]) for entry in unresponsive if isinstance(entry, list) and entry]
            if names:
                warnings.append("unresponsive SearXNG engines: " + ", ".join(names))

        total_results = payload.get("number_of_results")
        if isinstance(total_results, (int, float)) and not isinstance(total_results, bool):
            total_results = int(total_results)
        else:
            total_results = None

        elapsed_ms = int((time.monotonic() - start) * 1000)

        return SearchResponse(
            query=request.query,
            verticals=searched_verticals,
            results=results,
            metadata=SearchMetadata(
                provider=self.name,
                providers_attempted=[self.name],
                elapsed_ms=elapsed_ms,
                warnings=warnings,
                degraded_capabilities=degraded,
                page=request.page,
                total_pages=None,
                total_results=total_results,
            ),
        )


# ================================================================== helpers
def _parse_json_payload(response: httpx.Response, endpoint: str) -> dict[str, Any]:
    """Parse a JSON object payload, raising a domain error on failure."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise InvalidProviderResponseError(
            f"SearXNG {endpoint} returned invalid JSON",
            details={
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type"),
                "body_preview": response.text[:300],
            },
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidProviderResponseError(
            f"SearXNG {endpoint} returned a non-object JSON payload",
            details={"content_type": response.headers.get("content-type")},
        )
    return payload


def _normalize_result(raw: Any, rank: int, provider: str) -> SearchResult | None:
    """Map one SearXNG result dict to a domain result.

    Returns ``None`` for entries that cannot be normalized (missing/invalid
    URL or title) instead of raising, so a single bad entry does not poison
    the whole page.
    """
    if not isinstance(raw, dict):
        return None

    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith(_HTTP_URL_PREFIXES):
        return None

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None

    snippet = raw.get("content")
    if not isinstance(snippet, str) or not snippet:
        snippet = None

    source = raw.get("engine")
    if not isinstance(source, str) or not source:
        engines = raw.get("engines")
        if isinstance(engines, list) and engines and isinstance(engines[0], str):
            source = engines[0]
    if source is not None:
        source = source[:100] or None

    thumbnail = raw.get("img_src") or raw.get("thumbnail")
    if thumbnail is not None:
        if not isinstance(thumbnail, str) or not thumbnail.startswith(_HTTP_URL_PREFIXES):
            thumbnail = None

    return SearchResult(
        title=title,
        url=url,
        snippet=snippet,
        rank=rank,
        vertical=category_to_vertical(raw.get("category")),
        source=source,
        provider=provider,
        thumbnail_url=thumbnail,
        published_at=_parse_published_date(raw.get("publishedDate")),
        # NOTE: SearXNG's per-engine 'score' is deliberately not mapped —
        # it is not a cross-provider comparable relevance value.
    )


def _parse_published_date(value: Any) -> datetime | None:
    """Parse SearXNG's ``publishedDate`` (ISO 8601 string or datetime)."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
