"""The scrapping-service-owned interactive browser service.

This is the *single* place that enforces the security and resource policy for
interactive browsing, and the only component that knows both the
service-owned session/tab registry *and* the provider.

Responsibilities (the ticket's acceptance criteria map 1:1 here):

* **Ownership** — every session is owned by exactly one authenticated
  profile.  Lookups are keyed on ``(profile, session_id)``; a different
  profile requesting another profile's session ID gets a uniform 404
  (no cross-profile probing, no ID leakage).
* **Security** — every navigation/open is validated through the shared SSRF +
  domain rules (:func:`app.core.security.validate_url`) using the *request's*
  effective settings, so profile ``security:`` overrides apply.  JavaScript
  ``evaluate`` is refused unless enabled for that profile.
* **Resource limits** — per-profile active-session cap, per-session tab cap,
  and an idle TTL with a background maintenance loop that releases abandoned
  sessions upstream.
* **ID mapping** — the service generates its own ``session_id`` / ``tab_id``
  and maps them onto provider-internal IDs.  Upstream (Camofox) identifiers
  never appear in responses.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.browser.config import AgentBrowserConfig
from app.browser.models import (
    BrowserLink,
    BrowserProviderMetadata,
    BrowserSession,
    BrowserTab,
    SnapshotRequest,
    SnapshotResponse,
)
from app.browser.providers.base import AgentBrowserProvider, ProviderError
from app.core.config import Settings
from app.core.errors import (
    BrowserDisabledError,
    BrowserSessionError,
    BrowserSessionExpiredError,
    BrowserSessionLimitError,
    BrowserTabError,
    BrowserTabLimitError,
    EvaluateDisabledError,
    SecurityError,
    ValidationError,
)
from app.core.security import validate_url

if TYPE_CHECKING:
    from app.browser.providers.base import (
        ProviderActionResult,
        ProviderEvaluateResult,
        ProviderNavigateResult,
    )

__all__ = ["AgentBrowserService"]

logger = logging.getLogger("scraper-api.browser.service")

_UNKNOWN_PROFILE = "__anonymous__"


def _iso(ts: float) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


class _TrackedSession:
    """Internal mutable record for one service-owned session."""

    def __init__(
        self,
        session_id: str,
        profile: str,
        provider: AgentBrowserProvider,
        provider_session_id: str,
        config: AgentBrowserConfig,
        allow_evaluate: bool,
        security: dict[str, Any],
        now: float,
    ) -> None:
        self.session_id = session_id
        self.profile = profile
        self.provider = provider
        self.provider_session_id = provider_session_id
        self.config = config
        self.allow_evaluate = allow_evaluate
        # Security snapshot (SSRF + domain rules) for the owning profile.
        self.security = security
        self.created_at = now
        self.last_activity_at = now
        self.tabs: dict[str, _TrackedTab] = {}
        self._lock = asyncio.Lock()

    def _config(self) -> AgentBrowserConfig:
        """The (process-wide) limits/TTL config for this session."""
        return self.config

    def _allows_evaluate(self) -> bool:
        """Whether the profile that created this session allowed evaluate."""
        return self.allow_evaluate

    @property
    def expires_at(self) -> float:
        return self.last_activity_at + self._config().session_ttl_seconds

    def touch(self, now: float) -> None:
        self.last_activity_at = now


class _TrackedTab:
    """Internal mutable record for one service-owned tab."""

    def __init__(
        self,
        tab_id: str,
        provider_tab_id: str,
        url: str | None = None,
        title: str | None = None,
        now: float = 0.0,
    ) -> None:
        self.tab_id = tab_id
        self.provider_tab_id = provider_tab_id
        self.url = url
        self.title = title
        self.created_at = now


class AgentBrowserService:
    """Facade coordinating the provider with ownership, security, and limits."""

    def __init__(self, provider: AgentBrowserProvider, settings: Settings) -> None:
        if provider is None:
            raise ValueError("AgentBrowserService requires a provider")
        self.provider = provider
        self.settings = settings
        # Session records keep the *global* policy snapshot (limits/TTL are
        # process-wide resource constraints, not per-profile); per-request
        # operations use the profile's effective settings for security and the
        # evaluate policy via _effective_settings()/config_for().
        self._base_config = AgentBrowserConfig.from_settings(settings)
        self._base_security: dict[str, Any] = self._security_for(settings)
        self._sessions: dict[str, _TrackedSession] = {}
        self._lock = asyncio.Lock()
        self._maintenance_task: asyncio.Task | None = None
        self._maintenance_stop: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # Configuration / effective policy
    # ------------------------------------------------------------------

    def _effective_settings(self, profile: str | None) -> Settings:
        """Effective settings for *profile* (profile overrides merged in)."""
        if not profile:
            return self.settings
        try:
            from app.auth.resolver import get_profile_resolver

            return get_profile_resolver().effective_settings_for(profile)
        except Exception:  # noqa: BLE001 - fall back to global settings
            return self.settings

    def config_for(self, profile: str | None) -> AgentBrowserConfig:
        """Per-request policy (evaluate, security) for a profile.

        Limits and TTL come from the global (process-wide) config; the
        per-profile effective settings provide the evaluate policy.
        """
        effective = self._effective_settings(profile)
        return AgentBrowserConfig(
            enabled=self._base_config.enabled,
            allow_evaluate=effective.agent_browser_allow_evaluate,
            max_sessions=self._base_config.max_sessions,
            max_tabs_per_session=self._base_config.max_tabs_per_session,
            session_ttl_seconds=self._base_config.session_ttl_seconds,
            cleanup_interval_seconds=self._base_config.cleanup_interval_seconds,
            operation_timeout_seconds=self._base_config.operation_timeout_seconds,
        )

    def _security_for(self, settings: Settings) -> dict[str, Any]:
        """Security rules (SSRF + domain allowlist) for *settings*.

        The snapshot applied to a session is ``self._base_security`` (seeded
        from the service settings in ``__init__``).  Tests may override
        ``self._base_security`` directly to simulate a profile allowlist.
        """
        return {
            "block_private_ips": settings.security_block_private_ips,
            "block_localhost": settings.security_block_localhost,
            "allowed_domains": list(settings.security_allowed_domains),
        }

    def _security_snapshot(self) -> dict[str, Any]:
        return dict(self._base_security)

    # ------------------------------------------------------------------
    # Lifecycle / maintenance
    # ------------------------------------------------------------------

    async def start_maintenance(self) -> None:
        """Start the background idle-session sweeper (no-op if already running)."""
        if self._maintenance_task is not None:
            return
        self._maintenance_stop = asyncio.Event()
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def stop_maintenance(self) -> None:
        """Stop the sweeper and release all sessions upstream."""
        if self._maintenance_stop is not None:
            self._maintenance_stop.set()
        if self._maintenance_task is not None:
            try:
                await self._maintenance_task
            except Exception:  # noqa: BLE001
                pass
            self._maintenance_task = None
        for session in list(self._sessions.values()):
            await self._release_session(session)

    async def _maintenance_loop(self) -> None:
        assert self._maintenance_stop is not None
        stop = self._maintenance_stop
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.config_for(None).cleanup_interval_seconds
                )
            except TimeoutError:
                pass
            if stop.is_set():
                break
            await self.sweep_expired()

    async def sweep_expired(self) -> int:
        """Release sessions that exceeded their idle TTL. Returns the count."""
        now = time.time()
        async with self._lock:
            expired = [s for s in self._sessions.values() if now >= s.expires_at]
        for session in expired:
            await self._release_session(session)
        if expired:
            logger.info("Agent browser: released %d expired idle session(s)", len(expired))
        return len(expired)

    async def _release_session(self, session: _TrackedSession) -> None:
        """Close a session (and tabs) upstream and remove all local records."""
        async with session._lock:
            for tab in list(session.tabs.values()):
                try:
                    await session.provider.close_tab(
                        session.provider_session_id, tab.provider_tab_id
                    )
                except ProviderError:
                    pass
            try:
                await session.provider.close_session(session.provider_session_id)
            except ProviderError:
                pass
        async with self._lock:
            self._sessions.pop(session.session_id, None)

    # ------------------------------------------------------------------
    # Lookups (ownership + liveness)
    # ------------------------------------------------------------------

    def _owner(self, profile: str | None) -> str:
        return profile or _UNKNOWN_PROFILE

    def _get_session(self, profile: str | None, session_id: str) -> _TrackedSession:
        now = time.time()
        owner = self._owner(profile)
        session = self._sessions.get(session_id)
        # Uniform 404 for unknown IDs AND cross-profile access (no probing).
        if session is None or session.profile != owner:
            raise BrowserSessionError(
                "Browser session not found", details={"session_id": session_id}
            )
        if now >= session.expires_at:
            raise BrowserSessionExpiredError(
                "Browser session expired", details={"session_id": session_id}
            )
        return session

    def _get_tab(self, session: _TrackedSession, tab_id: str) -> _TrackedTab:
        tab = session.tabs.get(tab_id)
        if tab is None:
            raise BrowserTabError("Tab not found in session", details={"tab_id": tab_id})
        return tab

    def _validate_url(self, url: str, security: dict[str, Any]) -> None:
        valid, reason = validate_url(
            url,
            block_private_ips=security.get("block_private_ips", True),
            block_localhost=security.get("block_localhost", True),
            allowed_domains=security.get("allowed_domains") or None,
        )
        if not valid:
            raise SecurityError(reason or "URL rejected by security policy")

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:20]}"

    # ------------------------------------------------------------------
    # Session operations
    # ------------------------------------------------------------------

    async def create_session(self, profile: str | None) -> BrowserSession:
        if not self._base_config.enabled:
            raise BrowserDisabledError(
                "The interactive browser API is not enabled",
                details={"agent_browser_enabled": False},
            )
        owner = self._owner(profile)
        config = self._base_config
        now = time.time()

        async with self._lock:
            active = [
                s.session_id
                for s in self._sessions.values()
                if s.profile == owner and now < s.expires_at
            ]
            if len(active) >= config.max_sessions:
                raise BrowserSessionLimitError(
                    f"Session limit reached ({config.max_sessions} active sessions)",
                    details={"max_sessions": config.max_sessions, "active_sessions": len(active)},
                )

            try:
                provider_session = await self.provider.create_session()
            except ProviderError as exc:
                raise exc.to_public() from exc

            session = _TrackedSession(
                session_id=self._new_id("ses"),
                profile=owner,
                provider=self.provider,
                provider_session_id=provider_session.provider_session_id,
                config=config,
                allow_evaluate=config.allow_evaluate,
                security=self._security_snapshot(),
                now=now,
            )
            self._sessions[session.session_id] = session

        return self._session_view(session, now)

    async def get_session(self, profile: str | None, session_id: str) -> BrowserSession:
        session = self._get_session(profile, session_id)
        return self._session_view(session, time.time())

    async def delete_session(self, profile: str | None, session_id: str) -> str:
        session = self._get_session(profile, session_id)
        await self._release_session(session)
        return session_id

    # ------------------------------------------------------------------
    # Tab operations
    # ------------------------------------------------------------------

    async def create_tab(
        self,
        profile: str | None,
        session_id: str,
        url: str | None = None,
    ) -> BrowserTab:
        session = self._get_session(profile, session_id)
        config = session.config
        now = time.time()

        if url is not None:
            self._validate_url(url, session.security)

        async with session._lock:
            if len(session.tabs) >= config.max_tabs_per_session:
                raise BrowserTabLimitError(
                    f"Tab limit reached ({config.max_tabs_per_session} tabs per session)",
                    details={
                        "max_tabs_per_session": config.max_tabs_per_session,
                        "open_tabs": len(session.tabs),
                    },
                )
            try:
                provider_tab = await self.provider.create_tab(session.provider_session_id, url)
            except ProviderError as exc:
                raise exc.to_public() from exc

            tab = _TrackedTab(
                tab_id=self._new_id("tab"),
                provider_tab_id=provider_tab.provider_tab_id,
                url=provider_tab.url or url,
                title=provider_tab.title,
                now=now,
            )
            session.tabs[tab.tab_id] = tab
            session.touch(now)

        return BrowserTab(
            tab_id=tab.tab_id,
            url=tab.url,
            title=tab.title,
            created_at=_iso(now),
        )

    async def delete_tab(self, profile: str | None, session_id: str, tab_id: str) -> str:
        session = self._get_session(profile, session_id)
        tab = self._get_tab(session, tab_id)
        try:
            await self.provider.close_tab(session.provider_session_id, tab.provider_tab_id)
        except ProviderError as exc:
            public = exc.to_public()
            # A tab that is already gone upstream is the desired end state.
            if public.status_code not in (404,):
                raise public from exc
        async with session._lock:
            session.tabs.pop(tab.tab_id, None)
            session.touch(time.time())
        return tab_id

    # ------------------------------------------------------------------
    # Page data / actions
    # ------------------------------------------------------------------

    async def snapshot(
        self,
        profile: str | None,
        session_id: str,
        tab_id: str,
        request: SnapshotRequest | None = None,
    ) -> SnapshotResponse:
        session = self._get_session(profile, session_id)
        tab = self._get_tab(session, tab_id)
        now = time.time()
        request = request or SnapshotRequest()

        try:
            snap = await self.provider.snapshot(session.provider_session_id, tab.provider_tab_id)
        except ProviderError as exc:
            raise exc.to_public() from exc

        links: list[BrowserLink] = []
        if request.include_links:
            try:
                provider_links = await self.provider.get_links(
                    session.provider_session_id,
                    tab.provider_tab_id,
                    limit=request.links_limit,
                )
                links = [BrowserLink(url=item.url, text=item.text) for item in provider_links.links]
            except ProviderError:
                links = []  # optional; don't fail the snapshot over links

        if snap.url:
            tab.url = snap.url
        if snap.title:
            tab.title = snap.title
        session.touch(now)

        return SnapshotResponse(
            session_id=session_id,
            tab_id=tab_id,
            url=snap.url or tab.url,
            title=snap.title or tab.title,
            snapshot=snap.snapshot,
            refs_count=snap.refs_count,
            truncated=snap.truncated,
            has_more=snap.has_more,
            next_offset=snap.next_offset,
            links=links,
            provider=BrowserProviderMetadata(provider=self.provider.name),
        )

    async def navigate(
        self,
        profile: str | None,
        session_id: str,
        tab_id: str,
        url: str,
        timeout_seconds: float | None = None,
    ) -> ProviderNavigateResult:
        session = self._get_session(profile, session_id)
        tab = self._get_tab(session, tab_id)
        self._validate_url(url, session.security)
        try:
            result = await self.provider.navigate(
                session.provider_session_id,
                tab.provider_tab_id,
                url,
                timeout_seconds=timeout_seconds,
            )
        except ProviderError as exc:
            raise exc.to_public() from exc
        if result.url:
            tab.url = result.url
        if result.title:
            tab.title = result.title
        session.touch(time.time())
        return result

    async def click(
        self,
        profile: str | None,
        session_id: str,
        tab_id: str,
        ref: str | None = None,
        selector: str | None = None,
    ) -> ProviderActionResult:
        session = self._get_session(profile, session_id)
        tab = self._get_tab(session, tab_id)
        if ref is None and selector is None:
            raise ValidationError("click requires a ref or a selector")
        try:
            result = await self.provider.click(
                session.provider_session_id, tab.provider_tab_id, ref=ref, selector=selector
            )
        except ProviderError as exc:
            raise exc.to_public() from exc
        if result.url:
            tab.url = result.url
        session.touch(time.time())
        return result

    async def type_text(
        self,
        profile: str | None,
        session_id: str,
        tab_id: str,
        text: str,
        ref: str | None = None,
        selector: str | None = None,
    ) -> ProviderActionResult:
        session = self._get_session(profile, session_id)
        tab = self._get_tab(session, tab_id)
        try:
            result = await self.provider.type_text(
                session.provider_session_id, tab.provider_tab_id, text, ref=ref, selector=selector
            )
        except ProviderError as exc:
            raise exc.to_public() from exc
        if result.url:
            tab.url = result.url
        session.touch(time.time())
        return result

    async def evaluate(
        self,
        profile: str | None,
        session_id: str,
        tab_id: str,
        expression: str,
        timeout_ms: int | None = None,
    ) -> ProviderEvaluateResult:
        session = self._get_session(profile, session_id)
        tab = self._get_tab(session, tab_id)
        if not session._allows_evaluate():
            raise EvaluateDisabledError("JavaScript evaluation is not enabled for this profile")
        if self.provider.supports_evaluate is False:
            raise EvaluateDisabledError("The active provider does not support evaluation")
        try:
            result = await self.provider.evaluate(
                session.provider_session_id, tab.provider_tab_id, expression, timeout_ms=timeout_ms
            )
        except ProviderError as exc:
            raise exc.to_public() from exc
        session.touch(time.time())
        return result

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def _session_view(self, session: _TrackedSession, now: float) -> BrowserSession:
        tabs = [
            BrowserTab(tab_id=t.tab_id, url=t.url, title=t.title, created_at=_iso(t.created_at))
            for t in session.tabs.values()
        ]
        return BrowserSession(
            session_id=session.session_id,
            provider=self.provider.name,
            tabs=tabs,
            created_at=_iso(session.created_at),
            last_activity_at=_iso(session.last_activity_at),
            expires_at=_iso(session.expires_at),
        )
