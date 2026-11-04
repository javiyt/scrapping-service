"""Registry of available :class:`TranscriptProvider` instances.

The registry is the single place where transcript provider instances are
held and looked up by their stable ``name``.  Providers are registered in
construction order (driven by :func:`app.transcript.providers.
build_transcript_providers`); that registration order is the implicit
routing priority when no explicit ``provider_order`` is configured.

Registration is all-or-nothing: a provider with a duplicate or empty name
raises :class:`ValueError` (a configuration error the operator must fix at
startup, never a per-request surprise).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from app.transcript.contract import TranscriptProvider


class TranscriptProviderRegistry:
    """Ordered registry of transcript providers keyed by ``provider.name``."""

    def __init__(self, providers: Iterable[TranscriptProvider] = ()) -> None:
        self._providers: dict[str, TranscriptProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: TranscriptProvider) -> None:
        """Register ``provider`` under its ``name``.

        Raises:
            ValueError: if the name is empty or already registered.
        """
        name = provider.name
        if not name or not name.strip():
            raise ValueError("A provider name must not be empty")
        if name in self._providers:
            raise ValueError(f"duplicate provider name: '{name}'")
        self._providers[name] = provider

    def get(self, name: str) -> TranscriptProvider | None:
        """Return the provider registered under ``name``, or ``None``."""
        return self._providers.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._providers

    def __iter__(self) -> Iterator[TranscriptProvider]:
        return iter(self._providers.values())

    def __len__(self) -> int:
        return len(self._providers)

    @property
    def names(self) -> list[str]:
        """Provider names in registration order."""
        return list(self._providers)

    def all(self) -> list[TranscriptProvider]:
        """All registered providers in registration order."""
        return list(self._providers.values())
