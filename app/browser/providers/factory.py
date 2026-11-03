"""Agent browser provider selection / factory.

Chooses the active :class:`AgentBrowserProvider` for the app.  Camofox is the
first (and only) interactive backend.  Botasaurus remains the stateless
*scrape* browser backend only (out of scope for interactive agent control,
per ticket #126).

The factory returns ``None`` when the agent browser is disabled or no provider
is ready; the service and routes treat that as "unavailable" (503) and the
lifespan skips the maintenance loop.
"""

from __future__ import annotations

from app.browser.providers.base import AgentBrowserProvider
from app.browser.providers.camofox import build_camofox_provider
from app.camofox.client import CamofoxClient
from app.core.config import Settings

__all__ = ["create_provider", "build_camofox_provider"]


def create_provider(
    settings: Settings, camofox_client: CamofoxClient | None = None
) -> AgentBrowserProvider | None:
    """Return the active agent browser provider, or ``None`` if unavailable.

    Selection order:
      1. The agent browser must be enabled (``agent_browser_enabled``).
      2. Camofox requires a ready client (a ``camofox:`` section in config, or
         an injected client for tests).

    Returns ``None`` in either case; callers report "unavailable" (503).
    """
    if not settings.agent_browser_enabled:
        return None
    return build_camofox_provider(camofox_client)
