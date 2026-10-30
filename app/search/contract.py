"""Provider-agnostic search provider contract.

A :class:`SearchProvider` adapts a concrete search backend (an aggregator
service, a direct search API, ...) to the stable domain model defined in
:mod:`app.search.models`.  Clients and the service only ever interact with
the domain types, never with provider-specific request/response objects.

Implementations must be simple to fake in unit tests: the protocol is
structural, so any object with a ``name`` property plus ``capabilities()``
and ``search()`` methods satisfies it.
"""

from typing import Protocol, runtime_checkable

from app.search.models import SearchCapabilities, SearchQuery, SearchResponse


@runtime_checkable
class SearchProvider(Protocol):
    """Structural contract every search provider must satisfy."""

    @property
    def name(self) -> str:
        """Stable, unique provider identifier (e.g. ``"example"``)."""
        ...

    async def capabilities(self) -> SearchCapabilities:
        """Return this provider's current :class:`SearchCapabilities`.

        Implementations may cache or health-check, but the result must
        reflect what the provider can do *now* (including ``available``).
        """
        ...

    async def search(self, request: SearchQuery) -> SearchResponse:
        """Execute ``request`` and return a normalized domain response.

        Implementations must:

        - Set ``metadata.provider`` to their own ``name``.
        - Map the backend payload into :class:`SearchResult` fields; raw
          backend dictionaries must never be exposed.
        - Populate ``metadata.degraded_capabilities`` / ``warnings`` for any
          part of the request they could not honor (see
          :meth:`SearchCapabilities.degradations`).
        - Raise one of the search errors from :mod:`app.core.errors` on
          failure (unavailable, timeout, ...) instead of returning broken
          results.
        """
        ...
