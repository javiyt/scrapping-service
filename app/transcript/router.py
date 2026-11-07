"""Capability-aware transcript provider routing.

This module decides *which registered* :class:`~app.transcript.
TranscriptProvider` serves a normalized :class:`~app.transcript.models.
TranscriptRequest` and *what happens when a provider fails*.  The routing
algorithm is generic: providers are selected by registration, configured
priority, health and failure classification.  There is intentionally no
provider-specific conditional (no ``if provider == "camofox"`` anywhere in
this module).

Routing modes
=============

The request's ``provider`` field selects the mode (see
:class:`TranscriptRouterConfig`):

``provider = auto`` (or a configured ``default_provider``)
    1. Candidates are taken in priority order: configured
       ``provider_order`` (restricted to registered providers), then the
       remaining registered providers in registration order.
    2. Each candidate is probed with ``health()`` (bounded by
       ``probe_timeout_seconds``); a probe that raises, times out, or
       reports ``available=False`` marks the candidate *unhealthy* — it is
       skipped, never fatal.
    3. The first healthy candidate is selected.
    4. If the selected provider fails with a *retryable* error (see below),
       the router falls back to the next healthy candidate, in order.
    5. If no provider is eligible, or every eligible provider fails
       retryably, :class:`~app.core.errors.NoTranscriptProviderAvailableError`
       is raised.

``provider = <name>`` (explicit)
    The named provider is looked up in the registry and validated: it must
    exist (be registered/enabled) and be healthy (a violation raises
    ``validation_error`` / ``transcript_provider_unavailable``).  All
    failures of ``fetch()`` — retryable or definitive — are propagated
    unchanged: an explicit request never falls back silently.

Failure classification
======================

Fallback (auto mode only) is triggered exclusively by retryable,
provider-side failures:

============================================  =====================
Error type                                    Fallback?
============================================  =====================
``transcript_provider_unavailable``           yes (503)
``transcript_provider_timeout``               yes (504)
``transcript_provider_blocked``               yes (403, upstream IP block)
``transcript_provider_invalid_response``      yes (502, upstream-side)
``no_transcript_available``                   no — definitive (video)
``transcript_language_unavailable``           no — definitive (video)
``video_unavailable``                         no — definitive (video)
``no_transcript_provider_available``          no — terminal by definition
``validation_error``                          no — caller's request is wrong
any other exception                           no — never mask unknown errors
============================================  =====================

The definitive errors describe the *video*, not the provider: every other
provider would hit the same wall, so falling back would only burn time and
connections.

Response metadata
=================

The router records in :class:`~app.transcript.models.TranscriptMetadata`:

- ``provider`` — the provider that produced the response (set by the
  provider itself, per contract);
- ``providers_attempted`` — every provider whose ``fetch()`` was actually
  attempted, in order, failed ones included;
- ``fallback_occurred`` — ``True`` when a retryable failure forced a
  second attempt;
- ``warnings`` — one safe, redacted summary per failure/skip.  Details are
  scrubbed of credential-like material so metadata never leaks secrets.
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
    NoTranscriptProviderAvailableError,
    ScraperError,
    TranscriptProviderBlockedError,
    TranscriptProviderInvalidResponseError,
    TranscriptProviderTimeoutError,
    TranscriptProviderUnavailableError,
    ValidationError,
)
from app.metrics.prometheus import MetricsCollector, get_metrics
from app.transcript.contract import TranscriptHealth, TranscriptProvider
from app.transcript.models import TranscriptRequest, TranscriptResponse
from app.transcript.registry import TranscriptProviderRegistry

logger = logging.getLogger("scraper-api.transcript.router")

#: Sentinel provider value meaning "let the router decide".
AUTO_PROVIDER = "auto"

#: Error classes representing *retryable, provider-side* failures.  In auto
#: mode the router falls back to the next eligible provider when one of
#: these is raised.  Everything else (definitive video errors, validation,
#: unknown exceptions) is terminal and propagated to the caller unchanged.
FALLBACK_ELIGIBLE_ERRORS: tuple[type[ScraperError], ...] = (
    TranscriptProviderUnavailableError,
    TranscriptProviderTimeoutError,
    TranscriptProviderBlockedError,
    TranscriptProviderInvalidResponseError,
)

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
class TranscriptRouterConfig(BaseModel):
    """Routing policy parsed from the top-level ``transcript`` config block.

    Sibling of ``transcript.providers``::

        transcript:
          default_provider: auto      # or an explicit provider name
          provider_order:             # routing priority for auto mode
            - camofox
            - youtube-transcript-api
            - yt-dlp
          probe_timeout_seconds: 10   # bound for the health probe
          providers: ...

    ``default_provider`` selects the routing mode for requests whose
    ``provider`` is ``"auto"`` (the request default): ``auto`` runs the
    ordered fallback chain; any other value routes such requests as if the
    caller had explicitly requested that provider (strict semantics, no
    fallback).

    ``provider_order`` entries that are not registered are skipped (with a
    startup warning) so a config can name a future provider — e.g.
    ``yt-dlp`` — before that provider exists.

    Every value can be overridden with ``SCRAPER_TRANSCRIPT_*`` environment
    variables (``SCRAPER_TRANSCRIPT_PROVIDER_ORDER`` is comma-separated).
    """

    model_config = ConfigDict(extra="forbid")

    default_provider: str = Field(
        default="auto",
        min_length=1,
        max_length=100,
        description=(
            "Routing mode for requests with ``provider: auto``: ``auto`` "
            "(ordered fallback chain) or an explicit registered provider name."
        ),
    )
    provider_order: list[str] = Field(
        default_factory=list,
        description=(
            "Routing priority for auto mode (registered providers first). "
            "Default chain: camofox, youtube-transcript-api, yt-dlp."
        ),
    )
    probe_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=300,
        description=(
            "Bound for the health probe per candidate.  A probe that exceeds "
            "it is treated as an unhealthy provider and skipped."
        ),
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> TranscriptRouterConfig:
        """Build the router config from application settings.

        Priority (highest to lowest):
          1. ``SCRAPER_TRANSCRIPT_*`` environment variables
          2. The top-level ``transcript`` YAML block (``default_provider``,
             ``provider_order``, ``probe_timeout_seconds``)
          3. Hard-coded defaults
        """
        raw_transcript: dict[str, Any] | None = getattr(settings, "raw_yaml_transcript", None)
        block = raw_transcript if isinstance(raw_transcript, dict) else {}

        values: dict[str, Any] = {}
        for key in ("default_provider", "provider_order", "probe_timeout_seconds"):
            env_value = os.environ.get(f"SCRAPER_TRANSCRIPT_{key.upper()}")
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
class TranscriptRouter:
    """Routes normalized transcript requests to registered providers.

    The router holds a :class:`TranscriptProviderRegistry` and a
    :class:`TranscriptRouterConfig`; it owns no I/O of its own — all backend
    work is delegated to providers through the structural
    :class:`~app.transcript.TranscriptProvider` contract.

    The router records per-provider observability counters
    (``transcript_provider_attempts_total`` /
    ``transcript_provider_success_total`` / ``transcript_provider_error_total``
    on the injected :class:`~app.metrics.prometheus.MetricsCollector`,
    labelled only by the bounded, configured provider name) and a
    ``transcript_fallbacks_total`` counter for auto-mode fallbacks.
    """

    def __init__(
        self,
        registry: TranscriptProviderRegistry,
        config: TranscriptRouterConfig | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._registry = registry
        self._config = config or TranscriptRouterConfig()
        self._metrics = metrics if metrics is not None else get_metrics()

        default = self._config.default_provider
        if default != AUTO_PROVIDER and default not in registry:
            available = ", ".join(registry.names) or "(none)"
            raise ValueError(
                f"transcript.default_provider is '{default}' but no such provider is "
                f"configured; available providers: {available}"
            )
        for name in self._config.provider_order:
            if name not in registry:
                logger.warning(
                    "transcript.provider_order lists '%s', which is not registered; "
                    "ignoring it in routing",
                    name,
                )

    # ------------------------------------------------------------------ access
    @property
    def registry(self) -> TranscriptProviderRegistry:
        return self._registry

    @property
    def config(self) -> TranscriptRouterConfig:
        return self._config

    # ------------------------------------------------------------------- route
    async def route(self, request: TranscriptRequest) -> TranscriptResponse:
        """Route ``request`` to a provider and return the normalized response.

        Raises one of the transcript errors from :mod:`app.core.errors` when
        no provider can satisfy the request (see module docstring for the
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
        self, name: str, request: TranscriptRequest, start: float
    ) -> TranscriptResponse:
        """Serve a request from one explicitly named provider (no fallback)."""
        provider = self._registry.get(name)
        if provider is None:
            raise ValidationError(
                f"Provider '{name}' is not configured or is disabled",
                details={"provider": name, "available_providers": self._registry.names},
            )

        health = await self.probe_health(provider)
        if health is None or not health.available:
            reason = (
                health.reason if health is not None and health.reason else "health probe failed"
            )
            raise TranscriptProviderUnavailableError(
                f"Provider '{name}' is currently unavailable ({reason})",
                details={"provider": name, "reason": reason},
            )

        # All failures propagate unchanged: an explicit request never falls
        # back silently (definitive video errors included).
        self._metrics.inc("transcript_provider_attempts_total", provider=name)
        try:
            fetch_start = time.monotonic()
            response = await provider.fetch(request.video_id, request.languages)
        except Exception:
            self._metrics.inc("transcript_provider_error_total", provider=name)
            if hasattr(provider, "_plugin_name"):
                elapsed_ms = (time.monotonic() - fetch_start) * 1000
                self._metrics.observe_plugin_call("transcript", provider.name, elapsed_ms, ok=False)
            raise
        self._metrics.inc("transcript_provider_success_total", provider=name)
        if hasattr(provider, "_plugin_name"):
            elapsed_ms = (time.monotonic() - fetch_start) * 1000
            self._metrics.observe_plugin_call("transcript", provider.name, elapsed_ms, ok=True)

        # Providers may cache their response object and hand back the very
        # same instance on every call; stamp a clone so in-place metadata
        # updates never mutate the provider's cached object.
        response = response.clone()

        response.metadata.providers_attempted = [name]
        response.metadata.fallback_occurred = False
        response.metadata.elapsed_ms = _elapsed_ms(start)
        return response

    # ------------------------------------------------------------------- auto
    async def _route_auto(self, request: TranscriptRequest, start: float) -> TranscriptResponse:
        """Ordered fallback chain with deterministic retryable fallback."""
        candidates = self.ordered_candidates()
        if not candidates:
            raise NoTranscriptProviderAvailableError(
                "No transcript providers are configured",
                details={"reason": "empty registry"},
            )

        eligible: list[TranscriptProvider] = []
        skipped: list[dict[str, Any]] = []
        for provider in candidates:
            health = await self.probe_health(provider)
            if health is None:
                skipped.append({"provider": provider.name, "reason": "unavailable (probe failed)"})
                continue
            if not health.available:
                reason = health.reason or "unavailable"
                skipped.append({"provider": provider.name, "reason": reason})
                continue
            eligible.append(provider)

        if not eligible:
            raise NoTranscriptProviderAvailableError(
                "No configured transcript provider is available right now",
                details={"candidates": skipped},
            )

        warnings: list[str] = [
            f"transcript provider skipped: {entry['provider']} ({entry['reason']})"
            for entry in skipped
        ]

        attempts: list[str] = []
        failures: list[dict[str, Any]] = []
        response: TranscriptResponse | None = None
        for index, provider in enumerate(eligible):
            attempts.append(provider.name)
            self._metrics.inc("transcript_provider_attempts_total", provider=provider.name)
            try:
                fetch_start = time.monotonic()
                response = await provider.fetch(request.video_id, request.languages)
            except Exception as exc:
                self._metrics.inc("transcript_provider_error_total", provider=provider.name)
                if hasattr(provider, "_plugin_name"):
                    elapsed_ms = (time.monotonic() - fetch_start) * 1000
                    self._metrics.observe_plugin_call(
                        "transcript", provider.name, elapsed_ms, ok=False
                    )
                if not isinstance(exc, FALLBACK_ELIGIBLE_ERRORS):
                    # Definitive (video-side) or unknown failures are
                    # terminal; they never fall back.
                    raise
                failures.append(
                    {
                        "provider": provider.name,
                        "error": exc.error_type,
                        "message": redact_secrets(exc.message),
                    }
                )
                summary = (
                    f"transcript provider '{provider.name}' failed "
                    f"({exc.error_type}: {redact_secrets(exc.message)})"
                )
                if index + 1 < len(eligible):
                    summary += f"; falling back to '{eligible[index + 1].name}'"
                    self._metrics.inc("transcript_fallbacks_total")
                    logger.warning("Transcript routing fallback: %s", summary)
                else:
                    logger.error("Transcript routing: %s", summary)
                warnings.append(summary)
                continue
            self._metrics.inc("transcript_provider_success_total", provider=provider.name)
            if hasattr(provider, "_plugin_name"):
                elapsed_ms = (time.monotonic() - fetch_start) * 1000
                self._metrics.observe_plugin_call("transcript", provider.name, elapsed_ms, ok=True)
            # Stamp a clone (providers may return their cached instance).
            response = response.clone()
            break

        if response is None:
            raise NoTranscriptProviderAvailableError(
                "All eligible transcript providers failed with retryable errors",
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
        return response

    # -------------------------------------------------------------- internals
    def ordered_candidates(self) -> list[TranscriptProvider]:
        """Registry providers in routing priority order.

        Configured ``provider_order`` entries come first (in the configured
        order, restricted to registered providers); the remaining registered
        providers follow in registration order.
        """
        ordered: list[TranscriptProvider] = []
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

    async def probe_health(self, provider: TranscriptProvider) -> TranscriptHealth | None:
        """Best-effort current health for one candidate.

        Returns ``None`` when the probe raises any error (including a
        timeout beyond ``probe_timeout_seconds``); a probe failure is a
        health signal (skip the candidate), never a routing failure.
        """
        try:
            return await asyncio.wait_for(
                provider.health(),
                timeout=self._config.probe_timeout_seconds,
            )
        except Exception as exc:  # noqa: S110 — a probe must never crash routing
            logger.warning(
                "Health probe failed for provider '%s' (%s); treating as unavailable",
                provider.name,
                redact_secrets(str(exc)),
            )
            return None


def build_transcript_router(
    settings: Settings,
    metrics: MetricsCollector | None = None,
    camofox_client: Any | None = None,
    extra_providers: list[Any] | None = None,
) -> TranscriptRouter:
    """Build the transcript router from application settings.

    Constructs the enabled providers (via
    :func:`app.transcript.providers.build_transcript_providers`), registers
    them, applies the ``transcript`` routing configuration
    (``default_provider`` / ``provider_order`` / ``probe_timeout_seconds``)
    and returns a ready-to-route :class:`TranscriptRouter`.

    ``camofox_client`` is the application-wide shared Camofox client
    (``app.state.camofox_client``); passing it lets the Camofox transcript
    provider reuse it instead of opening a second HTTP client.  ``None``
    (Camofox disabled/unconfigured) yields a registered-but-unavailable
    provider.

    ``extra_providers`` is a list of additional providers to register after
    the built-in ones. These are typically provided by plugins. Duplicate
    names raise :class:`ValueError` (registration fails fast).

    ``metrics`` overrides the global metrics collector (used by tests); the
    router records per-provider attempt/success/error counters on it.

    Raises:
        ValueError: on configuration errors (duplicate provider names, a
            ``default_provider`` that is not registered, ...).
    """
    from app.transcript.providers import build_transcript_providers

    registry = TranscriptProviderRegistry(
        build_transcript_providers(settings, client=camofox_client)
    )
    if extra_providers:
        for provider in extra_providers:
            registry.register(provider)
    return TranscriptRouter(
        registry, TranscriptRouterConfig.from_settings(settings), metrics=metrics
    )
