"""Unit tests for the transcript result cache.

Uses a real :class:`SqliteCache` in a temp directory and fakes for the
router (the cache only talks to the router through ``route()``).  Covers:

- key normalization (same video / language set / casing → same key)
- cache hit / miss / force refresh / disabled
- stale-if-error (retryable failures vs. definitive errors vs. validation)
- storage failure degradation (reads → miss, writes → uncached)
- metadata stamping (from_cache / stale / cached_at / cache_age_seconds)
- config parsing (env + YAML)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import (
    NoTranscriptAvailableError,
    NoTranscriptProviderAvailableError,
    TranscriptLanguageUnavailableError,
    TranscriptProviderBlockedError,
    TranscriptProviderTimeoutError,
    TranscriptProviderUnavailableError,
    ValidationError,
    VideoUnavailableError,
)
from app.metrics.prometheus import MetricsCollector
from app.transcript.cache import (
    TranscriptCache,
    TranscriptCacheConfig,
    build_key_material,
    make_transcript_cache_key,
    normalize_languages_for_key,
)
from app.transcript.contract import TranscriptHealth
from app.transcript.models import (
    TranscriptMetadata,
    TranscriptRequest,
    TranscriptResponse,
    TranscriptSegment,
)
from app.transcript.registry import TranscriptProviderRegistry
from app.transcript.router import TranscriptRouter, TranscriptRouterConfig

# --------------------------------------------------------------------- fakes


class FakeProvider:
    """In-memory provider with a configurable fetch error / canned response."""

    def __init__(self, name: str = "fake") -> None:
        self._name = name
        self._available = True
        self._reason: str | None = None
        self._fetch_error: Exception | None = None
        self.response: TranscriptResponse | None = None
        self.fetch_calls: list[tuple[str, list[str]]] = []

    @property
    def name(self) -> str:
        return self._name

    async def health(self) -> TranscriptHealth:
        return TranscriptHealth(available=self._available, reason=self._reason)

    def set_unavailable(self, reason: str = "unavailable") -> None:
        """Flip the health probe to report unavailable (for stale tests)."""
        self._available = False
        self._reason = reason

    def set_available(self) -> None:
        self._available = True
        self._reason = None

    async def fetch(self, video_id: str, languages: list[str]) -> TranscriptResponse:
        self.fetch_calls.append((video_id, list(languages)))
        if self._fetch_error is not None:
            raise self._fetch_error
        if self.response is None:
            self.response = TranscriptResponse.from_segments(
                video_id=video_id,
                segments=[TranscriptSegment(start=0.0, duration=1.0, text=f"{self._name} text")],
                provider=self._name,
                metadata=TranscriptMetadata(provider=self._name),
                language=languages[0] if languages else None,
            )
        return self.response


def _request(**overrides: Any) -> TranscriptRequest:
    base: dict[str, Any] = {"video_id": "dQw4w9WgXcQ", "languages": ["en"]}
    base.update(overrides)
    return TranscriptRequest(**base)


def _router(*providers: FakeProvider) -> TranscriptRouter:
    return TranscriptRouter(TranscriptProviderRegistry(list(providers)), TranscriptRouterConfig())


def _canned(provider: FakeProvider, text: str = "cached text") -> None:
    """Give the provider a canned response (its error is reset to ``None``)."""
    provider._fetch_error = None
    provider.response = TranscriptResponse.from_segments(
        video_id="dQw4w9WgXcQ",
        segments=[TranscriptSegment(start=0.0, text=text)],
        provider=provider._name,
        metadata=TranscriptMetadata(provider=provider._name),
    )


@pytest.fixture()
def store(tmp_path) -> SqliteCache:
    s = SqliteCache(db_path=str(tmp_path / "transcript-test.db"))
    s.open()
    yield s
    s.close()


def _cache(
    store: SqliteCache,
    *providers: FakeProvider,
    config: TranscriptCacheConfig | None = None,
) -> TranscriptCache:
    return TranscriptCache(
        store=store,
        router=_router(*providers),
        config=config or TranscriptCacheConfig(),
        metrics=MetricsCollector(),
    )


# ------------------------------------------------------------------------ keys


class TestKeys:
    def test_same_video_same_langs_same_key(self) -> None:
        a = make_transcript_cache_key(_request())
        b = make_transcript_cache_key(_request())
        assert a == b
        assert len(a) == 64

    def test_language_order_and_casing_do_not_matter(self) -> None:
        a = make_transcript_cache_key(_request(languages=["en", "es"]))
        b = make_transcript_cache_key(_request(languages=["ES", "en"]))
        assert a == b

    def test_different_languages_different_key(self) -> None:
        a = make_transcript_cache_key(_request(languages=["en"]))
        b = make_transcript_cache_key(_request(languages=["de"]))
        assert a != b

    def test_different_provider_different_key(self) -> None:
        a = make_transcript_cache_key(_request(provider="auto"))
        b = make_transcript_cache_key(_request(provider="camofox"))
        assert a != b

    def test_different_video_different_key(self) -> None:
        a = make_transcript_cache_key(_request())
        b = make_transcript_cache_key(_request(video_id="aaaaaaaaaaa"))
        assert a != b

    def test_normalize_languages_for_key(self) -> None:
        assert normalize_languages_for_key(["EN", "es", "Fr"]) == ["en", "es", "fr"]

    def test_key_material_shape(self) -> None:
        material = build_key_material(_request(languages=["ES"]))
        assert material == {
            "v": 1,
            "video_id": "dQw4w9WgXcQ",
            "languages": ["es"],
            "provider": "auto",
        }


# ---------------------------------------------------------------------- hits/misses


class TestHitAndMiss:
    async def test_fresh_run_then_hit(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        cache = _cache(store, provider)

        first = await cache.fetch(_request())
        assert first.metadata.from_cache is False
        assert first.metadata.stale is False
        assert first.metadata.cached_at is None

        second = await cache.fetch(_request())
        assert second.metadata.from_cache is True
        assert second.metadata.stale is False
        assert second.metadata.cached_at is not None
        assert second.metadata.cache_age_seconds is not None
        assert provider.fetch_calls == [("dQw4w9WgXcQ", ["en"])]  # one live run only

    async def test_force_refresh_always_live(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        cache = _cache(store, provider)

        await cache.fetch(_request())
        fresh = await cache.fetch(_request(force_refresh=True))

        assert fresh.metadata.from_cache is False
        assert len(provider.fetch_calls) == 2

    async def test_disabled_cache_never_caches(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        cache = _cache(store, provider, config=TranscriptCacheConfig(enabled=False))

        await cache.fetch(_request())
        await cache.fetch(_request())
        assert len(provider.fetch_calls) == 2

    async def test_ttl_zero_entries_never_expire(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        cache = _cache(store, provider, config=TranscriptCacheConfig(default_ttl_seconds=0))

        await cache.fetch(_request())
        key = make_transcript_cache_key(_request())
        row = store.get_transcript_entry(key)
        assert row is not None
        assert row["expires_at"] is None

        second = await cache.fetch(_request())
        assert second.metadata.from_cache is True

    async def test_default_ttl_expires_entry(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        cache = _cache(store, provider, config=TranscriptCacheConfig(default_ttl_seconds=1))

        await cache.fetch(_request())
        key = make_transcript_cache_key(_request())
        row = store.get_transcript_entry(key)
        assert row is not None
        assert row["expires_at"] is not None

    async def test_dict_request_is_validated(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        cache = _cache(store, provider)

        response = await cache.fetch(
            {
                "video_id": "dQw4w9WgXcQ",
                "languages": ["en"],
                "url": "https://youtu.be/dQw4w9WgXcQ",
            }
        )
        assert response.video_id == "dQw4w9WgXcQ"

    async def test_invalid_dict_request_400(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        cache = _cache(store, provider)

        with pytest.raises(ValidationError):
            await cache.fetch({"video_id": "not-a-valid-id!!", "languages": []})

        assert provider.fetch_calls == []


# --------------------------------------------------------------- stale-if-error


def _expire_entry(cache: TranscriptCache, request: TranscriptRequest) -> None:
    """Back-date the stored entry for ``request`` until it is expired."""
    key = make_transcript_cache_key(request)
    row = cache._store.get_transcript_entry(key)
    assert row is not None
    cache._store.set_transcript_entry(
        cache_key=key,
        request=row["request"],
        response=row["response"],
        fetched_at=datetime.now(UTC) - timedelta(days=1),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        content_length=row["content_length"],
    )


class TestStaleIfError:
    @pytest.mark.parametrize(
        "error",
        [
            TranscriptProviderUnavailableError("down"),
            TranscriptProviderTimeoutError("slow"),
            TranscriptProviderBlockedError("blocked"),
            NoTranscriptProviderAvailableError(
                "all down", details={"providers_attempted": ["alpha"]}
            ),
        ],
    )
    async def test_stale_served_for_retryable_errors(
        self, store: SqliteCache, error: Exception
    ) -> None:
        provider = FakeProvider(name="alpha")
        _canned(provider)
        cache = _cache(store, provider)

        await cache.fetch(_request())
        # Take the provider down: unavailable health + retryable fetch error.
        provider.set_unavailable()
        provider._fetch_error = error
        _expire_entry(cache, _request())

        response = await cache.fetch(_request())

        assert response.metadata.from_cache is True
        assert response.metadata.stale is True
        assert any("stale" in warning for warning in response.metadata.warnings)

    @pytest.mark.parametrize(
        "error",
        [
            NoTranscriptAvailableError("no captions"),
            TranscriptLanguageUnavailableError("no matching language"),
            VideoUnavailableError("private"),
            NoTranscriptProviderAvailableError(
                "not configured", details={"reason": "empty registry"}
            ),
            ValidationError("bad request"),
            RuntimeError("boom"),
        ],
    )
    async def test_stale_never_served_for_definitive_or_invalid(
        self, store: SqliteCache, error: Exception
    ) -> None:
        provider = FakeProvider(name="alpha")
        _canned(provider)
        cache = _cache(store, provider)

        await cache.fetch(_request())
        provider._fetch_error = error
        _expire_entry(cache, _request())

        with pytest.raises(type(error)):
            await cache.fetch(_request())

    async def test_stale_disabled_by_config(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        _canned(provider)
        cache = _cache(store, provider, config=TranscriptCacheConfig(stale_if_error=False))

        await cache.fetch(_request())
        provider.set_unavailable()  # the only candidate is now skipped
        _expire_entry(cache, _request())

        # Health-probe skip -> NoTranscriptProviderAvailableError; with
        # stale_if_error=False the error propagates (no stale serve).
        with pytest.raises(NoTranscriptProviderAvailableError):
            await cache.fetch(_request())

    async def test_force_refresh_never_serves_stale(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        _canned(provider)
        cache = _cache(store, provider)

        await cache.fetch(_request())
        provider.set_unavailable()
        _expire_entry(cache, _request())

        # force_refresh skips the (expired) cache read entirely and the
        # provider is down -> error, never a stale serve.
        with pytest.raises(NoTranscriptProviderAvailableError):
            await cache.fetch(_request(force_refresh=True))


# ------------------------------------------------------------- storage failures


class TestStorageFailureDegradation:
    async def test_corrupt_entry_discarded(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        cache = _cache(store, provider)
        key = "k" * 64
        store.set_transcript_entry(
            cache_key=key,
            request="{}",
            response="{not-json",
            fetched_at=datetime.now(UTC),
            expires_at=None,
            content_length=9,
        )
        entry = cache._get(key)
        assert entry is None
        assert store.get_transcript_entry(key) is None  # discarded

    async def test_store_read_failure_is_a_miss(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        cache = _cache(store, provider)
        cache._store.get_transcript_entry = MagicMock(side_effect=RuntimeError("db broken"))

        response = await cache.fetch(_request())
        assert response is not None  # served live
        assert provider.fetch_calls

    async def test_store_write_failure_returns_uncached(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        cache = _cache(store, provider)
        cache._store.set_transcript_entry = MagicMock(side_effect=RuntimeError("db broken"))

        response = await cache.fetch(_request())
        assert response.metadata.from_cache is False
        assert len(provider.fetch_calls) == 1

    async def test_response_is_not_stamped_before_serving(self, store: SqliteCache) -> None:
        """The provider's response object must not be mutated in place.

        The router/cache stamp metadata in place; when the underlying
        provider returns the very same object for every request (a cached
        provider response), stamping must clone first so a previous serve
        (e.g. a stale stamp) never leaks into a later fresh serve.
        """
        provider = FakeProvider(name="alpha")
        _canned(provider)
        first = await TranscriptCache(
            store=store,
            router=_router(provider),
            config=TranscriptCacheConfig(stale_if_error=False),
            metrics=MetricsCollector(),
        ).fetch(_request())
        # Simulate what a stale serve would have stamped onto the cached object.
        first.metadata.stale = True

        second = await TranscriptCache(
            store=store,
            router=_router(provider),
            config=TranscriptCacheConfig(stale_if_error=False),
            metrics=MetricsCollector(),
        ).fetch(_request(force_refresh=True))

        assert second.metadata.stale is False
        assert first is not second
        assert first.metadata.stale is True  # the earlier stamp stayed on the earlier object

    async def test_store_failure_does_not_block_stale_serve(self, store: SqliteCache) -> None:
        provider = FakeProvider(name="alpha")
        _canned(provider)
        cache = _cache(store, provider)
        await cache.fetch(_request())
        provider._fetch_error = TranscriptProviderUnavailableError("down")
        _expire_entry(cache, _request())

        response = await cache.fetch(_request())
        assert response.metadata.stale is True


# ---------------------------------------------------------------------- config


def _setattr(settings: Settings, name: str, value: Any) -> None:
    object.__setattr__(settings, name, value)


class TestCacheConfigFromSettings:
    def test_defaults(self) -> None:
        settings = Settings()
        _setattr(settings, "raw_yaml_transcript", None)
        config = TranscriptCacheConfig.from_settings(settings)
        assert config.enabled is True
        assert config.default_ttl_seconds == 86400
        assert config.stale_if_error is True

    def test_yaml_block(self) -> None:
        settings = Settings()
        _setattr(
            settings,
            "raw_yaml_transcript",
            {"cache": {"enabled": False, "default_ttl_seconds": 600, "stale_if_error": False}},
        )
        config = TranscriptCacheConfig.from_settings(settings)
        assert config.enabled is False
        assert config.default_ttl_seconds == 600
        assert config.stale_if_error is False

    def test_env_overrides_yaml(self, monkeypatch) -> None:
        settings = Settings()
        _setattr(settings, "raw_yaml_transcript", {"cache": {"enabled": False}})
        monkeypatch.setenv("SCRAPER_TRANSCRIPT_CACHE_ENABLED", "true")

        config = TranscriptCacheConfig.from_settings(settings)
        assert config.enabled is True

    def test_invalid_block_rejected(self) -> None:
        settings = Settings()
        _setattr(settings, "raw_yaml_transcript", {"cache": "not-a-mapping"})
        with pytest.raises(ValueError):
            TranscriptCacheConfig.from_settings(settings)

    def test_unknown_key_rejected(self) -> None:
        settings = Settings()
        _setattr(settings, "raw_yaml_transcript", {"cache": {"bogus": 1}})
        config = TranscriptCacheConfig.from_settings(settings)
        # The bogus key is not part of the config contract (extra=forbid).
        assert not hasattr(config, "bogus")
        assert config.model_fields.keys() == {"enabled", "default_ttl_seconds", "stale_if_error"}
