"""Extractor registry for plugins.

Plugins may register per-domain extractors that run when a scrape request
does not include an explicit ``extract`` block. The extractor registry
is consulted during response processing (in ``process_scrape_response``
or equivalent) and applied before the per-request extract block (if any).

The registry is built at startup by ``load_plugins`` and stored on
``app.state.extractor_registry``. It is consulted by ``app/plugins/loader``
when no per-request extraction is configured.

The registry supports:

- Exact domain matches.
- Subdomain matches (e.g., ``example.com`` matches ``sub.example.com``).
- Fallback to wildcard ``"*"`` if no specific domain matches.
- Multiple extractors per domain (sorted by ``priority`` desc, then
  registration order).
- Two extractor types: ``"css"`` (uses the built-in
  ``app.scraper.extractor.extract``) and ``"custom"`` (arbitrary callable
  with ``extract(html: str, url: str) -> dict``).

The registry is additive only — once an extractor is registered, it
cannot be overwritten (a collision on domain+name raises an error).

"""

from __future__ import annotations

import fnmatch
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExtractionResult(BaseModel):
    """Definition of an extractor returned by a plugin.

    Plugins register extractors via :meth:`PluginRegistry.register_extractor`.
    The registry then holds a collection of these for runtime lookup.

    The ``extractor`` field holds either:

    - For type ``"css"``: a ``dict[str, dict]`` mapping field names to
      their selector configuration (same shape as ``extract`` blocks in
      requests). The registry runs ``app.scraper.extractor.extract`` internally.
    - For type ``"custom"``: a callable ``extract(html: str, url: str) -> dict``
      that performs the extraction.

    The ``domain`` field supports glob patterns via ``fnmatch``:

    - ``"example.com"`` matches only ``example.com``.
    - ``"*.example.com"`` matches any subdomain.
    - ``"*"`` matches every domain (fallback).

    The ``priority`` field controls order when multiple extractors apply
    to the same URL. Higher priorities are tried first. When priorities
    tie, registration order decides.

    The ``requires_full_page`` flag indicates whether the extractor needs
    the full HTML (true for most) or only the body content (false for
    lightweight transformations).

    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    domain: str = Field(..., description="Domain pattern (exact, glob, or '*').")
    name: str = Field(..., description="Unique name within the domain.")
    extractor_type: str = Field(
        default="css",
        pattern=r"^(css|custom)$",
        description='Extractor type: "css" or "custom".',
    )
    fields: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description='CSS selector field definitions (for type "css").',
    )
    extractor: Any | None = Field(
        default=None,
        description='Custom extractor callable (for type "custom").',
    )
    priority: int = Field(
        default=0,
        description="Higher priorities are tried first.",
    )
    requires_full_page: bool = Field(
        default=True,
        description="Whether the extractor needs the full HTML page.",
    )


class ExtractorRegistry:
    """Ordered registry of per-domain extractors.

    The registry holds extractors registered by plugins (and potentially
    built-in extractors in future versions). Clients consult the registry
    to find extractors applicable to a given URL.

    The lookup algorithm is:

    1. Parse the URL's domain.
    2. Find extractors whose ``domain`` matches the URL's domain (exact,
       subdomain via glob, or wildcard).
    3. Sort them by ``priority`` descending, then registration order.
    4. Return the list in that order.

    Multiple extractors may match a single URL; the caller decides how many
    to run (typically, run them in order until one succeeds or all are
    exhausted).

    """

    def __init__(self) -> None:
        """Create an empty extractor registry."""
        self._extractors: list[ExtractionResult] = []
        self._index: dict[tuple[str, str], ExtractionResult] = {}
        # _order counters for stable sort on registration order.
        self._order: int = 0

    def register(self, extractor: ExtractionResult) -> None:
        """Register an extractor.

        Args:
            extractor: An ExtractionResult instance.

        Raises:
            ValueError: if an extractor with the same domain and name
              is already registered.
        """
        key = (extractor.domain, extractor.name)
        if key in self._index:
            raise ValueError(
                f"Extractor with domain '{extractor.domain}' and name "
                f"'{extractor.name}' is already registered"
            )
        self._extractors.append(extractor)
        self._index[key] = extractor
        self._order += 1

    def get_for_domain(self, url: str) -> list[ExtractionResult]:
        """Return extractors applicable to the URL's domain.

        The lookup logic:

        1. Extract the domain from ``url``.
        2. Find all extractors whose ``domain`` field matches the domain
           via ``fnmatch`` (exact, glob, or wildcard).
        3. Sort by ``priority`` descending, then registration order.

        Args:
            url: The URL to look up extractors for.

        Returns:
            A list of matching extractors, sorted by priority desc then
            registration order. Empty if no extractors match.
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path.split("/")[0]

        # Match all extractors whose domain pattern matches the URL's domain
        matches = [
            ex
            for ex in self._extractors
            if fnmatch.fnmatch(domain, ex.domain) or fnmatch.fnmatch(domain, f"*.{ex.domain}")
        ]

        # Sort by priority descending, then by registration order (lower = earlier)
        matches.sort(key=lambda ex: (-ex.priority, self._extractors.index(ex)))
        return matches

    def get_all(self) -> list[ExtractionResult]:
        """Return all registered extractors in registration order."""
        return list(self._extractors)

    def __len__(self) -> int:
        return len(self._extractors)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, tuple) or len(key) != 2:
            return False
        domain, name = key
        return (domain, name) in self._index
