"""End-to-end integration tests for the Transcript path (ticket #129).

Same story as the search stack (it is a deliberate sibling): the real
production composition, external I/O faked::

    TestClient (real FastAPI app, real bearer auth)
      → GET /v1/transcript  (real query validation + video ID parsing)
      → TranscriptCache     (real SQLite transcript cache)
      → TranscriptRouter    (real health probe + routing + fallback policy)
      → TranscriptProvider  (structural fake on a dead transport)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import (
    NoTranscriptAvailableError,
    TranscriptProviderUnavailableError,
)
from app.main import app
from app.metrics.prometheus import MetricsCollector
from app.transcript import (
    TranscriptCache,
    TranscriptCacheConfig,
    TranscriptProviderRegistry,
    TranscriptRouter,
    TranscriptRouterConfig,
)
from tests.fakes import FakeTranscriptProvider

API_KEY = "test-key-for-tests"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
VIDEO_ID = "dQw4w9WgXcQ"


@pytest.fixture()
def pipeline(tmp_path):
    """Fresh in-memory transcript pipeline on ``app.state`` (leaves no state)."""

    class _Installer:
        def __init__(self) -> None:
            self.stores: list = []
            self.cache = None  # TranscriptCache | None

        def install(
            self,
            *providers: FakeTranscriptProvider,
            provider_order: list[str] | None = None,
            cache_config: TranscriptCacheConfig | None = None,
        ) -> TranscriptCache:
            from app.cache.sqlite_cache import SqliteCache

            router = TranscriptRouter(
                TranscriptProviderRegistry(list(providers)),
                TranscriptRouterConfig(provider_order=provider_order or []),
                metrics=MetricsCollector(),
            )
            # Reuse the real SQLite table shape (transcript_cache).
            store = SqliteCache(db_path=str(tmp_path / f"transcript-{len(self.stores)}.db"))
            store.open()
            self.stores.append(store)
            self.cache = TranscriptCache(
                store=store,
                router=router,
                config=cache_config or TranscriptCacheConfig(),
                metrics=MetricsCollector(),
            )
            app.state.transcript_cache = self.cache
            return self.cache

    installer = _Installer()
    app.state.settings = Settings(api_key=API_KEY)
    yield installer
    for store in installer.stores:
        try:
            store.close()
        except Exception:
            pass
    if hasattr(app.state, "transcript_cache"):
        delattr(app.state, "transcript_cache")


def _get(provider: str = "auto", **extra) -> dict:
    return TestClient(app).get(
        "/v1/transcript",
        params={"video_id": VIDEO_ID, "provider": provider, **extra},
        headers=AUTH,
    )


def test_transcript_cache_hit(pipeline):
    """Scenario 1 — a second identical request is served from cache; the
    provider runs exactly once."""
    provider = FakeTranscriptProvider("youtube-transcript-api")
    pipeline.install(provider)

    first = _get()
    assert first.status_code == 200, first.text
    assert first.json()["metadata"]["from_cache"] is False
    assert provider.transcript_calls == 1

    second = _get()
    assert second.status_code == 200
    assert second.json()["metadata"]["from_cache"] is True
    assert provider.transcript_calls == 1


def test_transcript_success(pipeline):
    """Scenario 2 — a healthy primary provider answers in one attempt."""
    provider = FakeTranscriptProvider("youtube-transcript-api")
    pipeline.install(provider, provider_order=["youtube-transcript-api"])

    body = _get()
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["video_id"] == VIDEO_ID
    assert data["text"]  # normalized plain text
    assert data["metadata"]["provider"] == "youtube-transcript-api"
    assert data["metadata"]["providers_attempted"] == ["youtube-transcript-api"]
    assert data["metadata"]["fallback_occurred"] is False


def test_provider_failure_falls_back(pipeline):
    """Scenario 3 — a transient failure on the primary routes to the fallback
    provider, which succeeds (auto mode)."""
    primary = FakeTranscriptProvider("youtube-transcript-api")
    fallback = FakeTranscriptProvider("yt-dlp")
    primary.set_transcript_error(TranscriptProviderUnavailableError("primary down"))
    pipeline.install(primary, fallback, provider_order=["youtube-transcript-api", "yt-dlp"])

    body = _get()
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["metadata"]["providers_attempted"] == ["youtube-transcript-api", "yt-dlp"]
    assert data["metadata"]["fallback_occurred"] is True
    assert len(data["metadata"]["warnings"]) >= 1


def test_unavailable_provider_surfaces_as_503(pipeline):
    """Scenario 4 — an *explicit* request to an unavailable provider fails
    with a clear 503; auto over a fully broken chain is 503 with all
    attempts reported (no silent 200)."""
    primary = FakeTranscriptProvider("youtube-transcript-api", healthy=False)
    fallback = FakeTranscriptProvider("yt-dlp", healthy=False)
    pipeline.install(primary, fallback, provider_order=["youtube-transcript-api", "yt-dlp"])

    explicit = _get(provider="youtube-transcript-api")
    assert explicit.status_code == 503
    assert explicit.json()["error"]["type"] == "transcript_provider_unavailable"

    auto = _get(provider="auto")
    assert auto.status_code == 503
    assert auto.json()["error"]["type"] == "no_transcript_provider_available"
    assert auto.json()["error"]["details"]["candidates"]  # both probes reported


def test_no_provider_for_video_404(pipeline):
    """Scenario 5 — a video with no captions at all → 404
    ``no_transcript_available`` (deterministic, not retryable, no fallback)."""
    provider = FakeTranscriptProvider(
        "youtube-transcript-api", error=NoTranscriptAvailableError("no captions")
    )
    pipeline.install(provider, provider_order=["youtube-transcript-api"])

    body = _get()
    assert body.status_code == 404
    assert body.json()["error"]["type"] == "no_transcript_available"


def test_stale_if_error(pipeline):
    """Scenario 6 — live run fails (retryable) and the cached entry is
    expired: the stale entry is still served, flagged as stale."""
    provider = FakeTranscriptProvider("youtube-transcript-api")
    cache = pipeline.install(
        provider,
        cache_config=TranscriptCacheConfig(
            enabled=True, default_ttl_seconds=60, stale_if_error=True
        ),
    )

    assert _get().status_code == 200
    provider.set_transcript_error(TranscriptProviderUnavailableError("down"))

    from app.transcript.cache import make_transcript_cache_key

    conn = cache._store._conn
    from app.transcript.models import TranscriptRequest

    # The request is made with provider="auto" (the default query param),
    # so the cache key must use the same provider value.
    key = make_transcript_cache_key(
        TranscriptRequest(video_id=VIDEO_ID, languages=["en"], provider="auto")
    )
    conn.execute(
        "UPDATE transcript_cache SET fetched_at = '2020-01-01T00:00:00+00:00', "
        "expires_at = '2020-01-01T00:01:00+00:00' WHERE cache_key = ?",
        (key,),
    )
    conn.commit()

    body = _get()
    assert body.status_code == 200, body.text
    assert body.json()["metadata"]["from_cache"] is True
    assert body.json()["metadata"]["stale"] is True
    assert any("stale" in w for w in body.json()["metadata"]["warnings"])
