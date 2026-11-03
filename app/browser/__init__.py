"""Provider-agnostic interactive agent browser API.

This package owns the *interactive* browser contract of the service — the
stateful, multi-step operations agents need (open a page, inspect it,
click, type, navigate, evaluate, close) in contrast to the stateless
scrape/search endpoints.

The public REST API (``/v1/browser/...``, see :mod:`app.api.browser_routes`)
uses scrapping-service-owned session and tab identifiers.  A
:class:`AgentBrowserProvider` implements the backend side of the contract —
Camofox is the first implementation (:class:`CamofoxAgentBrowserProvider`).
Upstream identifiers (Camofox ``userId`` / ``sessionKey`` / ``tabId``) are
mapped internally by the provider and never appear in the public API.

Security and policy live in :class:`BrowserService` (
:mod:`app.browser.service`), not in the provider:

* session ownership is scoped to the authenticated profile;
* navigation/opening is checked against the SSRF and domain rules of the
  request's effective settings;
* JavaScript evaluation is refused unless enabled for that profile;
* per-profile session/tab limits and the idle TTL are enforced by the
  service (and re-applied by the background maintenance loop).
"""

from app.browser.config import AgentBrowserConfig
from app.browser.providers import CamofoxAgentBrowserProvider
from app.browser.providers.base import AgentBrowserProvider, ProviderError
from app.browser.providers.factory import create_provider
from app.browser.service import AgentBrowserService

__all__ = [
    "AgentBrowserConfig",
    "AgentBrowserProvider",
    "AgentBrowserService",
    "CamofoxAgentBrowserProvider",
    "ProviderError",
    "create_provider",
]
