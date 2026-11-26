"""4get implementation of the :class:`SearchProvider` contract.

This adapter translates normalized search requests
(:class:`app.search.models.SearchQuery`) into 4get's JSON search API and
normalizes 4get's JSON responses back into project-owned domain types.
Clients never see 4get's raw API contract (endpoint paths, the
``status: "ok"`` envelope, the ``npt`` page token, scraper names, the
``pass`` cookie):

- Verticals map to 4get endpoints: ``web`` → ``/api/v1/web``,
  ``news`` → ``/api/v1/news``, ``images`` → ``/api/v1/images``,
  ``videos`` → ``/api/v1/videos``, ``music`` → ``/api/v1/music``. One
  ``search()`` call performs exactly one 4get request (the first supported
  vertical in the request); other requested verticals are reported as
  degraded — 4get has no multi-vertical endpoint, and fanning out would
  multiply quota usage on instances that meter ~100 requests / 24h.
- ``/ami4get`` (instance info) is the health probe for
  :meth:`capabilities`; it is cached with ``health_cache_ttl_seconds`` and
  no search endpoint is ever used for health.
- Paging is **sequential-only by design**: 4get paginates with opaque,
  single-use ``npt`` tokens that expire after ~15 minutes. This provider
  serves page 1 only, never sends ``npt``, and never exposes a token to
  clients; ``supports_paging`` is therefore ``False`` (see
  :mod:`app.search.providers.fourget.mapping`).
- The 4get ``time`` field (video duration, seconds) is intentionally not
  mapped: the domain :class:`SearchResult` has no duration field, and
  adding one would change the public Search API (out of scope), mirroring
  how the SearXNG provider drops its per-engine ``score``.

Request mapping (normalized → 4get query parameters):

============================================  ==========================
Normalized                                    4get parameter
============================================  ==========================
``query``                                     ``s``
``sources`` (first) / configured scraper      ``scraper``
``language`` / configured default language    ``lang``
configured default region                     ``country``
``safe_search`` / configured NSFW default     ``nsfw`` (``"0"`` / ``"1"``)
— (never)                                     ``npt`` (sequential-only)
============================================  ==========================

Result normalization (4get result dict → :class:`SearchResult`):

====================================  ==========================
4get field                            Domain field
====================================  ==========================
``title`` (required, non-empty)       ``title``
``url`` (must be absolute http(s))    ``url``
``snippet``                           ``snippet`` (music: falls back to
                                      ``artist — album`` when absent)
position in provider ordering         ``rank``
the endpoint the search used          ``vertical``
``engine``                            ``source``
``image``                             ``thumbnail_url``
``date`` (epoch seconds)              ``published_at`` (UTC)
``time`` (duration seconds)           — intentionally not mapped
====================================  ==========================

Failures raise the search error types from :mod:`app.core.errors`
(unavailable, timeout, invalid provider response, no provider available) —
never broken results. The raised errors are public-safe: they carry no
instance URL, upstream exception text, response body preview, or pass
token (all of which are logged server-side instead), so they may be
rendered into HTTP responses as-is. In particular, HTTP 429 (missing or
invalid pass) maps to :class:`NoProviderAvailableError` — a clear,
non-retryable configuration error (the router never falls back on it).
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from typing import Any

import httpx

from app.core.errors import (
    InvalidProviderResponseError,
    NoProviderAvailableError,
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
)
from app.search.models import (
    SafeSearch,
    SearchCapabilities,
    SearchMetadata,
    SearchQuery,
    SearchResponse,
    SearchResult,
    Vertical,
)
from app.search.providers.fourget.config import FourgetConfig
from app.search.providers.fourget.mapping import (
    HEALTH_PATH,
    NAME,
    OK_STATUS,
    PASS_COOKIE_NAME,
    RESULT_KEY,
    SAFE_SEARCH_TO_NSFW,
    SUPPORTED_VERTICALS,
    VERTICAL_TO_ENDPOINT,
    endpoint_for_vertical,
    epoch_to_datetime,
)

logger = logging.getLogger("scraper-api.search.fourget")

# httpx exceptions we treat as definite provider timeouts (vs. other errors).
_HTTPX_TIMEOUT_EXCEPTIONS = (
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.TimeoutException,
)

_HTTP_URL_PREFIXES = ("http://", "https://")


class FourgetProvider:
    """4get backend for the provider-agnostic search contract.

    Satisfies the structural :class:`app.search.SearchProvider` protocol
    (``name`` + ``capabilities()`` + ``search()``) plus a ``close()``
    (releases an internally owned HTTP client) and a ``diagnostics()``
    (consumed by ``/health/deep`` like the other providers').
    """

    def __init__(
        self,
        config: FourgetConfig,
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
        self._available: bool | None = None
        self._available_at: float = 0.0
        self._health_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return NAME

    async def close(self) -> None:
        """Release the HTTP client (only closes internally owned clients)."""
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------- diagnostics
    def diagnostics(self) -> dict[str, Any]:
        """Zero-I/O snapshot of this provider's internal state.

        Used by ``/health/deep``; reports the last health probe without
        forcing a new one. Never leaks the instance URL or the pass token —
        only whether each is configured, plus the cached health state.
        """
        return {
            "base_url_configured": self._config.base_url is not None,
            "pass_configured": self._config.pass_token is not None,
            "scraper": self._config.scraper,
            "health_state": ("ok" if self._available else "unavailable")
            if self._available is not None
            else "not_probed",
            "health_age_seconds": (
                round(max(0.0, time.monotonic() - self._available_at), 3)
                if self._available is not None
                else None
            ),
            "health_cache_ttl_seconds": self._config.health_cache_ttl_seconds,
        }

    # ------------------------------------------------------------------- health
    def _pass_header(self) -> dict[str, str] | None:
        """A pre-set ``Cookie`` header carrying the instance pass, if configured.

        Set as an explicit header (rather than httpx per-request cookies,
        which are deprecated) so an injected ``httpx.AsyncClient`` behaves
        identically to an owned one without mutating its cookie jar.
        """
        token = self._config.pass_token
        if not token:
            return None
        return {"Cookie": f"{PASS_COOKIE_NAME}={urllib.parse.quote(token, safe='')}"}

    async def _probe_available(self) -> bool:
        """Probe the 4get instance via ``/ami4get`` (health only, never search).

        The result is cached for ``health_cache_ttl_seconds`` (0 = always
        re-probe). A probe never raises — any failure (timeout, connection
        error, HTTP 4xx/5xx including 429, or a non-``ok`` status in the
        body) degrades to ``False`` so the router treats the provider as
        unhealthy instead of crashing.
        """
        ttl = self._config.health_cache_ttl_seconds
        if self._available is not None and (time.monotonic() - self._available_at) < max(ttl, 0):
            return self._available

        async with self._health_lock:
            # Re-check under the lock: a concurrent call may have refreshed.
            if self._available is not None and (time.monotonic() - self._available_at) < max(
                ttl, 0
            ):
                return self._available
            available = False
            try:
                response = await self._client.get(HEALTH_PATH, headers=self._pass_header())
                if response.status_code == 200:
                    available = True
                    # A body that parses as an object with a ``status`` field
                    # must carry "ok" (4get's envelope); anything else is
                    # unhealthy even though the transport succeeded.
                    try:
                        payload = response.json()
                    except ValueError:
                        payload = None
                    if isinstance(payload, dict) and payload.get("status") not in (
                        None,
                        OK_STATUS,
                    ):
                        available = False
                        logger.warning(
                            "4get instance at %s %s reported status %r (HTTP 200)",
                            self._config.base_url,
                            HEALTH_PATH,
                            str(payload.get("status"))[:80],
                        )
                else:
                    # Includes HTTP 429 (pass invalid/expired): a degraded
                    # health signal on the probe, a configuration error on a
                    # search request (see search()).
                    logger.warning(
                        "4get instance at %s %s returned HTTP %d",
                        self._config.base_url,
                        HEALTH_PATH,
                        response.status_code,
                    )
            except _HTTPX_TIMEOUT_EXCEPTIONS:
                logger.warning(
                    "4get instance at %s %s request timed out after %ss",
                    self._config.base_url,
                    HEALTH_PATH,
                    self._config.timeout_seconds,
                )
            except httpx.HTTPError as exc:
                logger.warning("4get instance at %s is unreachable: %s", self._config.base_url, exc)

            self._available = available
            self._available_at = time.monotonic()
            return available

    # ---------------------------------------------------------------- capabilities
    async def capabilities(self) -> SearchCapabilities:
        """Report what this 4get provider can do right now.

        The capability set is static (4get either has its endpoints or it
        does not — there is no per-instance discovery): the five verticals
        with API endpoints, language/safe-search via the ``lang``/``nsfw``
        parameters, source selection via ``scraper``, no time ranges, no
        paging (sequential-only npt decision, see mapping module).
        Availability comes from the TTL-cached ``/ami4get`` probe.
        """
        available = await self._probe_available()
        return SearchCapabilities(
            verticals=list(SUPPORTED_VERTICALS),
            time_ranges=[],
            supports_language=True,
            supports_safe_search=True,
            supports_paging=False,
            supports_source_selection=True,
            max_results=None,
            sources=None,
            available=available,
        )

    # --------------------------------------------------------------------- search
    async def search(self, request: SearchQuery) -> SearchResponse:
        """Execute ``request`` against the 4get instance and normalize the results."""
        start = time.monotonic()

        degraded: list[str] = []
        warnings: list[str] = []

        # ---- vertical -> endpoint (one 4get request per search)
        endpoint: str | None = None
        vertical: Vertical = Vertical.WEB
        primary: tuple[str, Vertical] | None = None
        for candidate in request.verticals:
            mapped = endpoint_for_vertical(candidate)
            if mapped is None:
                degraded.append(f"vertical:{candidate.value}")
                warnings.append(
                    f"vertical '{candidate.value}' skipped: 4get has no endpoint for it"
                )
                continue
            if primary is None:
                primary = (mapped, candidate)
            else:
                degraded.append(f"vertical:{candidate.value}")
                warnings.append(
                    f"vertical '{candidate.value}' not served: 4get has no "
                    "multi-vertical endpoint (one request per search)"
                )
        if primary is None:
            # Nothing mappable: fall back to web, keeping the warnings above.
            endpoint = VERTICAL_TO_ENDPOINT[Vertical.WEB]
            vertical = Vertical.WEB
            warnings.append("no supported vertical requested; searched web instead")
        else:
            endpoint, vertical = primary

        # ---- paging (sequential-only: npt tokens are never sent or exposed)
        if request.page > 1:
            degraded.append("paging")
            warnings.append(
                "page 2+ requested; 4get paging uses single-use npt tokens that are "
                "not exposed — served page 1"
            )

        # ---- time range (not implemented: instance date filters are not used)
        if request.time_range is not None:
            degraded.append("time_range")
            warnings.append(
                "time range requested; 4get date filters are instance-defined "
                "and not implemented by this provider"
            )

        # ---- request parameters
        params: dict[str, Any] = {"s": request.query}
        scraper = request.sources[0] if request.sources else self._config.scraper
        if scraper:
            params["scraper"] = scraper
        language = request.language or self._config.default_language
        if language:
            params["lang"] = language
        if self._config.default_region:
            params["country"] = self._config.default_region
        nsfw = SAFE_SEARCH_TO_NSFW[request.safe_search]
        if request.safe_search == SafeSearch.MODERATE and self._config.default_nsfw:
            nsfw = "1"
        params["nsfw"] = nsfw

        # ---- HTTP request
        try:
            response = await self._client.get(endpoint, params=params, headers=self._pass_header())
        except _HTTPX_TIMEOUT_EXCEPTIONS as exc:
            logger.warning(
                "4get instance at %s %s request timed out after %ss",
                self._config.base_url,
                endpoint,
                self._config.timeout_seconds,
            )
            # Public error omits the instance URL (internal-URL hygiene).
            raise SearchProviderTimeoutError(
                f"4get {endpoint} request timed out after {self._config.timeout_seconds}s",
                details={"timeout_seconds": self._config.timeout_seconds},
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("4get instance at %s is unreachable: %s", self._config.base_url, exc)
            # The public error deliberately omits the instance URL and the
            # upstream exception text (it may echo internal addresses).
            raise SearchProviderUnavailableError("4get instance is unreachable") from exc

        if response.status_code == 429:
            # Missing/invalid pass: a *configuration* problem, not a transient
            # failure — never retryable/fallback-eligible. The public error
            # never carries the pass token or the upstream body.
            logger.warning(
                "4get instance at %s %s returned HTTP 429 (pass invalid or expired): %s",
                self._config.base_url,
                endpoint,
                response.text[:300],
            )
            raise NoProviderAvailableError(
                "4get instance rejected the configured pass (HTTP 429): the provider "
                "requires a valid pass — configure SCRAPER_SEARCH_4GET_PASS_TOKEN "
                "with a current instance pass",
                details={"status_code": 429},
            )

        if response.status_code >= 400:
            logger.warning(
                "4get instance at %s %s returned HTTP %d: %s",
                self._config.base_url,
                endpoint,
                response.status_code,
                response.text[:300],
            )
            raise SearchProviderUnavailableError(
                f"4get {endpoint} returned HTTP {response.status_code}",
                details={"status_code": response.status_code},
            )

        payload = _parse_json_payload(response, endpoint)

        status = payload.get("status")
        if status != OK_STATUS:
            logger.warning(
                "4get instance at %s %s reported a non-ok status: %s",
                self._config.base_url,
                endpoint,
                response.text[:300],
            )
            # The status value is short envelope metadata, safe to name; the
            # body itself is never surfaced.
            status_text = f" (status: {status})" if isinstance(status, str) and status else ""
            raise InvalidProviderResponseError(
                f"4get {endpoint} reported a failure{status_text}",
                details={"status_code": response.status_code},
            )

        raw_results = payload.get(RESULT_KEY)
        if not isinstance(raw_results, list):
            logger.warning(
                "4get %s response is missing the '%s' list: %s",
                endpoint,
                RESULT_KEY,
                response.text[:300],
            )
            raise InvalidProviderResponseError(
                f"4get {endpoint} response is missing the '{RESULT_KEY}' list",
                details={"status_code": response.status_code},
            )

        results: list[SearchResult] = []
        skipped = 0
        for rank, raw in enumerate(raw_results, start=1):
            result = _normalize_result(raw, rank, vertical, self.name)
            if result is None:
                skipped += 1
            else:
                results.append(result)
        if skipped:
            warnings.append(f"dropped {skipped} malformed 4get result(s)")
        if not results and raw_results:
            raise InvalidProviderResponseError(
                "4get response contained results, but none could be normalized",
                details={"raw_count": len(raw_results)},
            )

        # ---- limit (4get page size is instance-controlled; truncate here)
        if request.limit is not None and len(results) > request.limit:
            results = results[: request.limit]
            degraded.append("limit")
            warnings.append(f"truncated results to {request.limit} (requested limit)")

        elapsed_ms = int((time.monotonic() - start) * 1000)

        return SearchResponse(
            query=request.query,
            verticals=[vertical],
            results=results,
            metadata=SearchMetadata(
                provider=self.name,
                providers_attempted=[self.name],
                elapsed_ms=elapsed_ms,
                warnings=warnings,
                degraded_capabilities=degraded,
                page=1,
                total_pages=None,
                total_results=None,
            ),
        )


# ================================================================== helpers
def _parse_json_payload(response: httpx.Response, endpoint: str) -> dict[str, Any]:
    """Parse a JSON object payload, raising a domain error on failure.

    Error details are public-safe: upstream body previews are logged, never
    surfaced in the raised error (raw provider blobs must not leak).
    """
    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning(
            "4get %s returned invalid JSON (HTTP %d): %s",
            endpoint,
            response.status_code,
            response.text[:300],
        )
        raise InvalidProviderResponseError(
            f"4get {endpoint} returned invalid JSON",
            details={
                "status_code": response.status_code,
                "content_type": response.headers.get("content-type"),
            },
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidProviderResponseError(
            f"4get {endpoint} returned a non-object JSON payload",
            details={"content_type": response.headers.get("content-type")},
        )
    return payload


def _normalize_result(
    raw: Any, rank: int, vertical: Vertical, provider: str
) -> SearchResult | None:
    """Map one 4get result dict to a domain result.

    Returns ``None`` for entries that cannot be normalized (missing/invalid
    URL or title) instead of raising, so a single bad entry does not poison
    the whole page. 4get fields are mostly nullable; unknown keys (``npt``,
    ``scraper``, ``extendedsearch``, ...) are never carried over.
    """
    if not isinstance(raw, dict):
        return None

    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith(_HTTP_URL_PREFIXES):
        return None

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None

    snippet = raw.get("snippet")
    if not isinstance(snippet, str) or not snippet.strip():
        snippet = None
    if snippet is None and vertical == Vertical.MUSIC:
        # Music items carry ``artist``/``album`` instead of a snippet.
        artist = raw.get("artist") if isinstance(raw.get("artist"), str) else None
        album = raw.get("album") if isinstance(raw.get("album"), str) else None
        composed = " — ".join(part.strip() for part in (artist, album) if part and part.strip())
        snippet = composed or None

    source = raw.get("engine")
    if source is not None:
        if not isinstance(source, str) or not source.strip():
            source = None
        else:
            source = source.strip()[:100] or None

    thumbnail = raw.get("image")
    if thumbnail is not None:
        if not isinstance(thumbnail, str) or not thumbnail.startswith(_HTTP_URL_PREFIXES):
            thumbnail = None

    return SearchResult(
        title=title.strip(),
        url=url,
        snippet=snippet,
        rank=rank,
        vertical=vertical,
        source=source,
        provider=provider,
        thumbnail_url=thumbnail,
        published_at=epoch_to_datetime(raw.get("date")),
        # NOTE: 4get's 'time' (video duration, seconds) is deliberately not
        # mapped — the domain model has no duration field (public API change
        # is out of scope), mirroring the SearXNG provider's 'score' drop.
    )
