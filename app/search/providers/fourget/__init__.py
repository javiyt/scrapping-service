"""4get search provider — privacy-oriented metasearch backend.

- :class:`FourgetConfig` — validated provider configuration
  (``pass_token`` is a secret: excluded from serialization, masked in repr).
- :class:`FourgetProvider` — the :class:`SearchProvider` implementation.
- :mod:`mapping` — the only place where 4get endpoint/parameter names live.
"""

from app.search.providers.fourget.config import FourgetConfig
from app.search.providers.fourget.provider import FourgetProvider

__all__ = ["FourgetConfig", "FourgetProvider"]
