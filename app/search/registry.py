"""Registry of available :class:`SearchProvider` instances.

The registry is the single place where provider instances are held and
looked up by their stable ``name``.  Providers are registered in
construction order (driven by :func:`app.search.providers.
build_search_providers`); that registration order is the implicit routing
priority when no explicit ``provider_order`` is configured.

Registration is all-or-nothing: a provider with a duplicate or empty name
raises :class:`ValueError` (a configuration error the operator must fix at
startup, never a per-request surprise).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from app.search.contract import SearchProvider


class SearchProviderRegistry:
    """Ordered registry of search providers keyed by ``provider.name``."""

    def __init__(self, providers: Iterable[SearchProvider] = ()) -> None:
        self._providers: dict[str, SearchProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: SearchProvider) -> None:
        """Register ``provider`` under its ``name``.

        Raises:
            ValueError: if the name is empty or already registered.
        """
        name = provider.name
        if not name:
            raise ValueError("A provider name must not be empty")
        if name in self._providers:
            raise ValueError(f"Provider '{name}' is already registered")
        self._providers[name] = provider

    def get(self, name: str) -> SearchProvider | None:
        """Return the provider registered under ``name``, or ``None``."""
        return self._providers.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._providers

    def __iter__(self) -> Iterator[SearchProvider]:
        return iter(self._providers.values())

    def __len__(self) -> int:
        return len(self._providers)

    @property
    def names(self) -> list[str]:
        """Provider names in registration order."""
        return list(self._providers)

    def all(self) -> list[SearchProvider]:
        """All registered providers in registration order."""
        return list(self._providers.values())
