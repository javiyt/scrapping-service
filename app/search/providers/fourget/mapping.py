"""Mapping between the normalized search domain and 4get concepts.

4get-specific names — endpoint paths, request parameter names, the ``nsfw``
flag, the ``status: "ok"`` envelope, the single-use ``npt`` page token, the
``pass`` cookie — live **only** in this module and the provider. The
provider-agnostic domain (:mod:`app.search.models`) never references them,
and 4get details never leak into public domain types.

Request parameters this provider relies on
==========================================

The 4get request-parameter set is instance-defined (craft requests in the
4get web UI and replace ``/web`` with ``/api/v1/web`` to verify yours); this
provider only ever sends the following, which match the web UI names::

    ==================  ==============================================
    Parameter           Meaning
    ==================  ==============================================
    ``s``               the search query (guaranteed by the API)
    ``scraper``         engine selection (instance-defined values, e.g.
                        ``duckduckgo``, ``brave``, ``google``, ``yandex``,
                        ``marginalia``); from ``SearchQuery.sources`` (first
                        entry) or the configured default scraper
    ``lang``            language tag; from the request or the configured
                        default language
    ``country``         region; from the configured default region
    ``nsfw``            ``"0"`` = safe (moderate/strict), ``"1"`` = allow
                        NSFW (safe-search off, or a configured NSFW default
                        at moderate)
    ``npt``             next-page token — **never sent** by this provider
    ==================  ==============================================

Note: ``extendedsearch`` (ddg scraper only) and the instance date-filter
parameters are deliberately not used — date filtering is not implemented
for this provider (see :data:`SUPPORTED_VERTICALS` / capabilities), so a
requested time range is reported as degraded instead of silently dropped.

Paging decision (npt)
=====================

4get paginates with opaque, single-use ``npt`` tokens that expire after
~15 minutes instead of page numbers. This provider therefore serves
**page 1 only**: it never sends an ``npt`` parameter and never exposes a
response ``npt`` token to clients (tokens are single-use, and holding a
server-side token chain would conflict with the provider-agnostic contract
and with 4get's terms of service). Paging is not advertised
(``supports_paging = False``), so the router never routes page > 1 requests
to this provider; a direct ``search()`` call with page > 1 degrades to
page 1 with a ``paging`` token and a warning.

Result fields that are intentionally **not** mapped
===================================================

- ``time`` — duration in seconds (video results). The domain
  :class:`app.search.models.SearchResult` has no duration field, and adding
  one would change the public Search API (out of scope for this provider),
  so the value is dropped, mirroring how the SearXNG provider drops its
  per-engine ``score``.
- ``answer`` / ``spelling`` / ``related`` / ``table`` — rich web extras;
  ignored (they carry no per-result URL/title contract).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.search.models import SafeSearch, Vertical

#: The stable public provider name (the package name stays a valid
#: identifier — ``fourget`` — while the provider is known as ``4get``).
NAME = "4get"

#: ``/ami4get`` — instance info endpoint used as the health probe. Search
#: endpoints are never used for health/capabilities.
HEALTH_PATH = "/ami4get"

#: 4get vertical → API endpoint path. Unsupported domain verticals
#: (social, it, science, maps, files) have no 4get endpoint and are not
#: advertised.
VERTICAL_TO_ENDPOINT: dict[Vertical, str] = {
    Vertical.WEB: "/api/v1/web",
    Vertical.NEWS: "/api/v1/news",
    Vertical.IMAGES: "/api/v1/images",
    Vertical.VIDEOS: "/api/v1/videos",
    Vertical.MUSIC: "/api/v1/music",
}

#: API endpoint path → vertical (reverse of the above).
ENDPOINT_TO_VERTICAL: dict[str, Vertical] = {
    path: vertical for vertical, path in VERTICAL_TO_ENDPOINT.items()
}

#: Verticals this provider supports, in capabilities-report order.
SUPPORTED_VERTICALS: tuple[Vertical, ...] = tuple(VERTICAL_TO_ENDPOINT)

#: Normalized safe-search level → 4get ``nsfw`` parameter value.
#: ``Moderate`` may be overridden to ``"1"`` by a configured NSFW default
#: (see :class:`FourgetConfig.default_nsfw`); strict never allows NSFW.
SAFE_SEARCH_TO_NSFW: dict[SafeSearch, str] = {
    SafeSearch.OFF: "1",
    SafeSearch.MODERATE: "0",
    SafeSearch.STRICT: "0",
}

#: The envelope value that marks a successful 4get response.
OK_STATUS = "ok"

#: The key holding the list of results in a 4get search response.
RESULT_KEY = "result"

#: The cookie name carrying the captcha-derived instance pass.
PASS_COOKIE_NAME = "pass"


def endpoint_for_vertical(vertical: Vertical) -> str | None:
    """Return the 4get endpoint path for a vertical, or ``None``."""
    return VERTICAL_TO_ENDPOINT.get(vertical)


def epoch_to_datetime(value: Any) -> datetime | None:
    """Convert a 4get ``date`` value (epoch seconds) to a UTC datetime.

    4get reports dates as epoch seconds; the field may be null or even a
    numeric string on some instances. Anything that is not a finite
    non-negative number maps to ``None`` (unknown date), never to a raise.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not number >= 0:  # also rejects NaN (NaN comparisons are False)
        return None
    return datetime.fromtimestamp(number, tz=UTC)
