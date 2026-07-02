"""Request and response schemas for scraping endpoints."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ========================================================= ExtractField
class ExtractField(BaseModel):
    """Configuration for a single extracted field.

    Supports four field types:

    * ``text`` — inner text content (stripped).
    * ``html`` — inner HTML markup (preserves tag structure).
    * ``attr`` — value of an HTML attribute on the matched element.
    * ``object`` — nested structured extraction via sub-``fields``.
    """

    selector: str = Field(..., description="CSS selector to locate the element(s).")
    type: str = Field(
        default="text",
        pattern=r"^(text|html|attr|object)$",
        description="Type of value to extract.",
    )
    attribute: str | None = Field(
        default=None,
        description="Attribute name to read (only used when ``type`` is ``attr``).",
    )
    multiple: bool = Field(
        default=False,
        description="When ``True``, return a list of all matching results.",
    )
    default: Any = Field(
        default=None,
        description="Fallback value when no element matches.",
    )
    required: bool = Field(
        default=False,
        description="When ``True``, a failed match produces a structured extraction error.",
    )
    absolute_url: bool = Field(
        default=False,
        description="Convert the extracted URL from relative to absolute using the"
        " page URL as base.",
    )
    fields: dict[str, "ExtractField"] | None = Field(
        default=None,
        description="Nested field definitions (only used when ``type`` is ``object``).",
    )


ExtractField.model_rebuild()


# ========================================================= ExtractConfig
class ExtractConfig(BaseModel):
    """Optional structured extraction applied at response time.

    Extraction runs *after* HTML normalization (if enabled), so it operates
    on the cleaned HTML.  When extraction is disabled (the default) the
    response does **not** include an ``extracted`` field.
    """

    enabled: bool = Field(
        default=False,
        description="Master toggle — must be ``True`` for any extraction to run.",
    )
    base_url: str | None = Field(
        default=None,
        description="Override base URL for relative-to-absolute URL conversion."
        " Falls back to ``final_url``, then the request ``url``.",
    )
    fields: dict[str, ExtractField] = Field(
        default_factory=dict,
        description="Map of field names to extraction field configurations.",
    )


# ========================================================== NormalizeConfig
class NormalizeConfig(BaseModel):
    """Optional HTML normalisation applied at response time.

    All fields default to ``False`` — normalization must be explicitly
    enabled per request.  The raw HTML stored in cache is never modified;
    normalization is applied on-the-fly when building the response.
    """

    enabled: bool = Field(
        default=False,
        description="Master toggle — must be true for any normalisation to run.",
    )
    absolute_urls: bool = Field(
        default=False,
        description="Convert relative ``href``, ``src``, ``action``, ``poster`` "
        "and ``srcset`` values to absolute URLs.",
    )
    remove_scripts: bool = Field(
        default=False,
        description="Remove all ``<script>`` elements.",
    )
    remove_styles: bool = Field(
        default=False,
        description="Remove ``<style>`` elements and inline ``style`` attributes.",
    )
    remove_comments: bool = Field(
        default=False,
        description="Remove HTML comments.",
    )
    remove_meta: bool = Field(
        default=False,
        description="Remove all ``<meta>`` elements.",
    )
    remove_noscript: bool = Field(
        default=False,
        description="Remove all ``<noscript>`` elements.",
    )
    collapse_whitespace: bool = Field(
        default=False,
        description="Reduce runs of whitespace to single spaces in text nodes.",
    )
    minify: bool = Field(
        default=False,
        description="Compact HTML output without breaking semantics.",
    )


# ====================================================================== Proxy
class ProxyConfig(BaseModel):
    """Optional proxy configuration for a scrape request.

    When left at defaults, the global proxy settings from the config file
    are used.  Using a per-request proxy ``url`` requires the admin to have
    set ``proxy.allow_request_override: true`` in the server configuration.
    """

    enabled: bool = Field(
        default=False,
        description="Master toggle for this proxy configuration.",
    )
    url: str | None = Field(
        default=None,
        description=(
            "Proxy URL, e.g. http://user:pass@proxy.example:8080. "
            "Accepted schemes: http, https, socks5."
        ),
    )
    country: str | None = Field(
        default=None,
        description="Optional country hint for the proxy provider (e.g. ``ES``).",
    )


# =================================================================== Scroll
class ScrollConfig(BaseModel):
    """Scrolling behaviour for JavaScript-rendered pages."""

    enabled: bool = False
    max_scrolls: int = Field(default=5, ge=1, le=100)
    delay_ms: int = Field(default=1000, ge=100, le=30000)
    stop_when_no_growth: bool = True


# =================================================================== Debug
class DebugConfig(BaseModel):
    """Debug output controls."""

    screenshot: bool = False
    html_dump: bool = False


# ============================================================= ScrapeRequest
class ScrapeRequest(BaseModel):
    """Request body for the ``/v1/scrape`` endpoint."""

    url: str = Field(..., min_length=1, max_length=8192, description="Target URL to scrape")

    mode: str = Field(
        default="auto",
        pattern=r"^(http|browser|auto)$",
        description="Scraping mode: http, browser, or auto",
    )

    cache_ttl_seconds: int | None = Field(
        default=None,
        ge=60,
        le=2592000,
        description="Per-request cache TTL (60 s – 30 d). Overrides domain and global defaults.",
    )

    force_refresh: bool = Field(
        default=False,
        description="If true, bypass cache and fetch fresh content.",
    )

    wait_until: str = Field(
        default="networkidle",
        pattern=r"^(load|domcontentloaded|networkidle|networkidle0)$",
        description="Browser wait strategy (only relevant in browser mode).",
    )

    wait_selector: str | None = Field(
        default=None,
        max_length=500,
        description="CSS selector to wait for before returning (browser mode only).",
    )

    timeout_seconds: int = Field(
        default=45,
        ge=5,
        le=120,
        description="Maximum time to wait for the page to load.",
    )

    proxy: ProxyConfig = Field(
        default_factory=ProxyConfig,
        description="Optional proxy configuration for this request.",
    )

    scroll: ScrollConfig = Field(default_factory=ScrollConfig)

    debug: DebugConfig = Field(default_factory=DebugConfig)

    normalize: NormalizeConfig = Field(
        default_factory=NormalizeConfig,
        description="Optional HTML normalisation applied at response time.",
    )

    extract: ExtractConfig = Field(
        default_factory=ExtractConfig,
        description="Optional structured extraction using CSS selectors.",
    )

    @model_validator(mode="after")
    def _check_timeout_vs_mode(self) -> "ScrapeRequest":
        if self.timeout_seconds > 60 and self.mode == "browser":
            raise ValueError(
                "Timeout > 60 s is not recommended for browser mode — risk of resource exhaustion"
            )
        return self


# ========================================================= BatchScrapeRequest
class BatchItem(BaseModel):
    """One item inside a batch scrape request."""

    url: str = Field(..., min_length=1, max_length=8192)
    mode: str = Field(default="auto", pattern=r"^(http|browser|auto)$")
    cache_ttl_seconds: int | None = Field(default=None, ge=60, le=2592000)
    force_refresh: bool = False
    wait_until: str = Field(
        default="networkidle",
        pattern=r"^(load|domcontentloaded|networkidle|networkidle0)$",
    )
    wait_selector: str | None = Field(default=None, max_length=500)
    timeout_seconds: int = Field(default=45, ge=5, le=120)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    scroll: ScrollConfig = Field(default_factory=ScrollConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    normalize: NormalizeConfig = Field(default_factory=NormalizeConfig)
    extract: ExtractConfig = Field(default_factory=ExtractConfig)


class BatchScrapeRequest(BaseModel):
    """Request body for the ``/v1/scrape/batch`` endpoint."""

    items: list[BatchItem] = Field(..., min_length=1, max_length=50)
    max_concurrency: int = Field(default=3, ge=1, le=10)


# ============================================================ ScrapeResponse
class Metadata(BaseModel):
    """Metadata attached to every scrape response."""

    mode: str
    elapsed_ms: int
    content_length: int
    cache_key: str
    normalized: bool = Field(
        default=False,
        description="True when HTML normalisation was applied to this response.",
    )
    normalization: dict[str, bool] | None = Field(
        default=None,
        description="Which normalisation features were applied, e.g. "
        '``{"remove_scripts": true, "absolute_urls": true}``.',
    )


class ScrapeResponse(BaseModel):
    """Successful scrape response."""

    url: str
    final_url: str
    status_code: int
    from_cache: bool = False
    stale: bool = False
    fetched_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    expires_at: str | None = None
    html: str
    metadata: Metadata
    extracted: dict[str, Any] | None = Field(
        default=None,
        description="Structured data extracted via CSS selectors. "
        "``None`` when extraction is disabled or when a required field fails.",
    )
    extraction_error: dict[str, Any] | None = Field(
        default=None,
        description="Structured error when a required extraction field cannot be resolved. "
        "``None`` when extraction succeeds or is disabled.",
    )


# ========================================================== CacheStats
class CacheStats(BaseModel):
    """Cache statistics."""

    total_entries: int
    total_size_bytes: int
    expired_entries: int
    cache_path: str


# ============================================================ BatchResponse
class BatchResult(BaseModel):
    """One result within a batch scrape response."""

    url: str
    success: bool
    result: ScrapeResponse | None = None
    error: dict[str, Any] | None = None


class BatchScrapeResponse(BaseModel):
    """Response for ``/v1/scrape/batch``."""

    results: list[BatchResult]
    total: int
    succeeded: int
    failed: int
    elapsed_ms: int


# ==================================================== CacheCleanupRequest
class CacheCleanupRequest(BaseModel):
    """Optional overrides for a manual cleanup request.

    Each field falls back to the global config default when ``None``.
    """

    delete_expired_after_seconds: int | None = None
    max_entries: int | None = None
    max_size_mb: int | None = None
    vacuum: bool | None = None


# ============================================================ PurgeResponse
class PurgeResponse(BaseModel):
    """Response after cache purge."""

    purged_entries: int
    message: str


# ================================================================== Health
class HealthResponse(BaseModel):
    """Liveness check response."""

    status: str = "ok"
    version: str = "1.0.0"
    service: str = "scraper-api"


class ReadinessResponse(BaseModel):
    """Readiness check response."""

    status: str = "ok"
    checks: dict[str, Any] = Field(default_factory=dict)
