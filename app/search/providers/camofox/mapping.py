"""Mapping between the normalized search domain and Camofox search macros.

Camofox-specific names — the search *macro* identifiers used in the
Camofox ``navigate`` request — live **only** in this module.  The
provider-agnostic domain (:mod:`app.search.models`) never references them,
and macro names never leak into the public Search API or into normalized
responses.
"""

from __future__ import annotations

from app.search.models import Vertical

#: Generic ``sources`` value → Camofox search macro.
#:
#: Requested ``sources`` are matched against these names case-insensitively
#: (and with or without a leading ``@`` / a ``_search`` suffix), so clients
#: keep using plain, engine-style names in the public API.
SOURCE_TO_MACRO: dict[str, str] = {
    "google": "@google_search",
    "reddit": "@reddit_search",
    "youtube": "@youtube_search",
}

#: The macros a source must map to for the provider to expose it as a
#: discoverable source.
_ALL_MACRO_NAMES = tuple(SOURCE_TO_MACRO.values())

#: Normalized vertical → Camofox search macro.
#:
#: The ticket's suggested vertical behavior: ``web`` → Google,
#: ``social`` → Reddit, ``videos`` → YouTube.  Verticals with no Camofox
#: macro are simply not advertised (they degrade through
#: :meth:`SearchCapabilities.degradations`).
VERTICAL_TO_MACRO: dict[Vertical, str] = {
    Vertical.WEB: SOURCE_TO_MACRO["google"],
    Vertical.SOCIAL: SOURCE_TO_MACRO["reddit"],
    Vertical.VIDEOS: SOURCE_TO_MACRO["youtube"],
}


def normalize_source_name(source: str) -> str:
    """Reduce a requested source to a lookup key.

    ``"Reddit"``, ``"@reddit"``, ``"reddit_search"`` and ``"@reddit_search"``
    all reduce to ``"reddit"``.
    """
    name = source.strip().lstrip("@").lower()
    if name.endswith("_search"):
        name = name[: -len("_search")]
    return name
