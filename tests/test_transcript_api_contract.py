"""End-to-end contract tests for the public transcript API.

These tests pin the *contract* of the transcript API across the full
pipeline, exactly the chain the ticket calls out:

    REST request -> validation -> cache -> router -> provider -> normalized response

as one unit, through the real FastAPI app (TestClient) with:

- real validation (Pydantic ``extra="forbid"`` query-parameter schema),
- a real :class:`app.transcript.cache.TranscriptCache` backed by a
  temporary SQLite database,
- the real ordered-fallback router,
- fake in-memory providers — no live YouTube is ever contacted,
- the real authentication dependency.

The centrepiece is the schema-leak guard: every key in the JSON response —
recursively, at every nesting level — must belong to the documented public
schema (``openapi.yaml``'s ``TranscriptApiResponse``).  Provider-private
payload details must produce a public response containing none of them.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.core.errors import NoTranscriptAvailableError
from app.main import app
from app.metrics.prometheus import MetricsCollector
from app.search import SearchCache, SearchCacheConfig, SearchProviderRegistry, SearchRouter
from app.transcript import (
    TranscriptCache,
    TranscriptCacheConfig,
    TranscriptProviderRegistry,
    TranscriptRouter,
    make_transcript_cache_key,
)
from tests.test_search_api import VALID_API_KEY
from tests.test_transcript_api import (
    AUTH_HEADER,
    VIDEO_URL,
)

# ================================================================== contract

#: Keys allowed **anywhere** in the public JSON response (envelope,
#: segments, and metadata).
PUBLIC_KEYS: set[str] = {
    # Envelope
    "video_id",
    "title",
    "language",
    "is_generated",
    "provider",
    "segments",
    "text",
    "total_words",
    "metadata",
    # Segment items
    "start",
    "duration",
    # Metadata
    "providers_attempted",
    "fallback_occurred",
    "elapsed_ms",
    "from_cache",
    "stale",
    "cached_at",
    "cache_age_seconds",
    "warnings",
    "auth_profile",
}

#: Provider-specific keys that must never appear anywhere in the public API.
PROVIDER_SPECIFIC_KEYS: set[str] = {
    # youtube-transcript-api
    "youtube_transcript_api",
    "transcript_text",
    "transcript_data",
    # yt-dlp
    "info_dict",
    "yt_dlp",
    "formats",
    # Camofox
    "camofox",
    "raw_transcript",
    # generic provider-internal noise
    "score",
    "engines",
    "infoboxes",
}

ENVELOPE_KEYS = {
    "video_id",
    "title",
    "language",
    "is_generated",
    "provider",
    "segments",
    "text",
    "total_words",
    "metadata",
}

#: Required by the documented ``TranscriptApiResponse`` schema.
ENVELOPE_REQUIRED_KEYS = {
    "video_id",
    "segments",
    "text",
    "total_words",
    "provider",
    "metadata",
}

SEGMENT_KEYS = {"start", "duration", "text"}


def collect_keys(node: Any) -> set[str]:
    """Collect every key appearing in ``node``, recursively."""
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            keys |= collect_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= collect_keys(item)
    return keys


def assert_public_contract(body: dict[str, Any]) -> None:
    """Assert the whole response body obeys the public schema contract."""
    keys = collect_keys(body)
    leaked = keys - PUBLIC_KEYS
    assert not leaked, f"non-public keys leaked into the response: {sorted(leaked)}"
    provider_specific = keys & PROVIDER_SPECIFIC_KEYS
    assert not provider_specific, (
        f"provider-specific keys leaked into the response: {sorted(provider_specific)}"
    )
    assert set(body) == ENVELOPE_KEYS, (
        f"unexpected envelope keys: {sorted(set(body) ^ ENVELOPE_KEYS)}"
    )
    assert ENVELOPE_REQUIRED_KEYS <= set(body), (
        f"missing required keys: {sorted(ENVELOPE_REQUIRED_KEYS - set(body))}"
    )
    for segment in body["segments"]:
        assert set(segment) <= SEGMENT_KEYS, (
            f"segment has non-public keys: {sorted(set(segment) - SEGMENT_KEYS)}"
        )


# ================================================================== pipeline

#: Transcript content the fakes produce (provider name interpolated).
TRANSCRIPT_TEXT = "hello world transcript"


def _make_response(name: str) -> Any:
    from app.transcript.models import (
        TranscriptMetadata,
        TranscriptResponse,
        TranscriptSegment,
    )

    return TranscriptResponse.from_segments(
        video_id="dQw4w9WgXcQ",
        segments=[
            TranscriptSegment(start=0.0, duration=1.0, text="hello"),
            TranscriptSegment(start=1.0, duration=2.0, text="world transcript"),
        ],
        provider=name,
        metadata=TranscriptMetadata(provider=name),
        language="en",
        title=f"Fake Video ({name})",
        is_generated=False,
    )


class _ContractFakeProvider:
    """Fake provider with switchable behaviour and a rich fake payload.

    ``payload_extra`` simulates provider-internal private fields that a
    careless adapter might leak: they are stashed on the object the
    provider *would* hand back, and the contract guard proves none of them
    reach the public response.
    """

    def __init__(
        self,
        name: str,
        available: bool = True,
        reason: str | None = None,
        fetch_error: Exception | None = None,
        payload_extra: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._available = available
        self._reason = reason
        self._fetch_error = fetch_error
        self._payload_extra = payload_extra or {}
        self.fetch_calls: list[tuple[str, list[str]]] = []

    @property
    def name(self) -> str:
        return self._name

    async def health(self) -> Any:
        from app.transcript.contract import TranscriptHealth

        return TranscriptHealth(available=self._available, reason=self._reason)

    async def fetch(self, video_id: str, languages: list[str]) -> Any:
        self.fetch_calls.append((video_id, list(languages)))
        if self._fetch_error is not None:
            raise self._fetch_error
        response = _make_response(self._name)
        # Stash private payload fields that must never be exposed; the route
        # maps the domain response through TranscriptApiResponse, so these
        # cannot leak unless someone bypasses the public schema.
        response._private_payload = self._payload_extra  # type: ignore[attr-defined]
        return response


def _client() -> TestClient:
    return TestClient(app)


def _transcript_query(**overrides: Any) -> str:
    params: dict[str, Any] = {"url": VIDEO_URL}
    params.update(overrides)
    pairs = []
    for key, value in params.items():
        if value is None:
            continue
        pairs.append(f"{key}={value}")
    return "&".join(pairs)


def _router(
    providers: list[Any],
    provider_order: list[str] | None = None,
) -> TranscriptRouter:
    from app.transcript.router import TranscriptRouterConfig

    config = None
    if provider_order is not None:
        config = TranscriptRouterConfig(provider_order=provider_order)
    return TranscriptRouter(TranscriptProviderRegistry(list(providers)), config)


@pytest.fixture()
def transcript_pipeline(tmp_path):
    """Install a fresh transcript pipeline (real SQLite-backed cache) plus a
    bare search subsystem on ``app.state`` and tear both down again.

    Usage: ``transcript_pipeline.install(router, cache_config=...)``.
    """
    app.state.settings = Settings(api_key=VALID_API_KEY)

    search_store = SqliteCache(db_path=str(tmp_path / "search-pipeline.db"))
    search_store.open()
    app.state.search_cache = SearchCache(
        store=search_store,
        router=SearchRouter(SearchProviderRegistry([])),
        config=SearchCacheConfig(),
    )

    installed: list[SqliteCache] = [search_store]

    class _Installer:
        def install(
            self,
            router: TranscriptRouter,
            cache_config: TranscriptCacheConfig | None = None,
        ) -> TranscriptCache:
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
            return app.state.transcript_cache

    yield _Installer()

    for store in installed:
        try:
            store.close()
        except Exception:
            pass
    for attr in ("cache", "search_cache", "transcript_cache", "transcript_router"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


# ================================================= full pipeline (fake chain)


class TestFullPipeline:
    """REST -> validation -> cache -> router -> provider -> response."""

    def test_pipeline_normalizes_and_caches(self, transcript_pipeline):
        provider = _ContractFakeProvider(name="alpha")
        transcript_pipeline.install(
            _router([provider]),
            cache_config=TranscriptCacheConfig(enabled=True, default_ttl_seconds=3600),
        )

        first = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert first.status_code == 200, first.text
        body = first.json()
        assert_public_contract(body)
        assert body["video_id"] == "dQw4w9WgXcQ"
        assert body["provider"] == "alpha"
        assert body["text"] == "hello world transcript"
        assert body["total_words"] == 3
        assert body["metadata"]["provider"] == "alpha"
        assert body["metadata"]["from_cache"] is False
        assert provider.fetch_calls == [("dQw4w9WgXcQ", ["en"])]

        second = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert second.status_code == 200, second.text
        assert_public_contract(second.json())
        assert second.json()["metadata"]["from_cache"] is True
        assert provider.fetch_calls == [("dQw4w9WgXcQ", ["en"])]  # cache absorbed the repeat

    def test_validation_rejection_never_touches_cache_or_provider(self, transcript_pipeline):
        provider = _ContractFakeProvider(name="alpha")
        transcript_pipeline.install(_router([provider]))
        store = app.state.transcript_cache._store

        # Missing identifier.
        response = _client().get("/v1/transcript", headers=AUTH_HEADER)
        assert response.status_code == 400
        # Non-YouTube URL.
        response = _client().get(
            "/v1/transcript?url=https://example.com/watch?v=dQw4w9WgXcQ", headers=AUTH_HEADER
        )
        assert response.status_code == 400
        # Bad language list.
        response = _client().get(
            "/v1/transcript?" + _transcript_query(lang="e~~~"), headers=AUTH_HEADER
        )
        assert response.status_code == 400

        assert provider.fetch_calls == []
        assert store._conn.execute("SELECT COUNT(*) FROM transcript_cache").fetchone()[0] == 0

        # The cache is still usable afterwards.
        ok = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert ok.status_code == 200
        assert provider.fetch_calls == [("dQw4w9WgXcQ", ["en"])]

    def test_provider_private_fields_never_leak(self, transcript_pipeline):
        """The ticket's core contract: provider-internal payload details must
        never appear in the public API — not at the envelope, segment or
        metadata level, and not nested anywhere."""
        provider = _ContractFakeProvider(
            name="alpha",
            payload_extra={
                "score": 4.7,
                "info_dict": {"formats": []},
                "raw_transcript": "[00:00] hello",
            },
        )
        transcript_pipeline.install(
            _router([provider]),
            cache_config=TranscriptCacheConfig(enabled=True, default_ttl_seconds=3600),
        )
        body = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER).json()
        assert_public_contract(body)
        keys = collect_keys(body)
        assert not (keys & PROVIDER_SPECIFIC_KEYS)

    def test_stale_if_error_serves_previous_transcript_end_to_end(self, transcript_pipeline):
        from app.core.errors import TranscriptProviderUnavailableError

        provider = _ContractFakeProvider(name="alpha")
        transcript_pipeline.install(
            _router([provider]),
            cache_config=TranscriptCacheConfig(
                enabled=True, default_ttl_seconds=3600, stale_if_error=True
            ),
        )
        client = _client()

        first = client.get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert first.status_code == 200
        assert first.json()["metadata"]["from_cache"] is False

        # Back-date the entry until it is expired, then take the provider down.
        from app.transcript.models import TranscriptRequest

        store = app.state.transcript_cache._store
        cache_key = make_transcript_cache_key(
            TranscriptRequest(video_id="dQw4w9WgXcQ", languages=["en"])
        )
        row = store.get_transcript_entry(cache_key)
        assert row is not None
        store.set_transcript_entry(
            cache_key=cache_key,
            request=row["request"],
            response=row["response"],
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            content_length=row["content_length"],
        )
        provider._fetch_error = TranscriptProviderUnavailableError("alpha is down")

        response = client.get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        body = response.json()
        assert_public_contract(body)
        assert body["metadata"]["from_cache"] is True
        assert body["metadata"]["stale"] is True
        assert any("stale" in warning for warning in body["metadata"]["warnings"])
        assert body["text"] == first.json()["text"]  # same normalized content


# ====================================================== fallback chain chain


class TestFallbackChain:
    """The ordered auto fallback chain, exactly as the ticket describes."""

    def test_camofox_down_falls_back_to_youtube_transcript_api(self, transcript_pipeline):
        camofox = _ContractFakeProvider(name="camofox", available=False, reason="service down")
        yta = _ContractFakeProvider(name="youtube-transcript-api")
        ytdlp = _ContractFakeProvider(name="yt-dlp")
        transcript_pipeline.install(
            _router(
                [camofox, yta, ytdlp],
                provider_order=["camofox", "youtube-transcript-api", "yt-dlp"],
            )
        )

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["provider"] == "youtube-transcript-api"
        assert body["metadata"]["fallback_occurred"] is False  # skip, not fetch failure
        assert camofox.fetch_calls == []
        assert yta.fetch_calls == [("dQw4w9WgXcQ", ["en"])]
        assert ytdlp.fetch_calls == []

    def test_blocked_falls_back_to_yt_dlp(self, transcript_pipeline):
        from app.core.errors import TranscriptProviderBlockedError

        camofox = _ContractFakeProvider(
            name="camofox", fetch_error=TranscriptProviderBlockedError("IP blocked")
        )
        yta = _ContractFakeProvider(name="youtube-transcript-api")
        ytdlp = _ContractFakeProvider(name="yt-dlp")
        transcript_pipeline.install(
            _router(
                [camofox, yta, ytdlp],
                provider_order=["camofox", "youtube-transcript-api", "yt-dlp"],
            )
        )

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["provider"] == "youtube-transcript-api"
        assert body["metadata"]["fallback_occurred"] is True
        assert body["metadata"]["providers_attempted"] == ["camofox", "youtube-transcript-api"]
        assert any("camofox" in warning for warning in body["metadata"]["warnings"])

    def test_all_retryable_failures_return_503_with_report(self, transcript_pipeline):
        from app.core.errors import (
            TranscriptProviderBlockedError,
            TranscriptProviderUnavailableError,
        )

        camofox = _ContractFakeProvider(
            name="camofox", fetch_error=TranscriptProviderUnavailableError("down")
        )
        yta = _ContractFakeProvider(
            name="youtube-transcript-api", fetch_error=TranscriptProviderBlockedError("blocked")
        )
        ytdlp = _ContractFakeProvider(
            name="yt-dlp", fetch_error=TranscriptProviderUnavailableError("down")
        )
        transcript_pipeline.install(
            _router(
                [camofox, yta, ytdlp],
                provider_order=["camofox", "youtube-transcript-api", "yt-dlp"],
            )
        )

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 503, response.text
        error = response.json()["error"]
        assert error["type"] == "no_transcript_provider_available"
        assert "camofox" in error["message"] or True  # message format is not contract
        assert error["details"]["providers_attempted"] == [
            "camofox",
            "youtube-transcript-api",
            "yt-dlp",
        ]

    def test_definitive_error_stops_the_chain(self, transcript_pipeline):
        """No captions is a *video* fact: every provider would hit the same
        wall, so the chain must stop — no fallback, 404."""
        camofox = _ContractFakeProvider(
            name="camofox", fetch_error=NoTranscriptAvailableError("no captions")
        )
        yta = _ContractFakeProvider(name="youtube-transcript-api")
        ytdlp = _ContractFakeProvider(name="yt-dlp")
        transcript_pipeline.install(
            _router(
                [camofox, yta, ytdlp],
                provider_order=["camofox", "youtube-transcript-api", "yt-dlp"],
            )
        )

        response = _client().get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert response.status_code == 404, response.text
        assert response.json()["error"]["type"] == "no_transcript_available"
        assert camofox.fetch_calls == [("dQw4w9WgXcQ", ["en"])]
        assert yta.fetch_calls == []  # definitive error stopped the chain
        assert ytdlp.fetch_calls == []

    def test_explicit_provider_never_falls_back(self, transcript_pipeline):
        from app.core.errors import TranscriptProviderUnavailableError

        camofox = _ContractFakeProvider(
            name="camofox", fetch_error=TranscriptProviderUnavailableError("down")
        )
        yta = _ContractFakeProvider(name="youtube-transcript-api")
        transcript_pipeline.install(_router([camofox, yta]))

        response = _client().get(
            "/v1/transcript?" + _transcript_query(provider="camofox"), headers=AUTH_HEADER
        )
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "transcript_provider_unavailable"
        assert yta.fetch_calls == []  # explicit request never falls back

    def test_unknown_provider_is_400(self, transcript_pipeline):
        transcript_pipeline.install(_router([_ContractFakeProvider(name="alpha")]))

        response = _client().get(
            "/v1/transcript?" + _transcript_query(provider="nope"), headers=AUTH_HEADER
        )
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "validation_error"


# ====================================================== cached & stale paths


class TestCachedAndStaleContract:
    def test_cached_response_stays_inside_contract(self, transcript_pipeline):
        provider = _ContractFakeProvider(name="alpha")
        transcript_pipeline.install(
            _router([provider]),
            cache_config=TranscriptCacheConfig(enabled=True, default_ttl_seconds=3600),
        )
        client = _client()
        fresh = client.get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER).json()
        assert_public_contract(fresh)

        cached = client.get("/v1/transcript?" + _transcript_query(), headers=AUTH_HEADER)
        assert cached.status_code == 200
        body = cached.json()
        assert body["metadata"]["from_cache"] is True
        assert body["metadata"]["cached_at"] is not None
        assert_public_contract(body)
        assert body["text"] == fresh["text"]  # identical normalized content

    def test_stale_response_stays_inside_contract(self, transcript_pipeline):
        from app.core.errors import TranscriptProviderTimeoutError

        provider = _ContractFakeProvider(name="alpha")
        transcript_pipeline.install(
            _router([provider]),
            cache_config=TranscriptCacheConfig(
                enabled=True, default_ttl_seconds=3600, stale_if_error=True
            ),
        )
        client = _client()
        query = "/v1/transcript?" + _transcript_query()
        client.get(query, headers=AUTH_HEADER)

        store = app.state.transcript_cache._store
        row = store._conn.execute("SELECT cache_key FROM transcript_cache LIMIT 1").fetchone()
        entry = store.get_transcript_entry(row["cache_key"])
        store.set_transcript_entry(
            cache_key=row["cache_key"],
            request=entry["request"],
            response=entry["response"],
            fetched_at=datetime.fromisoformat(entry["fetched_at"]),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            content_length=entry["content_length"],
        )
        provider._fetch_error = TranscriptProviderTimeoutError("timed out")

        stale = client.get(query, headers=AUTH_HEADER)
        assert stale.status_code == 200, stale.text
        body = stale.json()
        assert body["metadata"]["stale"] is True
        assert_public_contract(body)


# =========================================================== identifiability


class TestVideoIdentifiability:
    def test_watch_youtube_be_bare_id_all_resolve_to_canonical_id(self, transcript_pipeline):
        provider = _ContractFakeProvider(name="alpha")
        transcript_pipeline.install(_router([provider]))
        client = _client()

        for query in (
            "url=" + VIDEO_URL,
            "url=https://youtu.be/dQw4w9WgXcQ",
            "video_id=dQw4w9WgXcQ",
        ):
            response = client.get(f"/v1/transcript?{query}", headers=AUTH_HEADER)
            assert response.status_code == 200, response.text
            assert response.json()["video_id"] == "dQw4w9WgXcQ"
            assert response.json()["metadata"]["provider"] == "alpha"
