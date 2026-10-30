"""Provider-agnostic search domain.

Public contract:

- Domain models: ``SearchQuery``, ``SearchResult``, ``SearchMetadata``,
  ``SearchResponse``, ``SearchCapabilities``.
- Enums: ``Vertical``, ``TimeRange``, ``SafeSearch``.
- Provider contract: ``SearchProvider``.

Search-specific errors live in :mod:`app.core.errors`.
"""

from app.search.contract import SearchProvider
from app.search.models import (
    SafeSearch,
    SearchCapabilities,
    SearchMetadata,
    SearchQuery,
    SearchResponse,
    SearchResult,
    TimeRange,
    Vertical,
)

__all__ = [
    "SafeSearch",
    "SearchCapabilities",
    "SearchMetadata",
    "SearchProvider",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
    "TimeRange",
    "Vertical",
]
