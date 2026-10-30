"""SearXNG search provider — first backend for the search contract.

- :class:`SearxngConfig` — validated provider configuration.
- :class:`SearxngProvider` — the :class:`SearchProvider` implementation.
- :mod:`mapping` — the only place where SearXNG category/engine names exist.
"""

from app.search.providers.searxng.config import SearxngConfig
from app.search.providers.searxng.provider import SearxngInstance, SearxngProvider

__all__ = ["SearxngConfig", "SearxngInstance", "SearxngProvider"]
