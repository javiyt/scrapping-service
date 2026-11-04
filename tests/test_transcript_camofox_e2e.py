"""End-to-end router + Camofox provider tests.

These exercise the real :class:`app.transcript.providers.camofox.
CamofoxTranscriptProvider` through the real
:class:`app.transcript.router.TranscriptRouter`, faking only the Camofox
*client* (an in-memory structural stand-in).  The client-level behaviour is
covered in ``tests/test_camofox_client_transcript.py``; the provider in
``tests/test_transcript_camofox_provider.py``.  What is verified here is the
*composition*: the router picks the Camofox provider, forwards the
video/language, normalizes the result, and surfaces the right
:class:`ScraperError` (and therefore HTTP status) on each failure class.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.camofox.types import CamofoxTranscript
from app.core.config import Settings
from app.core.errors import (
    NoTranscriptAvailableError,
    NoTranscriptProviderAvailableError,
    TranscriptLanguageUnavailableError,
    TranscriptProviderInvalidResponseError,
    TranscriptProviderTimeoutError,
    TranscriptProviderUnavailableError,
    VideoUnavailableError,
)
from app.metrics.prometheus import MetricsCollector
from app.transcript.models import TranscriptRequest
from app.transcript.providers.camofox import (
    CamofoxTranscriptProvider,
    CamofoxTranscriptProviderConfig,
)
from app.transcript.router import build_transcript_router


class _FakeCamofoxClient:
    """Structural stand-in for the Camofox client used by the provider."""

    def __init__(
        self,
        *,
        result: CamofoxTranscript | None = None,
        raise_exc: Exception | None = None,
        health_available: bool = True,
        browser_connected: bool | None = None,
        probe_raises: Exception | None = None,
    ) -> None:
        self._result = result
        self._raise_exc = raise_exc
        self._health_available = health_available
        self._browser_connected = browser_connected
        self._probe_raises = probe_raises
        self.requests: list[tuple[str, list[str], float | None]] = []

    async def probe_health(self) -> Any:
        if self._probe_raises is not None:
            raise self._probe_raises
        from app.camofox.types import CamofoxHealthStatus

        return CamofoxHealthStatus(
            available=self._health_available,
            reason=None if self._health_available else "camofox unreachable",
            browser_connected=self._browser_connected,
        )

    async def youtube_transcript(
        self, url: str, languages: list[str], *, timeout_seconds: float | None = None
    ) -> CamofoxTranscript:
        self.requests.append((url, list(languages), timeout_seconds))
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._result is not None
        return self._result


def _settings(enabled: bool = True) -> Settings:
    s = Settings(_env_file=None, api_key="k")
    object.__setattr__(
        s,
        "raw_yaml_transcript",
        {"providers": {"camofox": {"enabled": enabled}}},
    )
    return s


def _ok_transcript(text: str, language: str = "en") -> CamofoxTranscript:
    return CamofoxTranscript(
        ok=True,
        status="ok",
        transcript=text,
        video_id="dQw4w9WgXcQ",
        video_title="A Title",
        language=language,
        total_words=len(text.split()),
    )


def _router(client: _FakeCamofoxClient) -> Any:
    return build_transcript_router(_settings(), metrics=MetricsCollector(), camofox_client=client)


def _request(**overrides: Any) -> TranscriptRequest:
    base: dict[str, Any] = {"video_id": "dQw4w9WgXcQ", "languages": ["en"]}
    base.update(overrides)
    return TranscriptRequest(**base)


# ------------------------------------------------------------------ explicit


class TestCamofoxExplicit:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        client = _FakeCamofoxClient(result=_ok_transcript("[00:01] hello\n[00:05] world"))
        router = _router(client)

        response = await router.route(_request(provider="camofox"))

        assert client.requests == [("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ["en"], None)]
        assert response.provider == "camofox"
        assert response.video_id == "dQw4w9WgXcQ"
        assert response.title == "A Title"
        assert response.language == "en"
        assert len(response.segments) == 2
        assert response.metadata.providers_attempted == ["camofox"]
        assert response.metadata.fallback_occurred is False

    @pytest.mark.asyncio
    async def test_unavailable_client_reports_503(self) -> None:
        client = _FakeCamofoxClient(health_available=False)
        router = _router(client)

        with pytest.raises(TranscriptProviderUnavailableError) as excinfo:
            await router.route(_request(provider="camofox"))

        assert excinfo.value.status_code == 503
        assert client.requests == []

    @pytest.mark.asyncio
    async def test_no_captions_is_404(self) -> None:
        client = _FakeCamofoxClient(
            result=CamofoxTranscript(
                ok=False,
                status="error",
                code=404,
                message="No captions available for this video",
            )
        )
        router = _router(client)

        with pytest.raises(NoTranscriptAvailableError) as excinfo:
            await router.route(_request(provider="camofox"))

        assert excinfo.value.status_code == 404

    @pytest.mark.asyncio
    async def test_timeout_is_504(self) -> None:
        from app.core.errors import CamofoxTimeoutError

        client = _FakeCamofoxClient(raise_exc=CamofoxTimeoutError("timed out"))
        router = _router(client)

        with pytest.raises(TranscriptProviderTimeoutError) as excinfo:
            await router.route(_request(provider="camofox"))

        assert excinfo.value.status_code == 504

    @pytest.mark.asyncio
    async def test_unreachable_is_503(self) -> None:
        from app.core.errors import CamofoxUnavailableError

        client = _FakeCamofoxClient(raise_exc=CamofoxUnavailableError("unreachable"))
        router = _router(client)

        with pytest.raises(TranscriptProviderUnavailableError) as excinfo:
            await router.route(_request(provider="camofox"))

        assert excinfo.value.status_code == 503

    @pytest.mark.asyncio
    async def test_invalid_response_is_502(self) -> None:
        # ok result but no parseable segments.
        client = _FakeCamofoxClient(result=_ok_transcript("   \n  "))
        router = _router(client)

        with pytest.raises(TranscriptProviderInvalidResponseError) as excinfo:
            await router.route(_request(provider="camofox"))

        assert excinfo.value.status_code == 502

    @pytest.mark.asyncio
    async def test_language_mismatch_is_404(self) -> None:
        # The server returns a different language than requested.
        client = _FakeCamofoxClient(result=_ok_transcript("[00:01] hola", language="es"))
        router = _router(client)

        with pytest.raises(TranscriptLanguageUnavailableError) as excinfo:
            await router.route(_request(provider="camofox"))

        assert excinfo.value.status_code == 404
        assert excinfo.value.details["available"] == ["es"]

    @pytest.mark.asyncio
    async def test_private_video_is_404(self) -> None:
        client = _FakeCamofoxClient(
            result=CamofoxTranscript(
                ok=False,
                status="error",
                code=500,
                message="Video unavailable: This video is private",
            )
        )
        router = _router(client)

        with pytest.raises(VideoUnavailableError) as excinfo:
            await router.route(_request(provider="camofox"))

        assert excinfo.value.status_code == 404


# ------------------------------------------------------------------ auto mode


class TestCamofoxAuto:
    @pytest.mark.asyncio
    async def test_auto_selects_healthy_camofox(self) -> None:
        client = _FakeCamofoxClient(result=_ok_transcript("[00:01] hi"))
        router = _router(client)

        response = await router.route(_request())

        assert response.provider == "camofox"
        assert response.metadata.providers_attempted == ["camofox"]
        assert response.metadata.fallback_occurred is False

    @pytest.mark.asyncio
    async def test_auto_skips_unavailable(self) -> None:
        client = _FakeCamofoxClient(health_available=False)
        router = _router(client)

        with pytest.raises(NoTranscriptProviderAvailableError) as excinfo:
            await router.route(_request())

        assert excinfo.value.status_code == 503

    @pytest.mark.asyncio
    async def test_auto_falls_back_after_retryable_failure(self) -> None:
        from app.core.errors import CamofoxUnavailableError
        from app.transcript.contract import TranscriptHealth
        from app.transcript.models import TranscriptMetadata, TranscriptResponse, TranscriptSegment
        from app.transcript.registry import TranscriptProviderRegistry
        from app.transcript.router import TranscriptRouter

        # A faked second provider so auto has somewhere to fall back to.
        class _Second:
            name = "second"

            async def health(self) -> TranscriptHealth:
                return TranscriptHealth(available=True)

            async def fetch(self, video_id: str, languages: list[str]) -> TranscriptResponse:
                return TranscriptResponse.from_segments(
                    video_id=video_id,
                    segments=[TranscriptSegment(start=0.0, duration=1.0, text="fallback")],
                    provider=self.name,
                    metadata=TranscriptMetadata(provider=self.name),
                )

        camofox_client = _FakeCamofoxClient(raise_exc=CamofoxUnavailableError("camofox down"))
        provider = CamofoxTranscriptProvider(
            camofox_client, CamofoxTranscriptProviderConfig(enabled=True)
        )
        router = TranscriptRouter(
            TranscriptProviderRegistry([provider, _Second()]), metrics=MetricsCollector()
        )

        response = await router.route(_request())

        assert response.provider == "second"
        assert response.metadata.fallback_occurred is True
        assert camofox_client.requests == [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ["en"], None)
        ]
