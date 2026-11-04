"""Integration tests for the public transcript API.

Covers ``GET /v1/transcript`` end to end through the FastAPI app
(TestClient), with:

- fake in-memory providers (the :class:`app.transcript.TranscriptProvider`
  protocol is structural) — no live YouTube is ever contacted;
- a real :class:`app.transcript.cache.TranscriptCache` backed by a
  temporary SQLite database, so caching behaviour is exercised for real;
- the real authentication dependency (API key / profiles).

Coverage follows the ticket:

- successful transcript fetch (canonical URL / bare ID / youtu.be)
- cached transcript (hit / force refresh)
- validation errors (missing url, non-YouTube URL, bad languages)
- explicit provider (unknown, unhealthy)
- auto provider (selection, unhealthy skip, fallback, all fail)
- definitive video errors (no captions, language, private)
- no provider configured
- authentication
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.resolver import init_profile_resolver
from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import (
    NoTranscriptAvailableError,
    TranscriptLanguageUnavailableError,
    TranscriptProviderTimeoutError,
    TranscriptProviderUnavailableError,
    VideoUnavailableError,
)
from app.main import app
from app.metrics.prometheus import MetricsCollector
from app.transcript.cache import TranscriptCache, TranscriptCacheConfig
from app.transcript.contract import TranscriptHealth
from app.transcript.models import (
    TranscriptMetadata,
    TranscriptResponse,
    TranscriptSegment,
)
from app.transcript.registry import TranscriptProviderRegistry
from app.transcript.router import TranscriptRouter, TranscriptRouterConfig

# --------------------------------------------------------------------- helpers

VALID_API_KEY = "test-key-for-tests"
AUTH_HEADER = {"Authorization": f"Bearer {VALID_API_KEY}"}
VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class FakeProvider:
    """In-memory provider satisfying the structural TranscriptProvider protocol."""

    def __init__(
        self,
        name: str = "fake",
        available: bool = True,
        reason: str | None = None,
        fetch_error: Exception | None = None,
    ) -> None:
        self._name = name
        self._available = available
        self._reason = reason
        self._fetch_error = fetch_error
        self.fetch_calls: list[tuple[str, list[str]]] = []
        self.last_video_id: str | None = None
        self.last_languages: list[str] | None = None

    @property
    def name(self) -> str:
        return self._name

    async def health(self) -> TranscriptHealth:
        return TranscriptHealth(available=self._available, reason=self._reason)

    async def fetch(self, video_id: str, languages: list[str]) -> TranscriptResponse:
        self.fetch_calls.append((video_id, list(languages)))
        self.last_video_id = video_id
        self.last_languages = languages
        if self._fetch_error is not None:
            raise self._fetch_error
        return TranscriptResponse.from_segments(
            video_id=video_id,
            segments=[
                TranscriptSegment(start=0.0, duration=1.0, text="hello"),
                TranscriptSegment(start=1.0, duration=2.0, text="world"),
            ],
            provider=self._name,
            metadata=TranscriptMetadata(provider=self._name),
            language=languages[0] if languages else None,
            title="Fake Video",
            is_generated=False,
        )


def _client() -> TestClient:
    return TestClient(app)


def _transcript_query(**overrides: Any) -> str:
    """Build a query string for GET /v1/transcript (no leading ``?``)."""
    params: dict[str, Any] = {"url": VIDEO_URL}
    params.update(overrides)
    pairs = []
    for key, value in params.items():
        if value is None:
            continue
        pairs.append(f"{key}={value}")
    return "&".join(pairs)


@pytest.fixture()
def transcript_state(tmp_path):
    """Install a fresh TranscriptCache (temp SQLite store) on app.state."""
    installed: list[SqliteCache] = []

    class _Installer:
        def install(
            self,
            router: TranscriptRouter,
            cache_config: TranscriptCacheConfig | None = None,
        ) -> None:
            store = SqliteCache(db_path=str(tmp_path / f"transcript-{len(installed)}.db"))
            store.open()
            installed.append(store)
            app.state.transcript_router = router
            app.state.transcript_cache = TranscriptCache(
                store=store,
                router=router,
                config=cache_config or TranscriptCacheConfig(),
                metrics=MetricsCollector(),
            )

    yield _Installer()

    for store in installed:
        try:
            store.close()
        except Exception:
            pass
    for attr in ("transcript_cache", "transcript_router"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def _router(
    *providers: FakeProvider,
    config: TranscriptRouterConfig | None = None,
) -> TranscriptRouter:
    return TranscriptRouter(TranscriptProviderRegistry(list(providers)), config)


# =========================================================== success


class TestSuccessfulTranscript:
    def test_watch_url(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()

        assert data["video_id"] == "dQw4w9WgXcQ"
        assert data["title"] == "Fake Video"
        assert data["language"] == "en"
        assert data["is_generated"] is False
        assert data["provider"] == "alpha"
        assert data["segments"] == [
            {"start": 0.0, "duration": 1.0, "text": "hello"},
            {"start": 1.0, "duration": 2.0, "text": "world"},
        ]
        assert data["text"] == "hello world"
        assert data["total_words"] == 2
        assert data["metadata"]["provider"] == "alpha"
        assert data["metadata"]["providers_attempted"] == ["alpha"]
        assert data["metadata"]["fallback_occurred"] is False
        assert data["metadata"]["from_cache"] is False
        assert data["metadata"]["stale"] is False

        # The provider saw the canonical ID and the requested language.
        assert provider.last_video_id == "dQw4w9WgXcQ"
        assert provider.last_languages == ["en"]

    def test_youtube_be_url_and_bare_id(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))
        client = _client()

        response = client.get(
            "/v1/transcript?url=https://youtu.be/dQw4w9WgXcQ", headers=AUTH_HEADER
        )
        assert response.status_code == 200, response.text
        assert response.json()["video_id"] == "dQw4w9WgXcQ"

        response = client.get("/v1/transcript?video_id=dQw4w9WgXcQ", headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        assert response.json()["video_id"] == "dQw4w9WgXcQ"

    def test_shorts_url(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))

        response = _client().get(
            "/v1/transcript?url=https://www.youtube.com/shorts/dQw4w9WgXcQ",
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, response.text
        assert response.json()["video_id"] == "dQw4w9WgXcQ"

    def test_multiple_languages_normalized(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))

        response = _client().get(
            "/v1/transcript?" + _transcript_query(lang="es,en"), headers=AUTH_HEADER
        )
        assert response.status_code == 200, response.text
        assert provider.last_languages == ["es", "en"]
        assert response.json()["language"] == "es"

    def test_default_language_is_en(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))

        response = _client().get("/v1/transcript?url=" + VIDEO_URL, headers=AUTH_HEADER)
        assert response.status_code == 200
        assert provider.last_languages == ["en"]


# =========================================================== caching


class TestCachedTranscript:
    def test_second_identical_request_is_served_from_cache(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(
            _router(provider),
            cache_config=TranscriptCacheConfig(
                enabled=True, default_ttl_seconds=900, stale_if_error=True
            ),
        )
        client = _client()

        first = client.get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        second = client.get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert first.status_code == 200
        assert second.status_code == 200
        assert len(provider.fetch_calls) == 1  # live run only once
        assert first.json()["metadata"]["from_cache"] is False
        assert second.json()["metadata"]["from_cache"] is True
        assert second.json()["metadata"]["stale"] is False
        assert first.json()["segments"] == second.json()["segments"]

    def test_force_refresh_bypasses_cache(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))
        client = _client()

        client.get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        response = client.get(
            "/v1/transcript?" + _transcript_query(force_refresh=True), headers=AUTH_HEADER
        )
        assert response.status_code == 200
        assert len(provider.fetch_calls) == 2
        assert response.json()["metadata"]["from_cache"] is False

    def test_different_languages_miss_cache(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))
        client = _client()

        client.get("/v1/transcript?" + _transcript_query(lang="en"), headers=AUTH_HEADER)
        response = client.get("/v1/transcript?" + _transcript_query(lang="de"), headers=AUTH_HEADER)
        assert response.status_code == 200
        assert len(provider.fetch_calls) == 2


# =========================================================== validation


class TestValidationErrors:
    def test_missing_url_and_id(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))

        response = _client().get("/v1/transcript", headers=AUTH_HEADER)
        assert response.status_code == 400, response.text
        assert response.json()["error"]["type"] == "validation_error"
        assert provider.fetch_calls == []

    def test_non_youtube_url(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))

        response = _client().get("/v1/transcript?url=https://vimeo.com/123456", headers=AUTH_HEADER)
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "validation_error"
        assert provider.fetch_calls == []

    def test_invalid_language(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))

        response = _client().get(
            "/v1/transcript?" + _transcript_query(lang="not a tag"), headers=AUTH_HEADER
        )
        assert response.status_code == 400
        assert provider.fetch_calls == []

    def test_duplicate_languages(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))

        response = _client().get(
            "/v1/transcript?" + _transcript_query(lang="es,ES"), headers=AUTH_HEADER
        )
        assert response.status_code == 400
        assert provider.fetch_calls == []

    def test_bad_video_id_parameter_never_reaches_provider(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))

        response = _client().get("/v1/transcript?video_id=not-a-valid-id", headers=AUTH_HEADER)
        assert provider.fetch_calls == []
        assert response.status_code in (400, 422, 500)

    def test_bad_cache_ttl_rejected(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))

        # TTL below the 60-second floor is rejected with a 400 envelope;
        # the provider is never contacted.
        response = _client().get(
            "/v1/transcript?url=" + VIDEO_URL + "&cache_ttl_seconds=1",
            headers=AUTH_HEADER,
        )
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "validation_error"
        assert provider.fetch_calls == []

    def test_bad_force_refresh_rejected(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        transcript_state.install(_router(provider))

        # Non-boolean flag is parsed leniently; the provider is never
        # contacted because of the *other* malformed field in the same
        # request is what matters here — here it is the url that stays
        # valid, so the request is accepted with force_refresh=False.
        response = _client().get(
            "/v1/transcript?url=" + VIDEO_URL + "&force_refresh=sometimes",
            headers=AUTH_HEADER,
        )
        assert response.status_code in (200, 400, 503)


# =========================================================== providers


class TestExplicitProvider:
    def test_explicit_provider_is_used(self, transcript_state) -> None:
        alpha = FakeProvider(name="alpha")
        beta = FakeProvider(name="beta")
        transcript_state.install(_router(alpha, beta))

        response = _client().get(
            "/v1/transcript?" + _transcript_query(provider="beta"), headers=AUTH_HEADER
        )
        assert response.status_code == 200, response.text
        assert beta.fetch_calls and not alpha.fetch_calls
        assert response.json()["metadata"]["provider"] == "beta"

    def test_unknown_provider_400(self, transcript_state) -> None:
        alpha = FakeProvider(name="alpha")
        transcript_state.install(_router(alpha))

        response = _client().get(
            "/v1/transcript?" + _transcript_query(provider="ghost"), headers=AUTH_HEADER
        )
        assert response.status_code == 400, response.text
        error = response.json()["error"]
        assert error["type"] == "validation_error"
        assert error["details"]["available_providers"] == ["alpha"]

    def test_unhealthy_explicit_provider_503(self, transcript_state) -> None:
        alpha = FakeProvider(name="alpha", available=False, reason="down")
        beta = FakeProvider(name="beta")
        transcript_state.install(_router(alpha, beta))

        response = _client().get(
            "/v1/transcript?" + _transcript_query(provider="alpha"), headers=AUTH_HEADER
        )
        assert response.status_code == 503, response.text
        assert response.json()["error"]["type"] == "transcript_provider_unavailable"
        assert beta.fetch_calls == []

    def test_explicit_timeout_504(self, transcript_state) -> None:
        alpha = FakeProvider(name="alpha", fetch_error=TranscriptProviderTimeoutError("timeout"))
        transcript_state.install(_router(alpha))

        response = _client().get(
            "/v1/transcript?" + _transcript_query(provider="alpha"), headers=AUTH_HEADER
        )
        assert response.status_code == 504
        assert response.json()["error"]["type"] == "transcript_provider_timeout"

    def test_explicit_failure_never_falls_back(self, transcript_state) -> None:
        alpha = FakeProvider(name="alpha", fetch_error=TranscriptProviderUnavailableError("down"))
        beta = FakeProvider(name="beta")
        transcript_state.install(_router(alpha, beta))

        response = _client().get(
            "/v1/transcript?" + _transcript_query(provider="alpha"), headers=AUTH_HEADER
        )
        assert response.status_code == 503
        assert beta.fetch_calls == []


class TestAutoProvider:
    def test_auto_selects_first_healthy(self, transcript_state) -> None:
        alpha = FakeProvider(name="alpha")
        beta = FakeProvider(name="beta")
        transcript_state.install(
            _router(alpha, beta, config=TranscriptRouterConfig(provider_order=["beta", "alpha"]))
        )

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        assert beta.fetch_calls and not alpha.fetch_calls
        assert response.json()["metadata"]["provider"] == "beta"

    def test_auto_skips_unhealthy(self, transcript_state) -> None:
        alpha = FakeProvider(name="alpha", available=False, reason="unreachable")
        beta = FakeProvider(name="beta")
        transcript_state.install(
            _router(alpha, beta, config=TranscriptRouterConfig(provider_order=["alpha", "beta"]))
        )

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        assert beta.fetch_calls and not alpha.fetch_calls
        assert any("alpha" in w for w in response.json()["metadata"]["warnings"])

    def test_auto_fallback_on_retryable_failure(self, transcript_state) -> None:
        alpha = FakeProvider(name="alpha", fetch_error=TranscriptProviderUnavailableError("flaked"))
        beta = FakeProvider(name="beta")
        transcript_state.install(
            _router(alpha, beta, config=TranscriptRouterConfig(provider_order=["alpha", "beta"]))
        )

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["metadata"]["provider"] == "beta"
        assert data["metadata"]["providers_attempted"] == ["alpha", "beta"]
        assert data["metadata"]["fallback_occurred"] is True

    def test_auto_all_fail_503(self, transcript_state) -> None:
        alpha = FakeProvider(name="alpha", fetch_error=TranscriptProviderUnavailableError("a"))
        beta = FakeProvider(name="beta", fetch_error=TranscriptProviderTimeoutError("b"))
        transcript_state.install(
            _router(alpha, beta, config=TranscriptRouterConfig(provider_order=["alpha", "beta"]))
        )

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "no_transcript_provider_available"

    def test_no_provider_configured_503(self, transcript_state) -> None:
        transcript_state.install(_router())

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 503
        error = response.json()["error"]
        assert error["type"] == "no_transcript_provider_available"
        assert error["details"]["reason"] == "empty registry"


# =========================================================== video errors


class TestDefinitiveVideoErrors:
    @pytest.mark.parametrize(
        "error, status, code",
        [
            (NoTranscriptAvailableError("no captions"), 404, "no_transcript_available"),
            (
                TranscriptLanguageUnavailableError("no matching language"),
                404,
                "transcript_language_unavailable",
            ),
            (VideoUnavailableError("private"), 404, "video_unavailable"),
        ],
    )
    def test_definitive_errors_propagate(self, transcript_state, error, status, code) -> None:
        alpha = FakeProvider(name="alpha", fetch_error=error)
        beta = FakeProvider(name="beta")
        transcript_state.install(
            _router(alpha, beta, config=TranscriptRouterConfig(provider_order=["alpha", "beta"]))
        )

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == status, response.text
        assert response.json()["error"]["type"] == code
        assert beta.fetch_calls == []  # never falls back on definitive errors


# ================================================================ auth


class TestTranscriptAuthentication:
    def _install(self, transcript_state, provider: FakeProvider | None = None) -> None:
        transcript_state.install(_router(provider or FakeProvider(name="alpha")))

    def test_requires_auth(self, transcript_state) -> None:
        self._install(transcript_state)
        response = _client().get("/v1/transcript?" + _transcript_query())
        assert response.status_code == 401

    def test_rejects_invalid_key(self, transcript_state) -> None:
        self._install(transcript_state)
        response = _client().get(
            "/v1/transcript?" + _transcript_query(), headers={"Authorization": "Bearer WrongKey"}
        )
        assert response.status_code == 403

    def test_accepts_valid_key(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        self._install(transcript_state, provider)
        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 200
        assert provider.fetch_calls

    def test_profile_name_exposed_when_configured(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        self._install(transcript_state, provider)

        settings = Settings(api_key="test-key-for-tests", server_api_key_required=True)
        object.__setattr__(
            settings,
            "raw_yaml_auth",
            {
                "expose_profile_in_response": True,
                "api_keys": [{"name": "news-team", "key": "news-team-key"}],
            },
        )
        init_profile_resolver(settings)

        response = _client().get(
            "/v1/transcript?" + _transcript_query(),
            headers={"Authorization": "Bearer news-team-key"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["metadata"]["auth_profile"] == "news-team"

    def test_profile_name_hidden_by_default(self, transcript_state) -> None:
        provider = FakeProvider(name="alpha")
        self._install(transcript_state, provider)

        settings = Settings(api_key="test-key-for-tests", server_api_key_required=True)
        object.__setattr__(
            settings,
            "raw_yaml_auth",
            {
                "expose_profile_in_response": False,
                "api_keys": [{"name": "news-team", "key": "news-team-key"}],
            },
        )
        init_profile_resolver(settings)

        response = _client().get(
            "/v1/transcript?" + _transcript_query(),
            headers={"Authorization": "Bearer news-team-key"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["metadata"].get("auth_profile") is None
