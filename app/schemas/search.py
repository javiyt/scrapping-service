"""Public request and response schemas for the search endpoints.

These schemas are the **service-owned** contract of ``POST /v1/search`` and
``GET /v1/search/capabilities``.  They are provider-agnostic by design:

- No SearXNG-specific parameter names appear (no ``pageno``, no numeric
  ``safesearch``, no comma-encoded categories, no raw provider request or
  response objects).
- ``SearchRequest`` is the public face of the internal
  :class:`app.search.models.SearchQuery` — same semantics, service-owned
  shape.  ``to_domain()`` maps onto the domain type that the
  :class:`app.search.cache.SearchCache` (and thus the router and the
  providers) consume.
- ``SearchApiResponse`` normalizes the domain
  :class:`app.search.models.SearchResponse` into the stable public shape
  (results + pagination + metadata); raw provider payloads never appear.
- ``SearchCapabilitiesResponse`` aggregates service-level capabilities
  (enabled providers, their normalized verticals/filters, discoverable
  sources, default routing behaviour, cache policy) — it never proxies a
  provider's own ``/config``-style discovery endpoint.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.search.models import (
    SafeSearch,
    SearchCapabilities,
    SearchQuery,
    SearchResponse,
    TimeRange,
    Vertical,
)
from app.search.router import SearchRouter


# ================================================================== request
class SearchRequest(BaseModel):
    """Request body for ``POST /v1/search``.

    All fields are service-level concepts; everything except ``query`` is
    optional.  Unknown fields are rejected so client typos (and any
    provider-specific parameter names) fail fast instead of leaking into
    provider requests.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="The search query (1-1000 characters, must contain text).",
    )
    verticals: list[Vertical] = Field(
        default_factory=lambda: [Vertical.WEB],
        description=(
            "Verticals to search (additive; each result is tagged with its "
            'vertical). Defaults to ``["web"]``. Known values: '
            + ", ".join(f'"{v.value}"' for v in Vertical)
            + "."
        ),
    )
    language: str | None = Field(
        default=None,
        pattern=r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$",
        description="Optional BCP-47 style language tag, e.g. ``en`` or ``en-US``.",
    )
    time_range: TimeRange | None = Field(
        default=None,
        description="Restrict results to this time range: day, week, month or year.",
    )
    safe_search: SafeSearch = Field(
        default=SafeSearch.MODERATE,
        description="Safe-search filtering level: off, moderate or strict.",
    )
    page: int = Field(
        default=1,
        ge=1,
        le=100,
        description="Result page number (1-based).",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Maximum number of results to return (1-100).",
    )
    sources: list[str] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "Optional preferred underlying sources (1-100 characters each, "
            "at most 50). Semantics are provider-defined; see the "
            "capabilities endpoint for discoverable sources."
        ),
    )
    provider: str = Field(
        default="auto",
        min_length=1,
        max_length=100,
        description=(
            "Provider name to use, or ``auto`` (the default) to let the "
            "service pick an available provider."
        ),
    )
    cache_ttl_seconds: int | None = Field(
        default=None,
        ge=60,
        le=2592000,
        description="Per-request cache TTL in seconds (60 s - 30 days).",
    )
    force_refresh: bool = Field(
        default=False,
        description="If ``true``, bypass the cache and query the provider fresh.",
    )

    @field_validator("query")
    @classmethod
    def _validate_query(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be whitespace-only")
        return v

    @model_validator(mode="after")
    def _check_verticals(self) -> "SearchRequest":
        if not self.verticals:
            raise ValueError("At least one vertical is required")
        if len(set(self.verticals)) != len(self.verticals):
            raise ValueError("verticals must not contain duplicates")
        return self

    @model_validator(mode="after")
    def _check_sources(self) -> "SearchRequest":
        for source in self.sources:
            if not source or len(source) > 100:
                raise ValueError("Each source must be 1-100 characters")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources must not contain duplicates")
        return self

    def to_domain(self) -> SearchQuery:
        """Map onto the internal, provider-agnostic :class:`SearchQuery`."""
        return SearchQuery(
            query=self.query,
            verticals=list(self.verticals),
            language=self.language,
            time_range=self.time_range,
            safe_search=self.safe_search,
            page=self.page,
            limit=self.limit,
            sources=list(self.sources),
            provider=self.provider.strip(),
            cache_ttl_seconds=self.cache_ttl_seconds,
            force_refresh=self.force_refresh,
        )


# ================================================================ responses
class SearchApiResult(BaseModel):
    """One normalized search result in the public response shape."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., description="Result title.")
    url: str = Field(..., description="Absolute http(s) URL of the result.")
    snippet: str | None = Field(
        default=None,
        description="Short summary or description, when the provider offers one.",
    )
    rank: int = Field(..., ge=1, description="1-based rank within its vertical.")
    vertical: str = Field(
        ...,
        description="Vertical this result belongs to (normalized vocabulary).",
    )
    source: str | None = Field(
        default=None,
        description="Underlying source (e.g. a specific search engine), when known.",
    )
    provider: str = Field(..., description="Provider that produced this result.")
    thumbnail_url: str | None = Field(
        default=None,
        description="Optional thumbnail image URL.",
    )
    published_at: datetime | None = Field(
        default=None,
        description="Optional publication timestamp, when known.",
    )


class SearchPagination(BaseModel):
    """Pagination information for a search response."""

    model_config = ConfigDict(extra="forbid")

    page: int | None = Field(
        default=None,
        ge=1,
        description="Current result page, when the provider paginates.",
    )
    has_more: bool | None = Field(
        default=None,
        description=(
            "Whether further pages exist. ``null`` when the provider does not "
            "report a total page count."
        ),
    )
    total_pages: int | None = Field(
        default=None,
        ge=1,
        description="Total number of pages, when the provider reports it.",
    )
    total_results: int | None = Field(
        default=None,
        ge=0,
        description="Estimated total number of matching results, if reported.",
    )


class SearchApiMetadata(BaseModel):
    """Metadata attached to every search response."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(..., description="Provider that produced this response.")
    providers_attempted: list[str] = Field(
        default_factory=list,
        description="Providers attempted in order, including failed ones.",
    )
    fallback_occurred: bool = Field(
        default=False,
        description="True when the router fell back after a transient failure.",
    )
    from_cache: bool = Field(
        default=False,
        description="True when the response was served from the search cache.",
    )
    stale: bool = Field(
        default=False,
        description="True when a stale (expired) cache entry was served on error.",
    )
    cached_at: datetime | None = Field(
        default=None,
        description=(
            "When the cached entry serving this response was created, when "
            "``from_cache`` is ``true``. ``null`` for fresh responses."
        ),
    )
    cache_age_seconds: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Age of the served entry in seconds when ``from_cache`` is "
            "``true``. ``null`` for fresh responses."
        ),
    )
    elapsed_ms: int = Field(default=0, ge=0, description="Time spent serving, in ms.")
    warnings: list[str] = Field(
        default_factory=list,
        description="Human-readable warnings (degradations, fallbacks, ...).",
    )
    degraded_capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "Machine-readable tokens for requested features the provider could "
            "not fully honor (e.g. ``time_range``, ``sources``, ``limit``)."
        ),
    )
    auth_profile: str | None = Field(
        default=None,
        description=(
            "Authenticated profile name, present only when profile exposure "
            "is enabled for this deployment."
        ),
    )


class SearchApiResponse(BaseModel):
    """Response envelope for ``POST /v1/search``."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., description="Echo of the requested query.")
    results: list[SearchApiResult] = Field(
        default_factory=list,
        description="Normalized results, ordered by rank within each vertical.",
    )
    pagination: SearchPagination = Field(..., description="Pagination information.")
    metadata: SearchApiMetadata = Field(..., description="Response metadata.")

    @classmethod
    def from_domain(cls, response: SearchResponse) -> "SearchApiResponse":
        """Convert a normalized domain response into the public shape."""
        meta = response.metadata
        has_more: bool | None = None
        if meta.page is not None and meta.total_pages is not None:
            has_more = meta.page < meta.total_pages
        return cls(
            query=response.query,
            results=[
                SearchApiResult(
                    title=result.title,
                    url=result.url,
                    snippet=result.snippet,
                    rank=result.rank,
                    vertical=result.vertical.value,
                    source=result.source,
                    provider=result.provider,
                    thumbnail_url=result.thumbnail_url,
                    published_at=result.published_at,
                )
                for result in response.results
            ],
            pagination=SearchPagination(
                page=meta.page,
                has_more=has_more,
                total_pages=meta.total_pages,
                total_results=meta.total_results,
            ),
            metadata=SearchApiMetadata(
                provider=meta.provider,
                providers_attempted=meta.providers_attempted,
                fallback_occurred=meta.fallback_occurred,
                from_cache=meta.from_cache,
                stale=meta.stale,
                cached_at=meta.cached_at,
                cache_age_seconds=meta.cache_age_seconds,
                elapsed_ms=meta.elapsed_ms,
                warnings=meta.warnings,
                degraded_capabilities=meta.degraded_capabilities,
            ),
        )


# ============================================================== capabilities
class SearchFilters(BaseModel):
    """Boolean flags for the normalized filters a provider (or the service
    union) supports."""

    model_config = ConfigDict(extra="forbid")

    language: bool = Field(
        default=False,
        description="Filter results by language tag.",
    )
    safe_search: bool = Field(
        default=False,
        description="Apply safe-search filtering.",
    )
    time_range: bool = Field(
        default=False,
        description="Restrict results to a time range.",
    )
    paging: bool = Field(
        default=False,
        description="Serve arbitrary result pages.",
    )
    source_selection: bool = Field(
        default=False,
        description="Restrict results to specific sources.",
    )


class SearchProviderCapabilities(BaseModel):
    """What one registered provider can do right now."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Stable provider identifier.")
    available: bool = Field(
        ...,
        description=(
            "Provider health. ``false`` when the provider is down or its capabilities probe failed."
        ),
    )
    verticals: list[str] = Field(
        default_factory=list,
        description="Normalized verticals this provider can search (empty when unknown).",
    )
    time_ranges: list[str] = Field(
        default_factory=list,
        description="Time-range filters this provider supports (empty when none/unknown).",
    )
    filters: SearchFilters = Field(
        default_factory=SearchFilters,
        description="Filter flags. All ``false``/absent values when the probe failed.",
    )
    max_results: int | None = Field(
        default=None,
        ge=1,
        description="Hard cap on results per request, when the provider has one.",
    )
    sources: list[str] | None = Field(
        default=None,
        description=(
            "Selectable source names, when the provider can discover them "
            "(null = not discoverable)."
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Human-readable notes, e.g. a failed capabilities probe.",
    )


class SearchCacheCapabilities(BaseModel):
    """Service-level search result cache policy."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(..., description="Whether the search result cache is on.")
    default_ttl_seconds: int = Field(
        ...,
        ge=0,
        description="TTL for cached entries when the request sets none (0 = never expires).",
    )
    stale_if_error: bool = Field(
        ...,
        description="Whether expired entries are served when the live search fails.",
    )


class SearchCapabilitiesResponse(BaseModel):
    """Response envelope for ``GET /v1/search/capabilities``."""

    model_config = ConfigDict(extra="forbid")

    default_provider: str = Field(
        ...,
        description=(
            "Routing behaviour for ``provider: auto`` requests: ``auto`` "
            "(capability-aware routing with fallback) or an explicit provider name."
        ),
    )
    provider_order: list[str] = Field(
        default_factory=list,
        description="Routing priority for auto mode (registered providers).",
    )
    providers: list[SearchProviderCapabilities] = Field(
        default_factory=list,
        description="Enabled providers, in routing priority order.",
    )
    verticals: list[str] = Field(
        default_factory=list,
        description=(
            "Normalized verticals usable with ``provider: auto`` — the union "
            "across available providers."
        ),
    )
    time_ranges: list[str] = Field(
        default_factory=list,
        description=(
            "Time-range filters usable with ``provider: auto`` (union across available providers)."
        ),
    )
    filters: SearchFilters = Field(
        default_factory=SearchFilters,
        description="Filters usable with ``provider: auto`` (any available provider).",
    )
    safe_search_levels: list[str] = Field(
        default_factory=list,
        description="Normalized safe-search levels accepted by the request.",
    )
    cache: SearchCacheCapabilities = Field(
        ...,
        description="Service-level search result cache policy.",
    )

    @classmethod
    def from_router(
        cls,
        router: SearchRouter,
        probes: list[tuple[str, SearchCapabilities | None]],
        cache_enabled: bool,
        cache_default_ttl_seconds: int,
        cache_stale_if_error: bool,
    ) -> "SearchCapabilitiesResponse":
        """Aggregate per-provider probes into the service-level report.

        ``probes`` must follow :meth:`SearchRouter.ordered_candidates` order;
        a ``None`` entry marks a provider whose capabilities probe failed.
        Unions (service-level verticals/time ranges/filters) only include
        providers currently ``available``.
        """
        providers: list[SearchProviderCapabilities] = []
        union_verticals: list[str] = []
        union_time_ranges: list[str] = []
        filters = SearchFilters()
        for name, caps in probes:
            if caps is None:
                providers.append(
                    SearchProviderCapabilities(
                        name=name,
                        available=False,
                        warnings=["capabilities probe failed (provider unhealthy)"],
                    )
                )
                continue
            entry = SearchProviderCapabilities(
                name=name,
                available=caps.available,
                verticals=[v.value for v in caps.verticals],
                time_ranges=[t.value for t in caps.time_ranges],
                filters=SearchFilters(
                    language=caps.supports_language,
                    safe_search=caps.supports_safe_search,
                    time_range=bool(caps.time_ranges),
                    paging=caps.supports_paging,
                    source_selection=caps.supports_source_selection,
                ),
                max_results=caps.max_results,
                sources=caps.sources,
            )
            providers.append(entry)
            if not caps.available:
                continue
            for vertical in entry.verticals:
                if vertical not in union_verticals:
                    union_verticals.append(vertical)
            for time_range in entry.time_ranges:
                if time_range not in union_time_ranges:
                    union_time_ranges.append(time_range)
            filters.language = filters.language or entry.filters.language
            filters.safe_search = filters.safe_search or entry.filters.safe_search
            filters.time_range = filters.time_range or entry.filters.time_range
            filters.paging = filters.paging or entry.filters.paging
            filters.source_selection = filters.source_selection or entry.filters.source_selection

        return cls(
            default_provider=router.config.default_provider,
            provider_order=list(router.config.provider_order),
            providers=providers,
            verticals=union_verticals,
            time_ranges=union_time_ranges,
            filters=filters,
            safe_search_levels=[level.value for level in SafeSearch],
            cache=SearchCacheCapabilities(
                enabled=cache_enabled,
                default_ttl_seconds=cache_default_ttl_seconds,
                stale_if_error=cache_stale_if_error,
            ),
        )
