"""Provider-agnostic search domain models.

This module defines the stable, provider-agnostic search contract owned by
the service:

- :class:`SearchQuery` — normalized search request.
- :class:`SearchResult` — a single normalized search result.
- :class:`SearchMetadata` / :class:`SearchResponse` — normalized response
  envelope with response metadata.
- :class:`SearchCapabilities` — what a provider can do, used for explicit
  capability negotiation.

Design principles:

- The service owns the public search contract.
- Providers adapt to this contract; clients never adapt to providers.
- Raw provider request/response objects are never exposed.
- Unsupported features are represented explicitly through capabilities.
- Search is separate from scraping/fetching (see ``app.scraper``).

All models reject unknown fields so raw provider payloads can never leak
into the domain.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ===================================================================== Enums
class Vertical(StrEnum):
    """Normalized search vertical vocabulary.

    This is the minimum vocabulary every provider is expected to understand
    semantically.  A provider does **not** have to support every vertical;
    it must advertise what it supports via :class:`SearchCapabilities`.
    """

    WEB = "web"
    NEWS = "news"
    IMAGES = "images"
    VIDEOS = "videos"
    SOCIAL = "social"
    IT = "it"
    SCIENCE = "science"
    MAPS = "maps"
    MUSIC = "music"
    FILES = "files"


class TimeRange(StrEnum):
    """Time-range filter values."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class SafeSearch(StrEnum):
    """Safe-search levels."""

    OFF = "off"
    MODERATE = "moderate"
    STRICT = "strict"


# ================================================================== SearchQuery
class SearchQuery(BaseModel):
    """Normalized, provider-agnostic search request.

    This is the internal canonical form of a search request.  HTTP layers
    and future client APIs map onto this type; providers adapt from it.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="The search query string.",
    )
    verticals: list[Vertical] = Field(
        default_factory=lambda: [Vertical.WEB],
        description=(
            "Verticals to search. Defaults to ``web``. Verticals are additive: "
            "results from each vertical are returned together, each tagged "
            "with its own vertical."
        ),
    )
    language: str | None = Field(
        default=None,
        pattern=r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*$",
        description=(
            "Optional BCP-47 style language tag, e.g. ``en``, ``en-US``, "
            "``pt-BR``. ``None`` lets the provider use its default."
        ),
    )
    time_range: TimeRange | None = Field(
        default=None,
        description="Restrict results to this time range. ``None`` = no restriction.",
    )
    safe_search: SafeSearch = Field(
        default=SafeSearch.MODERATE,
        description="Safe-search filtering level.",
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
        description=(
            "Maximum number of results to return. ``None`` lets the provider "
            "use its default page size. Providers with a lower cap should "
            "advertise it via ``SearchCapabilities.max_results``."
        ),
    )
    sources: list[str] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "Optional preferred underlying sources (e.g. a specific search "
            "engine name). Semantics are provider-defined; providers without "
            "source selection ignore this and should say so in capabilities."
        ),
    )
    provider: str = Field(
        default="auto",
        min_length=1,
        max_length=100,
        description=(
            "Provider name to use, or ``auto`` to let the service pick an available provider."
        ),
    )
    cache_ttl_seconds: int | None = Field(
        default=None,
        ge=60,
        le=2592000,
        description="Per-request cache TTL in seconds (60 s – 30 d).",
    )
    force_refresh: bool = Field(
        default=False,
        description="If ``True``, bypass the cache and query the provider fresh.",
    )

    @model_validator(mode="after")
    def _check_verticals(self) -> "SearchQuery":
        if not self.verticals:
            raise ValueError("At least one vertical is required")
        if len(set(self.verticals)) != len(self.verticals):
            raise ValueError("verticals must not contain duplicates")
        return self

    @model_validator(mode="after")
    def _check_sources(self) -> "SearchQuery":
        for source in self.sources:
            if not source or len(source) > 100:
                raise ValueError("Each source must be 1-100 characters")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("sources must not contain duplicates")
        return self


# ================================================================= SearchResult
class SearchResult(BaseModel):
    """A single normalized search result.

    Contains only provider-agnostic fields. Raw provider result dictionaries
    (with provider-specific keys) must never be stored here — adapters must
    map their payload into these fields.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, description="Result title.")
    url: str = Field(
        ...,
        min_length=1,
        pattern=r"^https?://\S+$",
        description="Absolute http(s) URL of the result.",
    )
    snippet: str | None = Field(
        default=None,
        description="Short summary or description, when the provider offers one.",
    )
    rank: int = Field(
        default=1,
        ge=1,
        description="1-based rank of this result within its vertical.",
    )
    vertical: Vertical = Field(
        default=Vertical.WEB,
        description="Vertical this result belongs to.",
    )
    source: str | None = Field(
        default=None,
        max_length=100,
        description=("Name of the underlying source (e.g. a specific search engine), when known."),
    )
    provider: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Name of the search provider that produced this result.",
    )
    thumbnail_url: str | None = Field(
        default=None,
        pattern=r"^https?://\S+$",
        description="Optional thumbnail image URL.",
    )
    published_at: datetime | None = Field(
        default=None,
        description="Optional publication timestamp, when known.",
    )
    score: float | None = Field(
        default=None,
        description=(
            "Optional relevance score. The scale and semantics are "
            "provider-defined; scores must never be compared across "
            "providers or assumed to be normalized."
        ),
    )


# =============================================================== SearchMetadata
class SearchMetadata(BaseModel):
    """Metadata attached to every search response."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Name of the provider that produced this response.",
    )
    providers_attempted: list[str] = Field(
        default_factory=list,
        description=(
            "Providers attempted in order, including failed ones. "
            "Standalone providers list only themselves."
        ),
    )
    elapsed_ms: int = Field(
        default=0,
        ge=0,
        description="Time spent serving this response, in milliseconds.",
    )
    from_cache: bool = Field(
        default=False,
        description="``True`` when the response was served from cache.",
    )
    stale: bool = Field(
        default=False,
        description=(
            "``True`` when the cached response is stale (served while a "
            "background refresh is running)."
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Human-readable warnings, e.g. degraded capabilities.",
    )
    degraded_capabilities: list[str] = Field(
        default_factory=list,
        description=(
            "Machine-readable tokens for requested features the provider "
            "could not fully honor, e.g. ``vertical:news``, ``time_range``, "
            "``language``, ``safe_search``, ``paging``, ``sources``, "
            "``limit``."
        ),
    )
    page: int | None = Field(
        default=None,
        ge=1,
        description="Current result page, when the provider paginates.",
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


# =============================================================== SearchResponse
class SearchResponse(BaseModel):
    """Normalized search response envelope."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        description="Echo of the requested query.",
    )
    verticals: list[Vertical] = Field(
        default_factory=list,
        description="Verticals actually searched (after any provider clamping).",
    )
    results: list[SearchResult] = Field(
        default_factory=list,
        description="Normalized results, ordered by rank within each vertical.",
    )
    metadata: SearchMetadata = Field(
        ...,
        description="Response metadata (provider, timing, cache, pagination).",
    )


# =========================================================== SearchCapabilities
class SearchCapabilities(BaseModel):
    """What a search provider can do.

    Capabilities are the mechanism for explicit negotiation: the service
    (and clients) can see what a provider supports instead of discovering
    gaps through errors or silently dropped request fields.
    """

    model_config = ConfigDict(extra="forbid")

    verticals: list[Vertical] = Field(
        ...,
        description="Verticals this provider can search.",
    )
    time_ranges: list[TimeRange] = Field(
        default_factory=list,
        description="Time-range filters this provider supports (empty = none).",
    )
    supports_language: bool = Field(
        default=False,
        description="``True`` when the provider can filter by language.",
    )
    supports_safe_search: bool = Field(
        default=False,
        description="``True`` when the provider can apply safe-search filtering.",
    )
    supports_paging: bool = Field(
        default=False,
        description="``True`` when the provider can serve arbitrary result pages.",
    )
    supports_source_selection: bool = Field(
        default=False,
        description="``True`` when the provider can restrict results to specific sources.",
    )
    max_results: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Hard cap on results per request, when the provider has one. "
            "``None`` = no documented cap."
        ),
    )
    available: bool = Field(
        default=True,
        description=(
            "Provider availability/health. ``False`` when the provider is "
            "down or unhealthy and cannot serve searches right now."
        ),
    )

    @model_validator(mode="after")
    def _check_verticals(self) -> "SearchCapabilities":
        if not self.verticals:
            raise ValueError("At least one supported vertical is required")
        if len(set(self.verticals)) != len(self.verticals):
            raise ValueError("Supported verticals must not contain duplicates")
        return self

    def degradations(self, request: SearchQuery) -> list[str]:
        """Return capability tokens this provider cannot fully honor.

        Returns machine-readable tokens that can be placed directly into
        :attr:`SearchMetadata.degraded_capabilities`, e.g.
        ``["vertical:news", "time_range", "limit"]``.  An empty list means
        the provider can honor the full request.
        """
        degraded: list[str] = []
        for vertical in request.verticals:
            if vertical not in self.verticals:
                degraded.append(f"vertical:{vertical.value}")
        if request.time_range is not None and request.time_range not in self.time_ranges:
            degraded.append("time_range")
        if request.language is not None and not self.supports_language:
            degraded.append("language")
        if request.safe_search is not SafeSearch.OFF and not self.supports_safe_search:
            degraded.append("safe_search")
        if request.page > 1 and not self.supports_paging:
            degraded.append("paging")
        if request.sources and not self.supports_source_selection:
            degraded.append("sources")
        if (
            request.limit is not None
            and self.max_results is not None
            and request.limit > self.max_results
        ):
            degraded.append("limit")
        return degraded
