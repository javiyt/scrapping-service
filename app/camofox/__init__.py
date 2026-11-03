"""Reusable Camofox browser REST client and session abstraction.

Camofox (``camofox-browser``) is an **external** long-running service; it is
never embedded in or launched by this application.  This package is the
single place that knows the Camofox REST contract:

* :class:`CamofoxClient` — typed async HTTP client.  All upstream JSON is
  parsed into the project-owned models in :mod:`app.camofox.types`, and all
  failures are mapped onto the project-owned error types in
  :mod:`app.core.errors` (see the client docstring for the mapping table).
  Secrets never reach logs, metrics, or exceptions.
* :class:`CamofoxSession` — a logical browser identity (unique ``userId`` +
  ``sessionKey``) with a tracked tab registry, so concurrent sessions can
  never share tabs.
* :func:`build_camofox_client` — startup factory; returns ``None`` when the
  ``camofox`` config section is absent/disabled.

Planned consumers: the Camofox search provider, the browser fallback, the
agent/browser API, and MCP tooling.
"""

from app.camofox.client import CamofoxClient
from app.camofox.config import CamofoxConfig
from app.camofox.session import CamofoxSession
from app.camofox.types import (
    CamofoxActionResult,
    CamofoxCookie,
    CamofoxEvaluateResult,
    CamofoxHealthStatus,
    CamofoxLinks,
    CamofoxNavigateResult,
    CamofoxSnapshot,
    CamofoxTab,
    CamofoxTabInfo,
)
from app.core.config import Settings

__all__ = [
    "CamofoxActionResult",
    "CamofoxCookie",
    "CamofoxClient",
    "CamofoxConfig",
    "CamofoxHealthStatus",
    "CamofoxLinks",
    "CamofoxEvaluateResult",
    "CamofoxNavigateResult",
    "CamofoxSession",
    "CamofoxSnapshot",
    "CamofoxTab",
    "CamofoxTabInfo",
    "build_camofox_client",
]


def build_camofox_client(settings: Settings) -> CamofoxClient | None:
    """Build the application-wide Camofox client from settings.

    Returns ``None`` when the ``camofox`` section is absent or disabled.
    The caller (application lifespan) stores it on ``app.state`` and closes
    it on shutdown.
    """
    config = CamofoxConfig.from_settings(settings)
    if not config.enabled:
        return None
    return CamofoxClient(config)
