"""Configuration for the interactive agent browser subsystem.

Values come from the flat ``agent_browser_*`` settings fields (which are
themselves overridable per authenticated profile via the ``agent_browser:``
config section).  The profile override mechanism already maps section keys to
``agent_browser_<key>`` fields, so no resolver changes are needed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.config import Settings

__all__ = ["AgentBrowserConfig"]


class AgentBrowserConfig(BaseModel):
    """Effective agent browser limits/policy for one request (one profile).

    Built from a :class:`app.core.config.Settings` instance, so a profile's
    ``agent_browser:`` overrides (merged into the settings) are automatically
    reflected here.
    """

    enabled: bool = False
    allow_evaluate: bool = False
    max_sessions: int = Field(default=5, ge=1)
    max_tabs_per_session: int = Field(default=3, ge=1)
    session_ttl_seconds: int = Field(default=1800, ge=60)
    cleanup_interval_seconds: int = Field(default=60, ge=5)
    operation_timeout_seconds: int = Field(default=30, ge=1, le=300)

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentBrowserConfig:
        return cls(
            enabled=settings.agent_browser_enabled,
            allow_evaluate=settings.agent_browser_allow_evaluate,
            max_sessions=settings.agent_browser_max_sessions,
            max_tabs_per_session=settings.agent_browser_max_tabs_per_session,
            session_ttl_seconds=settings.agent_browser_session_ttl_seconds,
            cleanup_interval_seconds=settings.agent_browser_cleanup_interval_seconds,
            operation_timeout_seconds=settings.agent_browser_operation_timeout_seconds,
        )
