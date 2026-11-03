"""Camofox implementation of the :class:`SearchProvider` contract.

This adapter translates normalized search requests
(:class:`app.search.models.SearchQuery`) into Camofox *search macro*
navigations and normalizes what the rendered results page yields back into
project-owned domain types.  Clients never see the macro names: the public
Search API only ever speaks verticals and plain source names (``google`` /
``reddit`` / ``youtube``), and the mapping to ``@google_search`` /
``@reddit_search`` / ``@youtube_search`` lives entirely inside this
provider (see :mod:`app.search.providers.camofox.mapping`).

How a search runs
=================

1. A short-lived :class:`app.camofox.session.CamofoxSession` is created on
   a shared, reusable :class:`app.camofox.client.CamofoxClient` (one
   long-lived HTTP client per provider; many throwaway sessions).
2. One tab is created and navigated with the resolved search macro for the
   requested query.
3. The rendered results page is fetched as page links
   (``get_links``); entries whose URL host matches the search engine's own
   host are result candidates (search engines embed result links alongside
   their own navigation chrome).
4. The accessibility-tree ``snapshot`` is parsed *deterministically* (regex
   over the ref-tagged text — never an LLM) to attach titles and snippets
   to the candidate URLs, in order.
5. The tab is closed on **every** path (success and failure alike); the
   logical session is released best-effort so the pool drains.

Result normalization (page material → :class:`SearchResult`):

====================================  ==========================
Source                                Domain field
====================================  ==========================
snapshot link text (first match)      ``title``
page link URL (must be absolute)      ``url``
snapshot text following the link      ``snippet`` (when non-empty)
position in candidate order           ``rank``
the macro the search used             ``vertical`` (web/social/videos)
the source the search was mapped from ``source`` (e.g. ``"google"``)
always                                ``provider = "camofox"``
====================================  ==========================

Thumbnails and ``published_at`` are only populated when the snapshot
unambiguously yields them next to a result; most result layouts yield
neither, and that is fine — the fields are optional.

Resource lifecycle
==================

Every ``search()``/``capabilities()`` run uses one session with at most one
tab, and :meth:`_release_search_resources` (called from a ``finally`` block
on every path) closes the tab (tolerating an already-gone tab) and closes
the logical session (tolerating an already-expired session).  Sessions are
short-lived logical identities; the *browser* itself stays in the reusable
Camofox pool.  Browser resources are therefore always released, and the
client/HTTP connection pool is reused across searches.

Failures
========

Camofox errors are mapped onto the search error types from
:mod:`app.core.errors`:

======================================  ==========================
Camofox situation                       Search error
======================================  ==========================
service unreachable / 5xx               ``search_provider_unavailable``
any timeout (client- or server-side)    ``search_provider_timeout``
auth rejected                           ``search_provider_unavailable``
invalid / unparseable payload           ``invalid_provider_response``
tab closed or session gone              ``search_provider_unavailable``
navigation (macro) failed               ``search_provider_unavailable``
======================================  ==========================

Public error messages carry only the macro-less endpoint and status;
upstream text and the service URL are logged (server-side) instead, so the
exceptions are safe to render into HTTP responses.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.camofox.client import CamofoxClient
from app.camofox.session import CamofoxSession
from app.camofox.types import (
    CamofoxHealthStatus,
    CamofoxLink,
    CamofoxLinks,
    CamofoxSnapshot,
    CamofoxTab,
)
from app.core.errors import (
    CamofoxAuthError,
    CamofoxError,
    CamofoxInvalidResponseError,
    CamofoxNavigationError,
    CamofoxSessionExpiredError,
    CamofoxTabNotFoundError,
    CamofoxTimeoutError,
    CamofoxUnavailableError,
    InvalidProviderResponseError,
    ScraperError,
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
)
from app.search.models import (
    SearchCapabilities,
    SearchMetadata,
    SearchQuery,
    SearchResponse,
    SearchResult,
    Vertical,
)
from app.search.providers.camofox.config import NAME, CamofoxSearchConfig
from app.search.providers.camofox.mapping import (
    SOURCE_TO_MACRO,
    VERTICAL_TO_MACRO,
    normalize_source_name,
)

logger = logging.getLogger("scraper-api.search.camofox")

# Macro → host of the engine whose search-results page it lands on.  Links
# whose host matches one of these are result candidates; the engine's own
# UI (nav, footer, chrome) is filtered out.  ``www.``-style prefixes are
# stripped before matching, so ``www.reddit.com`` matches ``reddit.com``.
_MACRO_RESULT_HOSTS: dict[str, tuple[str, ...]] = {
    # Google results live on arbitrary result hosts, so no host filter is
    # applied for the web macro (engine chrome is filtered structurally).
    "@google_search": (),
    "@reddit_search": ("reddit.com",),
    "@youtube_search": ("youtube.com", "youtu.be"),
}

# How far past a result link node the snapshot may be scanned for its
# snippet text (longer would collide with the following result).
_SNIPPET_SCAN_REFS = 30

# Upper bound for the deterministic page parse (a pathological page must
# never burn the whole macro time budget on text processing).
_SNIPPET_SCAN_CHAR_LIMIT = 1_000_000


# ==================================================================== errors
def _map_camofox_error(exc: CamofoxError) -> ScraperError:
    """Map a client-level Camofox error onto the search error family.

    The router treats ``search_provider_unavailable`` / ``search_provider_
    timeout`` / ``invalid_provider_response`` as *transient* (auto-mode
    fallback applies); the mapping keeps transient vs. terminal semantics
    consistent with the SearXNG provider.
    """
    details = {**(exc.details or {}), "provider": NAME}
    if isinstance(exc, CamofoxTimeoutError):
        return SearchProviderTimeoutError("Camofox search timed out", details=details)
    if isinstance(
        exc,
        (
            CamofoxUnavailableError,
            CamofoxAuthError,
            CamofoxTabNotFoundError,
            CamofoxSessionExpiredError,
            CamofoxNavigationError,
            CamofoxInvalidResponseError,
            CamofoxError,
        ),
    ):
        return SearchProviderUnavailableError(
            "Camofox could not complete the search", details=details
        )
    return InvalidProviderResponseError("Camofox search failed unexpectedly", details=details)
    return InvalidProviderResponseError("Camofox search failed", details=details)


# =================================================================== support
class _SnapshotLink(BaseModel):
    """A link parsed from an accessibility-tree snapshot (ref-tagged)."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    text: str | None = None
    url: str | None = None


# ``- link "Title" [ref=e123, url=https://...]`` — accessibility-tree link
# nodes with an optional quoted title and a ``ref=...`` (plus optional
# ``url=...``) in the trailing brackets.
_SNAPSHOT_LINK_PATTERN = re.compile(
    r'link(?P<text> "[^"]*")?\s*\[\s*ref=(?P<ref>e\d+)(?:,\s*url=(?P<url>\S+))?\s*\]'
)
_SNAPSHOT_REF_PATTERN = re.compile(r"\[ref=(?P<ref>e\d+)(?:,\s*url=\S+)?\]")


def _parse_snapshot_links(snapshot_text: str) -> list[_SnapshotLink]:
    """Parse links from an accessibility-tree snapshot (deterministic).

    Returns the links in document order, each carrying its ref, its quoted
    text (when any) and its ``url=`` attribute (when any).  Text that is
    only a host or a single ``/`` path is not a title — it is stripped so
    callers must fall back to the page-link text.
    """
    links: list[_SnapshotLink] = []
    for match in _SNAPSHOT_LINK_PATTERN.finditer(snapshot_text):
        text = (match.group("text") or "").strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1].strip()
        elif not text:
            text = ""
        if len(text) <= 3 and ("/" in text or "." in text):
            text = ""
        url = (match.group("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            url = ""
        links.append(_SnapshotLink(ref=match.group("ref"), text=text or None, url=url or None))
    return links


def _collect_snippets(snapshot_text: str, refs: list[str]) -> dict[str, str]:
    """Map each ref to the text that follows its link node (deterministic).

    The accessibility tree is a flat, ordered document: for every result
    link, its snippet is the first non-heading, non-link text node between
    this link node and the next link's ref (bounded by
    :data:`_SNIPPET_SCAN_REFS` refs / :data:`_SNIPPET_SCAN_CHAR_LIMIT` chars
    so a pathological page cannot stall the parse).

    Refs without following text (e.g. the last link, or a link immediately
    followed by another link) get no snippet.
    """
    snippets: dict[str, str] = {}
    start_of_ref: dict[str, int] = {}
    end_of_ref: dict[str, int] = {}
    ref_positions: list[tuple[int, str]] = []
    for match in _SNAPSHOT_REF_PATTERN.finditer(snapshot_text):
        ref = match.group("ref")
        ref_positions.append((match.start(), ref))
        start_of_ref.setdefault(ref, match.start())
        end_of_ref[ref] = match.end()

    for ref in refs:
        window_start = end_of_ref.get(ref)
        if window_start is None:
            continue
        end = len(snapshot_text)
        # Bound the scan: stop at the next *link* node after this one.
        for position, other_ref in ref_positions:
            if position > window_start:
                end = position
                break
        window = snapshot_text[window_start:end]
        if len(window) > _SNIPPET_SCAN_CHAR_LIMIT:
            window = window[:_SNIPPET_SCAN_CHAR_LIMIT]
        snippet = _extract_snippet_text(window)
        if snippet:
            snippets[ref] = snippet

    return snippets


def _extract_snippet_text(window: str) -> str | None:
    """Return the first prose-like text run in ``window``.

    Skips nodes that belong to result chrome (headings, other links, short
    bare URLs/paths, pure numbers, and bare timestamps such as ``2 days
    ago`` / ``3 hours ago``) and returns the first substantial text run,
    whitespace-collapsed.
    """
    for line in window.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith("- link"):
            continue
        if re.fullmatch(r"(https?://\S+|/\S*|\d+(?:\.\d+)?\w*)", line):
            continue
        if re.fullmatch(r"\d+\s+(minutes?|hours?|days?|weeks?|months?|years?)\s+ago", line, re.I):
            continue
        collapsed = re.sub(r"\s+", " ", line)
        if len(collapsed) < 15:
            continue
        return collapsed
    return None


# ==================================================================== provider
class CamofoxSearchProvider:
    """Camofox backend for the provider-agnostic search contract.

    Satisfies the structural :class:`app.search.SearchProvider` protocol
    (``name`` + ``capabilities()`` + ``search()``) plus a ``close()``
    (releases the shared HTTP client) and a ``diagnostics()`` (consumed by
    ``/health/deep`` like the SearXNG provider's).
    """

    def __init__(
        self,
        config: CamofoxSearchConfig,
        client: CamofoxClient,
    ) -> None:
        self._config = config
        if client is None:
            raise ValueError(
                "CamofoxSearchProvider requires a shared CamofoxClient "
                "(build_search_providers resolves it from the camofox section)"
            )
        self._client = client
        self._client_config = client.config
        self._session: CamofoxSession | None = None
        self._state_lock = asyncio.Lock()
        self._last_health: CamofoxHealthStatus | None = None
        self._last_health_at: float = 0.0

    @property
    def name(self) -> str:
        return NAME

    @property
    def client(self) -> CamofoxClient:
        return self._client

    async def close(self) -> None:
        """Close the shared HTTP client (safe to call multiple times)."""
        await self._client.close()

    # ------------------------------------------------------------- diagnostics
    def diagnostics(self) -> dict[str, Any]:
        """Zero-I/O snapshot of this provider's internal state.

        Used by ``/health/deep``; reports the last health probe without
        forcing a new one and never leaks the service URL or credentials.
        """
        health = self._last_health
        elapsed = time.monotonic() - self._last_health_at if health is not None else None
        return {
            "base_url_configured": self._client_config.base_url is not None,
            "last_health": (
                {
                    "available": health.available,
                    "reason": health.reason,
                    "browser_connected": health.browser_connected,
                    "pool_size": health.pool_size,
                    "age_seconds": round(max(0.0, elapsed), 3) if elapsed is not None else None,
                }
                if health is not None
                else None
            ),
        }

    # ------------------------------------------------------------------ capabilities
    async def capabilities(self) -> SearchCapabilities:
        """Report what this provider can do right now.

        The verticals/sources are the static macro set (Camofox either has
        its macros or it does not — there is no per-instance discovery).
        Availability comes from the cheap shared health probe
        (``GET /health``, no tab created); probe failures are reported as
        ``available=False`` and never raised, so the router's bounded probe
        treats the provider as unhealthy instead of crashing.
        """
        available = await self._probe_available()
        return SearchCapabilities(
            verticals=[vertical for vertical, _ in VERTICAL_TO_MACRO.items()],
            time_ranges=[],
            supports_language=False,
            supports_safe_search=False,
            supports_paging=False,
            supports_source_selection=True,
            max_results=self._config.max_results,
            sources=list(SOURCE_TO_MACRO),
            available=available,
        )

    async def _probe_available(self) -> bool:
        """Probe the shared Camofox service (health endpoint, no tab).

        A cached healthy probe is reused (it costs nothing); any other state
        re-probes so the router sees recoveries.  All failures degrade to
        ``False`` — a probe never raises.
        """
        async with self._state_lock:
            if self._last_health is not None and self._last_health.available:
                return True
            try:
                self._last_health = await self._client.probe_health()
            except CamofoxError:
                self._last_health = None
            finally:
                self._last_health_at = time.monotonic()
            return self._last_health is not None and self._last_health.available

    # ------------------------------------------------------------------ search
    async def search(self, request: SearchQuery) -> SearchResponse:
        """Execute ``request`` through the resolved Camofox search macro."""
        start = time.monotonic()

        macro, source_name, vertical = self._resolve_macro(request)

        degraded: list[str] = []
        warnings: list[str] = []
        self._note_degraded(request, degraded, warnings)

        session = self._client.create_session()
        self._session = session
        tab: CamofoxTab | None = None
        try:
            tab = await session.create_tab()
            navigate = await session.navigate(tab, macro=macro, query=request.query)
            if not navigate.ok:
                raise CamofoxNavigationError(
                    "Camofox search macro navigation reported failure",
                    details={"macro": macro},
                )
            links = await session.get_links(tab, limit=100)
            snapshot = await session.snapshot(tab)
            results = self._normalize_results(
                request, macro, source_name, vertical, links, snapshot
            )
        except CamofoxError as exc:
            raise _map_camofox_error(exc) from exc
        finally:
            try:
                await self._release_search_resources(session, tab)
            except Exception:  # pragma: no cover - defensive; never mask the result
                self._session = None

        # ---- limit (the browser page yields what it yields; truncate here)
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
                page=None,
                total_pages=None,
                total_results=None,
            ),
        )

    # --------------------------------------------------------------- resolution
    def _resolve_macro(self, request: SearchQuery) -> tuple[str, str | None, Vertical]:
        """Resolve the single search macro for a request.

        Precedence (a search run is one macro navigation, so exactly one
        macro is chosen):

        1. ``sources`` — the first requested source that maps to a known
           macro.  Unknown sources are reported by the caller as degraded
           (the provider cannot restrict to them).
        2. ``verticals`` — the first requested vertical with a macro mapping
           (``web``→google, ``social``→reddit, ``videos``→youtube); unmapped
           verticals are reported as degraded.

        Returns ``(macro, source_name, vertical)`` — ``source_name`` is the
        public source the search was mapped from (``None`` for the default
        web search), and ``vertical`` is the vertical the results are tagged
        with (the vertical the macro searches; a source-driven search
        searches the engine's domain, so results are tagged with that
        engine's vertical).
        """
        if request.sources:
            for source in request.sources:
                macro = SOURCE_TO_MACRO.get(normalize_source_name(source))
                if macro is not None:
                    vertical = next(
                        (v for v, m in VERTICAL_TO_MACRO.items() if m == macro), Vertical.WEB
                    )
                    return macro, source, vertical
            return SOURCE_TO_MACRO["google"], None, Vertical.WEB

        for vertical in request.verticals:
            macro = VERTICAL_TO_MACRO.get(vertical)
            if macro is not None:
                source = next((s for s, m in SOURCE_TO_MACRO.items() if m == macro), None)
                return macro, source, vertical
        return SOURCE_TO_MACRO["google"], None, Vertical.WEB

    def _note_degraded(
        self, request: SearchQuery, degraded: list[str], warnings: list[str]
    ) -> None:
        """Report request parts this provider cannot honor (honestly).

        The macro set is static and small, so the honest report is: the
        parts of the request outside it.  These tokens mirror
        :meth:`SearchCapabilities.degradations` for the same request, so the
        router's ``_merge_degradations`` never duplicates them.
        """
        for vertical in request.verticals:
            if vertical not in VERTICAL_TO_MACRO and f"vertical:{vertical.value}" not in degraded:
                degraded.append(f"vertical:{vertical.value}")
        if request.time_range is not None and "time_range" not in degraded:
            degraded.append("time_range")
            warnings.append("time range requested; Camofox macros do not support it")
        if request.language is not None and "language" not in degraded:
            degraded.append("language")
            warnings.append("language requested; Camofox macros do not support it")
        if (
            request.safe_search is not None
            and request.safe_search.value != "off"
            and "safe_search" not in degraded
        ):
            degraded.append("safe_search")
            warnings.append("safe-search requested; Camofox macros do not support it")
        if request.page > 1 and "paging" not in degraded:
            degraded.append("paging")
            warnings.append("page 2+ requested; Camofox macros serve page 1 only")
        if request.sources and not any(
            normalize_source_name(source) in SOURCE_TO_MACRO for source in request.sources
        ):
            if "sources" not in degraded:
                degraded.append("sources")
            warnings.append(
                "no requested source is available on Camofox; used the default web search"
            )

    # ------------------------------------------------------------------ results
    def _normalize_results(
        self,
        request: SearchQuery,
        macro: str,
        source_name: str | None,
        vertical: Vertical,
        links: CamofoxLinks,
        snapshot: CamofoxSnapshot,
    ) -> list[SearchResult]:
        """Deterministically build normalized results from links + snapshot.

        Result candidates are the page links whose host matches the engine
        for the macro used (engine chrome is filtered out).  Titles and
        snippets come from the snapshot text of the same nodes; the page
        link's own text is the fallback title.  Entries with no usable URL
        are dropped; an engine page that yields nothing is *not* an error —
        it is an empty result set (the navigation succeeded).
        """
        hosts = _MACRO_RESULT_HOSTS.get(macro, ())
        snapshot_links = (
            [link for link in _parse_snapshot_links(snapshot.snapshot) if link.url]
            if snapshot.snapshot
            else []
        )
        snippets = (
            _collect_snippets(snapshot.snapshot, [link.ref for link in snapshot_links])
            if snapshot.snapshot
            else {}
        )

        candidates: list[CamofoxLink] = []
        seen_urls: set[str] = set()
        for link in links.links:
            host = _host_of(link.url)
            # An empty host tuple means "no filtering" (Google web results
            # live on arbitrary result hosts); otherwise the host must match.
            if host is None or not _host_matches(host, hosts):
                continue
            if link.url in seen_urls:
                continue
            seen_urls.add(link.url)
            candidates.append(link)

        results: list[SearchResult] = []
        rank = 0
        for candidate in candidates:
            snapshot_link = _snapshot_link_for_url(candidate.url, snapshot_links)
            title = self._title_for(candidate, snapshot_link)
            if not title:
                continue
            snippet: str | None = None
            if snapshot.snapshot:
                # The snippet is the text that follows the *candidate's own*
                # link node in the accessibility tree (its ref), not the ref
                # of a sibling node that happens to carry the same URL.
                for link in snapshot_links:
                    if link.ref and _url_match(candidate.url, link.url or ""):
                        snippet = snippets.get(link.ref)
                        break
            if snippet is not None and not snippet.strip():
                snippet = None
            rank += 1
            results.append(
                SearchResult(
                    title=title,
                    url=candidate.url,
                    snippet=snippet or None,
                    rank=rank,
                    vertical=vertical,
                    source=source_name[:100] if (source_name and request.sources) else None,
                    provider=self.name,
                )
            )
        return results

    @staticmethod
    def _snapshot_link_for_url(
        url: str, snapshot_links: list[_SnapshotLink]
    ) -> _SnapshotLink | None:
        """Find the snapshot link node carrying ``url`` (exact, then prefix)."""
        for link in snapshot_links:
            if link.url and link.url.rstrip("/") == url.rstrip("/"):
                return link
        for link in snapshot_links:
            if link.url and url.startswith(link.url.rstrip("/")[: max(1, len(link.url) - 1)]):
                return link
        return None

    @staticmethod
    def _title_for(candidate: CamofoxLink, snapshot_link: _SnapshotLink | None) -> str | None:
        """Best available title: snapshot link text, else the page link text."""
        if snapshot_link is not None and snapshot_link.text:
            title = re.sub(r"\s+", " ", snapshot_link.text).strip()
            if title:
                return title
        text = re.sub(r"\s+", " ", candidate.text or "").strip()
        if text and len(text) > 1:
            return text
        return None

    # ------------------------------------------------------------------ lifecycle
    async def _release_search_resources(
        self, session: CamofoxSession, tab: CamofoxTab | None
    ) -> None:
        """Close every resource a search created — on success **and** failure.

        Tab first (it is what the ticket calls for), then the logical
        session.  Both tolerate an already-gone upstream state (the server
        may have cleaned up meanwhile) and a failed close must never mask
        the search's own outcome: failures are logged and swallowed here.
        """
        if self._session is session:
            self._session = None
        if tab is not None:
            try:
                await session.close_tab(tab)
            except CamofoxError as exc:
                logger.warning("Failed to close search tab %s: %s", tab.tab_id, exc.message)
                session.forget_tab(tab.tab_id)
            except Exception as exc:  # noqa: BLE001 - cleanup must never raise
                logger.warning("Unexpected error closing search tab: %s", exc)
                session.forget_tab(tab.tab_id)
        try:
            await session.close()
        except CamofoxError as exc:
            logger.warning("Failed to release search session: %s", exc.message)
            session.forget_all_tabs()
        except Exception as exc:  # noqa: BLE001 - cleanup must never raise
            logger.warning("Unexpected error releasing search session: %s", exc)
            session.forget_all_tabs()


# ================================================================== helpers
def _snapshot_link_for_url(url: str, snapshot_links: list[_SnapshotLink]) -> _SnapshotLink | None:
    """Find the snapshot link node carrying ``url`` (exact, then prefix)."""
    for link in snapshot_links:
        if link.url and _url_match(url, link.url):
            return link
    return None


def _host_of(url: str) -> str | None:
    """Extract the host from an absolute URL (lower-cased), else None."""
    if not isinstance(url, str):
        return None
    if not url.startswith(("http://", "https://")):
        return None
    rest = url.split("://", 1)[1]
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.rsplit(":", 1)[0]
    host = host.lower()
    if not host:
        return None
    for prefix in ("www.", "m.", "old.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    return host


def _host_matches(host: str, hosts: tuple[str, ...]) -> bool:
    """Whether ``host`` is one of the engine hosts (exact or subdomain).

    An empty ``hosts`` tuple means *no filtering* — every http(s) host is a
    candidate (used for Google web results, which live on arbitrary hosts).
    """
    if not hosts:
        return True
    for candidate in hosts:
        if host == candidate or host.endswith("." + candidate):
            return True
    return False


def _url_match(candidate_url: str, snapshot_url: str) -> bool:
    """Whether a candidate URL and a snapshot URL refer to the same node.

    Trailing slashes and fragments are ignored; a prefix match is also
    accepted (the accessibility tree sometimes elates a long URL).
    """
    if not snapshot_url:
        return False
    a = candidate_url.rstrip("/")
    b = snapshot_url.rstrip("/")
    if a == b:
        return True
    shorter = min(len(a), len(b))
    return shorter > 8 and (a[:shorter] == b[:shorter])
