"""Unit tests for the transcript provider router.

Exercises the routing policy through the structural
:class:`app.transcript.TranscriptProvider` protocol with fakes:

- explicit provider (unknown / unhealthy / success / no fallback on any failure)
- auto mode (priority order, unhealthy skip, retryable fallback, all-fail,
  definitive errors do not fall back, empty registry)
- health probes (timeout → treated as unavailable)
- metrics accounting
- secret redaction in warnings
- :meth:`TranscriptRouterConfig.from_settings` (env + YAML)
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.core.errors import (
    NoTranscriptAvailableError,
    NoTranscriptProviderAvailableError,
    TranscriptLanguageUnavailableError,
    TranscriptProviderBlockedError,
    TranscriptProviderInvalidResponseError,
    TranscriptProviderTimeoutError,
    TranscriptProviderUnavailableError,
    ValidationError,
    VideoUnavailableError,
)
from app.metrics.prometheus import MetricsCollector
from app.transcript.contract import TranscriptHealth
from app.transcript.models import (
    TranscriptMetadata,
    TranscriptRequest,
    TranscriptResponse,
    TranscriptSegment,
)
from app.transcript.registry import TranscriptProviderRegistry
from app.transcript.router import (
    TranscriptRouter,
    TranscriptRouterConfig,
    build_transcript_router,
    redact_secrets,
)

# --------------------------------------------------------------------- fakes


class FakeProvider:
    """In-memory provider satisfying the structural TranscriptProvider protocol."""

    def __init__(
        self,
        name: str = "fake",
        available: bool = True,
        reason: str | None = None,
        fetch_error: Exception | None = None,
        slow_health: bool = False,
        title: str | None = None,
    ) -> None:
        self._name = name
        self._available = available
        self._reason = reason
        self._fetch_error = fetch_error
        self._slow_health = slow_health
        self._title = title
        self.fetch_calls: list[tuple[str, list[str]]] = []
        self.health_calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def health(self) -> TranscriptHealth:
        self.health_calls += 1
        if self._slow_health:
            import asyncio

            await asyncio.sleep(30)
        return TranscriptHealth(available=self._available, reason=self._reason)

    async def fetch(self, video_id: str, languages: list[str]) -> TranscriptResponse:
        self.fetch_calls.append((video_id, list(languages)))
        if self._fetch_error is not None:
            raise self._fetch_error
        return TranscriptResponse.from_segments(
            video_id=video_id,
            segments=[
                TranscriptSegment(
                    start=0.0,
                    duration=1.0,
                    text=f"{self._name} transcript ({languages[0] if languages else '?'})",
                )
            ],
            provider=self._name,
            metadata=TranscriptMetadata(provider=self._name),
            language=languages[0] if languages else None,
            title=self._title,
        )


def _request(**overrides: Any) -> TranscriptRequest:
    base: dict[str, Any] = {"video_id": "dQw4w9WgXcQ", "languages": ["en"]}
    base.update(overrides)
    return TranscriptRequest(**base)


def _router(
    *providers: FakeProvider, config: TranscriptRouterConfig | None = None
) -> TranscriptRouter:
    return TranscriptRouter(
        TranscriptProviderRegistry(list(providers)), config or TranscriptRouterConfig()
    )


# ------------------------------------------------------------ explicit provider


class TestExplicitProvider:
    async def test_explicit_success(self) -> None:
        alpha = FakeProvider(name="alpha")
        beta = FakeProvider(name="beta")
        router = _router(alpha, beta)

        response = await router.route(_request(provider="alpha"))

        assert alpha.fetch_calls == [("dQw4w9WgXcQ", ["en"])]
        assert beta.fetch_calls == []
        assert response.provider == "alpha"
        assert response.metadata.providers_attempted == ["alpha"]
        assert response.metadata.fallback_occurred is False

    async def test_unknown_provider_validation_error(self) -> None:
        router = _router(FakeProvider(name="alpha"))

        with pytest.raises(ValidationError) as excinfo:
            await router.route(_request(provider="ghost"))

        error = excinfo.value
        assert "ghost" in error.message
        assert error.details["available_providers"] == ["alpha"]

    async def test_unhealthy_explicit_provider_503(self) -> None:
        alpha = FakeProvider(name="alpha", available=False, reason="down")
        beta = FakeProvider(name="beta")
        router = _router(alpha, beta)

        with pytest.raises(TranscriptProviderUnavailableError) as excinfo:
            await router.route(_request(provider="alpha"))

        assert excinfo.value.status_code == 503
        assert beta.fetch_calls == []

    async def test_explicit_retryable_failure_never_falls_back(self) -> None:
        alpha = FakeProvider(
            name="alpha", fetch_error=TranscriptProviderUnavailableError("alpha down")
        )
        beta = FakeProvider(name="beta")
        router = _router(alpha, beta)

        with pytest.raises(TranscriptProviderUnavailableError):
            await router.route(_request(provider="alpha"))

        assert beta.fetch_calls == []

    async def test_explicit_definitive_failure_propagates(self) -> None:
        alpha = FakeProvider(name="alpha", fetch_error=NoTranscriptAvailableError("no captions"))
        router = _router(alpha)

        with pytest.raises(NoTranscriptAvailableError):
            await router.route(_request(provider="alpha"))


# ------------------------------------------------------------------ auto mode


class TestAutoProvider:
    async def test_auto_uses_configured_priority(self) -> None:
        alpha = FakeProvider(name="alpha")
        beta = FakeProvider(name="beta")
        router = _router(
            alpha, beta, config=TranscriptRouterConfig(provider_order=["beta", "alpha"])
        )

        response = await router.route(_request())

        assert beta.fetch_calls and not alpha.fetch_calls
        assert response.metadata.providers_attempted == ["beta"]
        assert response.metadata.fallback_occurred is False

    async def test_auto_registration_order_without_config(self) -> None:
        alpha = FakeProvider(name="alpha")
        beta = FakeProvider(name="beta")
        router = _router(alpha, beta)

        await router.route(_request())
        assert alpha.fetch_calls and not beta.fetch_calls

    async def test_auto_skips_unhealthy(self) -> None:
        alpha = FakeProvider(name="alpha", available=False, reason="unreachable")
        beta = FakeProvider(name="beta")
        router = _router(
            alpha, beta, config=TranscriptRouterConfig(provider_order=["alpha", "beta"])
        )

        response = await router.route(_request())

        assert beta.fetch_calls and not alpha.fetch_calls
        assert any("alpha" in warning for warning in response.metadata.warnings)
        assert response.metadata.fallback_occurred is False

    async def test_auto_falls_back_on_retryable_failure(self) -> None:
        alpha = FakeProvider(
            name="alpha", fetch_error=TranscriptProviderUnavailableError("alpha flaked")
        )
        beta = FakeProvider(name="beta")
        router = _router(
            alpha, beta, config=TranscriptRouterConfig(provider_order=["alpha", "beta"])
        )

        response = await router.route(_request())

        assert response.provider == "beta"
        assert response.metadata.providers_attempted == ["alpha", "beta"]
        assert response.metadata.fallback_occurred is True
        assert any("alpha" in warning for warning in response.metadata.warnings)
        assert any("falling back to 'beta'" in warning for warning in response.metadata.warnings)

    @pytest.mark.parametrize(
        "error",
        [
            TranscriptProviderUnavailableError("down"),
            TranscriptProviderTimeoutError("slow"),
            TranscriptProviderBlockedError("IP blocked"),
            TranscriptProviderInvalidResponseError("garbage"),
        ],
    )
    async def test_all_retryable_errors_trigger_fallback(self, error: Exception) -> None:
        alpha = FakeProvider(name="alpha", fetch_error=error)
        beta = FakeProvider(name="beta")
        router = _router(
            alpha, beta, config=TranscriptRouterConfig(provider_order=["alpha", "beta"])
        )

        response = await router.route(_request())
        assert response.provider == "beta"
        assert response.metadata.fallback_occurred is True

    @pytest.mark.parametrize(
        "error",
        [
            NoTranscriptAvailableError("no captions"),
            TranscriptLanguageUnavailableError("no matching language"),
            VideoUnavailableError("private video"),
            ValidationError("bad request"),
        ],
    )
    async def test_definitive_errors_do_not_fall_back(self, error: Exception) -> None:
        alpha = FakeProvider(name="alpha", fetch_error=error)
        beta = FakeProvider(name="beta")
        router = _router(
            alpha, beta, config=TranscriptRouterConfig(provider_order=["alpha", "beta"])
        )

        with pytest.raises(type(error)):
            await router.route(_request())

        assert beta.fetch_calls == []

    async def test_unknown_exception_does_not_fall_back(self) -> None:
        alpha = FakeProvider(name="alpha", fetch_error=RuntimeError("boom"))
        beta = FakeProvider(name="beta")
        router = _router(
            alpha, beta, config=TranscriptRouterConfig(provider_order=["alpha", "beta"])
        )

        with pytest.raises(RuntimeError):
            await router.route(_request())

        assert beta.fetch_calls == []

    async def test_all_providers_fail_503(self) -> None:
        alpha = FakeProvider(name="alpha", fetch_error=TranscriptProviderUnavailableError("a down"))
        beta = FakeProvider(name="beta", fetch_error=TranscriptProviderTimeoutError("b slow"))
        router = _router(
            alpha, beta, config=TranscriptRouterConfig(provider_order=["alpha", "beta"])
        )

        with pytest.raises(NoTranscriptProviderAvailableError) as excinfo:
            await router.route(_request())

        assert excinfo.value.status_code == 503
        assert excinfo.value.details["providers_attempted"] == ["alpha", "beta"]

    async def test_all_unhealthy_503(self) -> None:
        alpha = FakeProvider(name="alpha", available=False, reason="unavailable")
        beta = FakeProvider(name="beta", available=False, reason="unavailable")
        router = _router(alpha, beta)

        with pytest.raises(NoTranscriptProviderAvailableError) as excinfo:
            await router.route(_request())

        assert alpha.fetch_calls == []
        assert beta.fetch_calls == []
        candidates = excinfo.value.details["candidates"]
        assert {entry["provider"] for entry in candidates} == {"alpha", "beta"}

    async def test_empty_registry_503(self) -> None:
        router = _router()

        with pytest.raises(NoTranscriptProviderAvailableError) as excinfo:
            await router.route(_request())

        assert excinfo.value.details["reason"] == "empty registry"

    async def test_default_provider_config_routes_strictly(self) -> None:
        alpha = FakeProvider(name="alpha", fetch_error=TranscriptProviderUnavailableError("down"))
        beta = FakeProvider(name="beta")
        router = _router(alpha, beta, config=TranscriptRouterConfig(default_provider="alpha"))

        # Request provider=auto → routes to the configured default strictly.
        with pytest.raises(TranscriptProviderUnavailableError):
            await router.route(_request())

        assert beta.fetch_calls == []

    async def test_default_provider_must_be_registered(self) -> None:
        with pytest.raises(ValueError, match="default_provider"):
            TranscriptRouter(
                TranscriptProviderRegistry([FakeProvider(name="alpha")]),
                TranscriptRouterConfig(default_provider="ghost"),
            )


# ----------------------------------------------------------------- health probe


class TestHealthProbes:
    async def test_slow_health_treated_as_unavailable(self) -> None:
        alpha = FakeProvider(name="alpha", slow_health=True)
        beta = FakeProvider(name="beta")
        router = _router(alpha, beta, config=TranscriptRouterConfig(probe_timeout_seconds=0.01))

        response = await router.route(_request())

        assert beta.fetch_calls and not alpha.fetch_calls
        assert any("alpha" in warning for warning in response.metadata.warnings)

    async def test_raising_health_treated_as_unavailable(self) -> None:
        class Exploding(FakeProvider):
            async def health(self) -> TranscriptHealth:
                raise RuntimeError("probe boom with Authorization: Bearer secret-token-123")

        alpha = Exploding(name="alpha")
        beta = FakeProvider(name="beta")
        router = _router(alpha, beta, config=TranscriptRouterConfig(probe_timeout_seconds=10))

        await router.route(_request())
        assert beta.fetch_calls and not alpha.fetch_calls


# ---------------------------------------------------------------------- metrics


class TestRouterMetrics:
    async def test_success_counts(self) -> None:
        metrics = MetricsCollector()
        router = _router(FakeProvider(name="alpha"), config=TranscriptRouterConfig())
        # Rebuild with the test metrics collector.
        router = TranscriptRouter(
            TranscriptProviderRegistry([FakeProvider(name="alpha")]),
            TranscriptRouterConfig(),
            metrics=metrics,
        )

        await router.route(_request())

        assert metrics.get("transcript_provider_attempts_total", provider="alpha") == 1
        assert metrics.get("transcript_provider_success_total", provider="alpha") == 1
        assert metrics.get("transcript_provider_error_total", provider="alpha") == 0
        assert metrics.get("transcript_fallbacks_total") == 0

    async def test_fallback_counts(self) -> None:
        metrics = MetricsCollector()
        alpha = FakeProvider(name="alpha", fetch_error=TranscriptProviderUnavailableError("down"))
        beta = FakeProvider(name="beta")
        router = TranscriptRouter(
            TranscriptProviderRegistry([alpha, beta]),
            TranscriptRouterConfig(provider_order=["alpha", "beta"]),
            metrics=metrics,
        )

        await router.route(_request())

        assert metrics.get("transcript_provider_attempts_total", provider="alpha") == 1
        assert metrics.get("transcript_provider_error_total", provider="alpha") == 1
        assert metrics.get("transcript_provider_attempts_total", provider="beta") == 1
        assert metrics.get("transcript_provider_success_total", provider="beta") == 1
        assert metrics.get("transcript_fallbacks_total") == 1


# ----------------------------------------------------------- secret redaction


class TestRedactSecrets:
    def test_url_userinfo(self) -> None:
        assert "user:pass@" not in redact_secrets("https://user:pass@example.com")

    def test_authorization_header(self) -> None:
        redacted = redact_secrets("request failed Authorization: Bearer sk-abc123")
        assert "sk-abc123" not in redacted
        assert "[REDACTED]" in redacted

    def test_bearer_token(self) -> None:
        redacted = redact_secrets("got 401 with Bearer abcdef123456")
        assert "abcdef123456" not in redacted

    def test_api_key_assignment(self) -> None:
        redacted = redact_secrets("api_key = super-secret-value")
        assert "super-secret-value" not in redacted

    def test_plain_text_untouched(self) -> None:
        assert redact_secrets("alpha failed (transcript_provider_unavailable)") is not None

    async def test_warning_messages_are_redacted(self) -> None:
        alpha = FakeProvider(
            name="alpha",
            fetch_error=TranscriptProviderUnavailableError("boom api_key = topsecret-value-123"),
        )
        beta = FakeProvider(name="beta")
        router = _router(
            alpha, beta, config=TranscriptRouterConfig(provider_order=["alpha", "beta"])
        )

        response = await router.route(_request())
        assert all("topsecret-value-123" not in warning for warning in response.metadata.warnings)


# ------------------------------------------------------------------- config


class TestRouterConfigFromSettings:
    def test_defaults(self) -> None:
        settings = Settings()
        _setattr(settings, "raw_yaml_transcript", None)
        config = TranscriptRouterConfig.from_settings(settings)
        assert config.default_provider == "auto"
        assert config.provider_order == []
        assert config.probe_timeout_seconds == 10.0

    def test_yaml_block(self) -> None:
        settings = Settings()
        _setattr(
            settings,
            "raw_yaml_transcript",
            {
                "default_provider": "camofox",
                "provider_order": ["camofox", "yt-dlp"],
                "probe_timeout_seconds": 5,
                "providers": {"camofox": {"enabled": False}},
            },
        )
        config = TranscriptRouterConfig.from_settings(settings)
        assert config.default_provider == "camofox"
        assert config.provider_order == ["camofox", "yt-dlp"]
        assert config.probe_timeout_seconds == 5.0

    def test_env_overrides_yaml(self, monkeypatch) -> None:
        settings = Settings()
        _setattr(
            settings,
            "raw_yaml_transcript",
            {"default_provider": "camofox", "provider_order": ["camofox"]},
        )
        monkeypatch.setenv("SCRAPER_TRANSCRIPT_DEFAULT_PROVIDER", "yt-dlp")
        monkeypatch.setenv("SCRAPER_TRANSCRIPT_PROVIDER_ORDER", "yt-dlp,youtube-transcript-api")

        config = TranscriptRouterConfig.from_settings(settings)
        assert config.default_provider == "yt-dlp"
        assert config.provider_order == ["yt-dlp", "youtube-transcript-api"]


def _setattr(settings: Settings, name: str, value: Any) -> None:
    """Set a private pydantic field (alias-backed) on a Settings instance."""
    object.__setattr__(settings, name, value)


# ---------------------------------------------------------------------- builder


class TestBuildTranscriptRouter:
    def test_empty_registry_with_empty_factory(self) -> None:
        settings = Settings()
        _setattr(settings, "raw_yaml_transcript", None)

        router = build_transcript_router(settings, metrics=MetricsCollector())

        assert router.registry.names == []

        async def _check() -> None:
            with pytest.raises(NoTranscriptProviderAvailableError):
                await router.route(_request())

        import asyncio

        asyncio.new_event_loop().run_until_complete(_check())

    def test_builder_rejects_unregistered_default(self) -> None:
        settings = Settings()
        _setattr(settings, "raw_yaml_transcript", {"default_provider": "ghost"})

        with pytest.raises(ValueError, match="default_provider"):
            build_transcript_router(settings, metrics=MetricsCollector())
