"""Agent browser provider implementations and provider-agnostic contract.

Camofox is the first (and only) interactive backend.  Botasaurus remains the
stateless *scrape* browser backend only (out of scope for interactive agent
control, per ticket #126).

The contract types in :mod:`app.browser.providers.base` define the
provider-agnostic surface.  Concrete providers map service-level session/tab
handles onto their upstream identifiers.
"""

from app.browser.providers.base import (
    AgentBrowserProvider,
    ProviderActionResult,
    ProviderError,
    ProviderEvaluateResult,
    ProviderHealth,
    ProviderLink,
    ProviderLinks,
    ProviderNavigateResult,
    ProviderSession,
    ProviderSnapshot,
    ProviderTab,
)
from app.browser.providers.camofox import CamofoxAgentBrowserProvider

__all__ = [
    "AgentBrowserProvider",
    "ProviderError",
    "CamofoxAgentBrowserProvider",
    "ProviderActionResult",
    "ProviderEvaluateResult",
    "ProviderHealth",
    "ProviderLinks",
    "ProviderLink",
    "ProviderNavigateResult",
    "ProviderSession",
    "ProviderSnapshot",
    "ProviderTab",
]
