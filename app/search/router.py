"""Capability-aware search provider routing.

This module decides *which registered* :class:`~app.search.SearchProvider`
serves a normalized :class:`~app.search.models.SearchQuery` and *what happens
when a provider fails*.  The routing algorithm is fully generic: providers
are selected by registration, configured priority, advertised capabilities,
health and failure classification.  There is intentionally no
provider-specific conditional (no ``if provider == "searxng"`` anywhere in
this module).

Routing modes
=============

The request's ``provider`` field selects the mode (see
:class:`SearchRouterConfig`):

``provider = auto`` (or a configured ``default_provider``)
    1. Candidates are taken in priority order: configured
       ``provider_order`` (restricted to registered providers), then the
       remaining registered providers in registration order.
    2. Each candidate is probed with ``capabilities()`` (bounded by
       ``probe_timeout_seconds``); a probe that raises, times out, or
       reports ``available=False`` marks the candidate *unhealthy* — it is
       skipped, never fatal.
    3. A candidate must also satisfy the request's *strict* capabilities:
       every requested feature whose absence changes search semantics
       (verticals, time range, language, safe search, paging, source
       selection) must be supported.  The first healthy provider able to
       satisfy the request is selected.
    4. If the selected provider fails with a *transient* error (see below),
       the router falls back to the next eligible provider, in order.
    5. If no provider is eligible, or every eligible provider fails
       transiently, :class:`~app.core.errors.NoProviderAvailableError` is
       raised.

``provider = <name>`` (explicit)
    The named provider is looked up in the registry and validated: it must
    exist (be registered/enabled), be healthy, and support the request's
    strict capabilities.  A violation raises a clear, deterministic error
    (``validation_error`` / ``search_provider_unavailable`` /
    ``unsupported_search_capability``).  A *transient* failure of the
    explicit provider's ``search()`` is propagated unchanged — an explicit
    request never falls back silently.

Failure classification
======================

Fallback (auto mode only) is triggered exclusively by transient,
provider-side failures:

======================================  =========================
Error type                              Fallback?
======================================  =========================
``search_provider_unavailable``         yes (503)
``search_provider_timeout``             yes (504)
``invalid_provider_response``           yes (502, upstream-side)
``validation_error``                    no — caller's request is wrong
``unsupported_search_capability``       no — deterministic mismatch
``no_provider_available``               no — terminal by definition
any other exception                     no — never mask unknown errors
======================================  =========================

Response metadata
=================

The router records in :class:`~app.search.models.SearchMetadata`:

- ``provider`` — the provider that produced the response (set by the
  provider itself, per contract);
- ``providers_attempted`` — every provider whose ``search()`` was actually
  attempted, in order, failed ones included;
- ``fallback_occurred`` — ``True`` when a transient failure forced a second
  attempt;
- ``warnings`` — one safe, redacted summary per failure/skip.  Details are
  scrubbed of credential-like material (URL user-info, ``token=...``,
  ``Bearer ...``, ...) so metadata never leaks secrets.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.core.errors import (
    InvalidProviderResponseError,
    NoProviderAvailableError,
    ScraperError,
    SearchProviderTimeoutError,
    SearchProviderUnavailableError,
    UnsupportedSearchCapabilityError,
    ValidationError,
)
from app.search.contract import SearchProvider
from app.search.models import SearchCapabilities, SearchQuery, SearchResponse
from app.search.registry import SearchProviderRegistry

logger = logging.getLogger("scraper-api.search.router")

#: Sentinel provider value meaning "let the router decide".
AUTO_PROVIDER = "auto"

#: Error classes representing *transient, provider-side* failures.  In auto
#: mode the router falls back to the next eligible provider when one of
#: these is raised.  Everything else is terminal and is propagated to the
#: caller unchanged.
FALLBACK_ELIGIBLE_ERRORS: tuple[type[ScraperError], ...] = (
    SearchProviderUnavailableError,
    SearchProviderTimeoutError,
    InvalidProviderResponseError,
)

#: Degradation tokens that may be *degraded* (reported through
#: ``SearchMetadata.degraded_capabilities`` / ``warnings``) instead of
#: rejecting the provider.  Every other token produced by
#: :meth:`SearchCapabilities.degradations` — ``vertical:*``, ``time_range``,
#: ``language``, ``safe_search``, ``paging``, ``sources`` — changes the
#: semantics of the results if silently dropped, so a provider that cannot
#: honor it is not selected for that request (in auto mode) or is rejected
#: with :class:`UnsupportedSearchCapabilityError` (explicit mode).
#:
#: ``limit`` is the only degradable token: it is an upper bound, not a
#: filter.  Requesting more results than a provider can return degrades to
#: "as many as the provider can" without changing what the results mean.
DEGRADABLE_CAPABILITY_TOKENS: frozenset[str] = frozenset({"limit"})

# Credential-like material that must never leak into response metadata.
_URL_USERINFO_PATTERN = re.compile(r"(?i)\b(https?://)([^/@\s]+)@")
# ``Authorization: <scheme> <token>`` — redact the whole value (up to two
# words so e.g. ``Bearer sk-...`` disappears entirely).
_AUTHORIZATION_PATTERN = re.compile(r"(?i)\b(authorization)\s*[:=]\s*\S+(?:\s+\S+)?")
# ``Bearer <token>`` in any position.
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer)\s+\S+")
# Key =/ : value pairs for common credential names.
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[-_ ]?key|access[-_ ]?key)\b"
    r"(\s*[:=]\s*)([^\s,;&'\"\\)]*)"
)


def redact_secrets(text: str) -> str:
    """Remove credential-like material from a failure message.

    Provider error messages may echo upstream URLs or configuration.  They
    are placed into response metadata in redacted form (never their
    ``details`` dict), so this scrub is the last line of defence against
    leaking secrets through response metadata.
    """
    text = _URL_USERINFO_PATTERN.sub(r"\1", text)
    text = _AUTHORIZATION_PATTERN.sub(lambda m: f"{m.group(1).lower()} [REDACTED]", text)
    text = _BEARER_PATTERN.sub(lambda m: f"{m.group(1).lower()} [REDACTED]", text)
    return _CREDENTIAL_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


# ====================================================================== config
class SearchRouterConfig(BaseModel):
    """Routing policy parsed from the top-level ``search`` config block.

    Sibling of ``search.providers``::

        search:
          default_provider: auto        # or an explicit provider name
          provider_order:               # routing priority for auto mode
            - searxng
            - camofox
          probe_timeout_seconds: 10     # bound for the capabilities/health probe
          providers: ...

    ``default_provider`` selects the routing mode for requests whose
    ``provider`` is ``"auto"`` (the request default): ``auto`` runs
    capability-aware routing; any other value routes such requests as if the
    caller had explicitly requested that provider (strict semantics, no
    fallback).

    ``provider_order`` entries that are not registered are skipped (with a
    startup warning) so a config can name a future provider — e.g.
    ``camofox`` — before that provider exists.

    Every value can be overridden with ``SCRAPER_SEARCH_*`` environment
    variables (``SCRAPER_SEARCH_PROVIDER_ORDER`` is comma-separated).
    """

    model_config = ConfigDict(extra="forbid")

    default_provider: str = Field(
        default="auto",
        min_length=1,
        max_length=100,
        description=(
            "Routing mode for requests with ``provider: auto``: ``auto`` "
            "(capability-aware routing) or an explicit registered provider name."
        ),
    )
    provider_order: list[str] = Field(
        default_factory=list,
        description="Routing priority for auto mode (registered providers first).",
    )
    probe_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=300,
        description=(
            "Bound for the capabilities/health probe per candidate.  A probe "
            "that exceeds it is treated as an unhealthy provider and skipped."
        ),
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> SearchRouterConfig:
        """Build the router config from application settings.

        Priority (highest to lowest):
          1. ``SCRAPER_SEARCH_*`` environment variables
          2. The top-level ``search`` YAML block (``default_provider``,
             ``provider_order``, ``probe_timeout_seconds``)
          3. Hard-coded defaults
        """
        raw_search: dict[str, Any] | None = getattr(settings, "raw_yaml_search", None)
        block = raw_search if isinstance(raw_search, dict) else {}

        values: dict[str, Any] = {}
        for key in ("default_provider", "provider_order", "probe_timeout_seconds"):
            env_value = os.environ.get(f"SCRAPER_SEARCH_{key.upper()}")
            yaml_value = block.get(key)
            if env_value is not None:
                values[key] = env_value
            elif yaml_value is not None:
                values[key] = yaml_value

        if isinstance(values.get("provider_order"), str):
            values["provider_order"] = [
                item.strip() for item in values["provider_order"].split(",") if item.strip()
            ]
        return cls(**values)


# ====================================================================== router
class SearchRouter:
    """Routes normalized search requests to registered providers.

    The router holds a :class:`SearchProviderRegistry` and a
    :class:`SearchRouterConfig`; it owns no I/O of its own — all backend
    work is delegated to providers through the structural
    :class:`~app.search.SearchProvider` contract.
    """

    def __init__(
        self,
        registry: SearchProviderRegistry,
        config: SearchRouterConfig | None = None,
    ) -> None:
        self._registry = registry
        self._config = config or SearchRouterConfig()

        default = self._config.default_provider
        if default != AUTO_PROVIDER and default not in registry:
            available = ", ".join(registry.names) or "(none)"
            raise ValueError(
                f"search.default_provider is '{default}' but no such provider is "
                f"configured; available providers: {available}"
            )
        for name in self._config.provider_order:
            if name not in registry:
                logger.warning(
                    "search.provider_order lists '%s', which is not registered; "
                    "ignoring it in routing",
                    name,
                )

    # ------------------------------------------------------------------ access
    @property
    def registry(self) -> SearchProviderRegistry:
        return self._registry

    @property
    def config(self) -> SearchRouterConfig:
        return self._config

    # ------------------------------------------------------------------- route
    async def route(self, request: SearchQuery) -> SearchResponse:
        """Route ``request`` to a provider and return the normalized response.

        Raises one of the search errors from :mod:`app.core.errors` when no
        provider can satisfy the request (see module docstring for the
        exact mapping).
        """
        start = time.monotonic()
        requested = request.provider.strip()
        if requested == AUTO_PROVIDER:
            mode = self._config.default_provider
        else:
            mode = requested
        if mode == AUTO_PROVIDER:
            return await self._route_auto(request, start)
        return await self._route_explicit(mode, request, start)

    # --------------------------------------------------------------- explicit
    async def _route_explicit(
        self, name: str, request: SearchQuery, start: float
    ) -> SearchResponse:
        """Serve a request from one explicitly named provider (no fallback)."""
        provider = self._registry.get(name)
        if provider is None:
            raise ValidationError(
                f"Provider '{name}' is not configured or is disabled",
                details={"provider": name, "available_providers": self._registry.names},
            )

        caps = await self._probe_capabilities(provider)
        if caps is None:
            raise SearchProviderUnavailableError(
                f"Provider '{name}' is currently unavailable (capabilities probe failed)",
                details={"provider": name},
            )
        if not caps.available:
            raise SearchProviderUnavailableError(
                f"Provider '{name}' reports itself unavailable",
                details={"provider": name},
            )

        missing = self._strict_degradations(caps, request)
        if missing:
            raise UnsupportedSearchCapabilityError(
                f"Provider '{name}' cannot satisfy the requested capabilities: "
                + ", ".join(missing),
                details={"provider": name, "missing_capabilities": missing},
            )

        # Transient failures propagate unchanged: an explicit request never
        # falls back silently.
        response = await provider.search(request)

        response.metadata.providers_attempted = [name]
        response.metadata.fallback_occurred = False
        response.metadata.elapsed_ms = _elapsed_ms(start)
        self._merge_degradations(response, caps, request)
        return response

    # ------------------------------------------------------------------- auto
    async def _route_auto(self, request: SearchQuery, start: float) -> SearchResponse:
        """Capability-aware routing with deterministic transient fallback."""
        candidates = self._ordered_candidates()
        if not candidates:
            raise NoProviderAvailableError(
                "No search providers are configured",
                details={"reason": "empty registry"},
            )

        eligible: list[tuple[SearchProvider, SearchCapabilities]] = []
        skipped: list[dict[str, Any]] = []
        for provider in candidates:
            caps = await self._probe_capabilities(provider)
            if caps is None:
                skipped.append({"provider": provider.name, "reason": "unavailable (probe failed)"})
                continue
            if not caps.available:
                skipped.append({"provider": provider.name, "reason": "unavailable"})
                continue
            missing = self._strict_degradations(caps, request)
            if missing:
                skipped.append(
                    {
                        "provider": provider.name,
                        "reason": "capability mismatch",
                        "missing_capabilities": missing,
                    }
                )
                continue
            eligible.append((provider, caps))

        if not eligible:
            raise NoProviderAvailableError(
                "No configured search provider can satisfy the request right now",
                details={"candidates": skipped},
            )

        warnings: list[str] = [
            f"search provider skipped: {entry['provider']} ({entry['reason']})" for entry in skipped
        ]

        attempts: list[str] = []
        failures: list[dict[str, Any]] = []
        response: SearchResponse | None = None
        selected_caps: SearchCapabilities | None = None
        for index, (provider, caps) in enumerate(eligible):
            attempts.append(provider.name)
            try:
                response = await provider.search(request)
            except FALLBACK_ELIGIBLE_ERRORS as exc:
                failures.append(
                    {
                        "provider": provider.name,
                        "error": exc.error_type,
                        "message": redact_secrets(exc.message),
                    }
                )
                summary = (
                    f"provider '{provider.name}' failed ({exc.error_type}: "
                    f"{redact_secrets(exc.message)})"
                )
                if index + 1 < len(eligible):
                    summary += f"; falling back to '{eligible[index + 1][0].name}'"
                    logger.warning("Search routing fallback: %s", summary)
                else:
                    logger.error("Search routing: %s", summary)
                warnings.append(summary)
                continue
            selected_caps = caps
            break

        if response is None or selected_caps is None:
            raise NoProviderAvailableError(
                "All eligible search providers failed with transient errors",
                details={
                    "providers_attempted": attempts,
                    "failures": failures,
                },
            )

        response.metadata.providers_attempted = attempts
        response.metadata.fallback_occurred = len(attempts) > 1
        response.metadata.elapsed_ms = _elapsed_ms(start)
        if warnings:
            response.metadata.warnings = [*warnings, *response.metadata.warnings]
        self._merge_degradations(response, selected_caps, request)
        return response

    # -------------------------------------------------------------- internals
    def _ordered_candidates(self) -> list[SearchProvider]:
        """Registry providers in routing priority order.

        Configured ``provider_order`` entries come first (in the configured
        order, restricted to registered providers); the remaining registered
        providers follow in registration order.
        """
        ordered: list[SearchProvider] = []
        seen: set[int] = set()
        for name in self._config.provider_order:
            provider = self._registry.get(name)
            if provider is not None and id(provider) not in seen:
                seen.add(id(provider))
                ordered.append(provider)
        for provider in self._registry.all():
            if id(provider) not in seen:
                seen.add(id(provider))
                ordered.append(provider)
        return ordered

    async def _probe_capabilities(self, provider: SearchProvider) -> SearchCapabilities | None:
        """Best-effort current capabilities/health for one candidate.

        Returns ``None`` when the probe raises any error (including a
        timeout beyond ``probe_timeout_seconds``); a probe failure is a
        health signal (skip the candidate), never a routing failure.
        """
        try:
            return await asyncio.wait_for(
                provider.capabilities(),
                timeout=self._config.probe_timeout_seconds,
            )
        except Exception as exc:  # noqa: S110 — a probe must never crash routing
            logger.warning(
                "Capabilities probe failed for provider '%s' (%s); treating as unavailable",
                provider.name,
                redact_secrets(str(exc)),
            )
            return None

    @staticmethod
    def _strict_degradations(caps: SearchCapabilities, request: SearchQuery) -> list[str]:
        """Degradation tokens that disqualify a provider for ``request``.

        Strict tokens (semantic filters) reject; degradable tokens (currently
        only ``limit``) do not (see :data:`DEGRADABLE_CAPABILITY_TOKENS`).
        """
        return [
            token
            for token in caps.degradations(request)
            if token not in DEGRADABLE_CAPABILITY_TOKENS
        ]

    @staticmethod
    def _merge_degradations(
        response: SearchResponse, caps: SearchCapabilities, request: SearchQuery
    ) -> None:
        """Guarantee the negotiated degradation tokens are present in metadata.

        The provider is the authority for what *actually* happened during
        ``search()``; the probe-time capabilities are a lower bound on what
        the provider admitted it cannot honor.  Both are merged (deduplicated,
        provider tokens first) so capability mismatches are predictable.
        """
        metadata = response.metadata
        for token in caps.degradations(request):
            if token not in metadata.degraded_capabilities:
                metadata.degraded_capabilities.append(token)
