"""Mapping between the normalized search domain and SearXNG concepts.

SearXNG-specific names — category names, engine identifiers, numeric
safesearch levels — live **only** in this module.  The provider-agnostic
domain (:mod:`app.search.models`) never references them, and SearXNG
details never leak into public domain types.
"""

from __future__ import annotations

from typing import Any

from app.search.models import SafeSearch, TimeRange, Vertical

#: Normalized vertical → SearXNG category name.
#:
#: SearXNG category names come from its own installation (see ``/config``);
#: the mapping covers SearXNG's standard categories.  Categories that a
#: given instance does not enable are discovered via ``/config`` and
#: reported as degraded capabilities instead of being silently dropped.
VERTICAL_TO_CATEGORY: dict[Vertical, str] = {
    Vertical.WEB: "general",
    Vertical.NEWS: "news",
    Vertical.IMAGES: "images",
    Vertical.VIDEOS: "videos",
    Vertical.SOCIAL: "social media",
    Vertical.IT: "it",
    Vertical.SCIENCE: "science",
    Vertical.MAPS: "map",
    Vertical.MUSIC: "music",
    Vertical.FILES: "files",
}

#: SearXNG category name → normalized vertical (reverse of the above).
CATEGORY_TO_VERTICAL: dict[str, Vertical] = {
    category: vertical for vertical, category in VERTICAL_TO_CATEGORY.items()
}

#: Normalized safe-search level → SearXNG numeric level.
SAFE_SEARCH_LEVELS: dict[SafeSearch, int] = {
    SafeSearch.OFF: 0,
    SafeSearch.MODERATE: 1,
    SafeSearch.STRICT: 2,
}

#: Normalized time range → SearXNG ``time_range`` parameter value.
TIME_RANGE_PARAMS: dict[TimeRange, str] = {
    TimeRange.DAY: "day",
    TimeRange.WEEK: "week",
    TimeRange.MONTH: "month",
    TimeRange.YEAR: "year",
}


def verticals_to_categories(verticals: list[Vertical]) -> list[str]:
    """Map normalized verticals to SearXNG category names.

    Unknown verticals map to SearXNG's default ``general`` category so that
    forward-compatibility with new domain verticals does not break requests.
    """
    return [VERTICAL_TO_CATEGORY.get(v, "general") for v in verticals]


def category_to_vertical(category: Any) -> Vertical:
    """Map a SearXNG result category back to a normalized vertical.

    Falls back to :attr:`Vertical.WEB` for unknown or missing categories
    (SearXNG reports ``""`` when a result has no specific category).
    """
    if isinstance(category, str):
        mapped = CATEGORY_TO_VERTICAL.get(category)
        if mapped is not None:
            return mapped
    return Vertical.WEB
