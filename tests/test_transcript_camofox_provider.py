"""Tests for the Camofox transcript provider.

Covers the ``[mm:ss]`` transcript parser (minutes, hours, multi-line,
empty, garbage), the provider's error mapping, and its disabled-config
behaviour.  The Camofox client is faked in-memory (a structural stand-in);
client-level behaviour is covered in ``tests/test_camofox_client.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.camofox.types import CamofoxTranscript
from app.core.config import Settings
from app.core.errors import (
    NoTranscriptAvailableError,
    TranscriptLanguageUnavailableError,
    TranscriptProviderBlockedError,
    TranscriptProviderInvalidResponseError,
    TranscriptProviderTimeoutError,
    TranscriptProviderUnavailableError,
    ValidationError,
    VideoUnavailableError,
)
from app.transcript.models import TranscriptSegment
from app.transcript.providers.camofox import (
    CamofoxTranscriptProvider,
    CamofoxTranscriptProviderConfig,
    _map_failure,
    _map_upstream_error,
    _timestamp_to_seconds,
    build_camofox_transcript_provider,
    parse_transcript_segments,
)


def _provider(
    client: Any | None = "client",
    *,
    config: CamofoxTranscriptProviderConfig | None = None,
    default_timeout_seconds: float | None = None,
) -> CamofoxTranscriptProvider:
    return CamofoxTranscriptProvider(
        client,
        config or CamofoxTranscriptProviderConfig(),
        default_timeout_seconds=default_timeout_seconds,
    )


# ========================================================================
# parse_transcript_segments
# ========================================================================


class TestParseTranscriptSegments:
    def test_minutes_format(self) -> None:
        segments = parse_transcript_segments("[00:01] hello\n[00:03] world")
        assert segments == [
            TranscriptSegment(start=1.0, duration=2.0, text="hello"),
            TranscriptSegment(start=3.0, duration=None, text="world"),
        ]

    def test_hours_format(self) -> None:
        segments = parse_transcript_segments("[1:02:03] long one\n[1:03:00] next")
        assert segments[0].start == 3723.0
        assert segments[0].duration == 57.0
        assert segments[1].start == 3780.0
        assert segments[1].duration is None

    def test_multi_line_entries(self) -> None:
        # Verified shape from a real Camofox instance: continuation lines
        # (no timestamp) belong to the previous caption.
        text = "[00:22] \u266a You know the rules\nand so do I \u266a\n[00:27] next line"
        segments = parse_transcript_segments(text)
        assert len(segments) == 2
        assert segments[0].start == 22.0
        assert segments[0].text == "\u266a You know the rules and so do I \u266a"
        assert segments[0].duration == 5.0
        assert segments[1].start == 27.0
        assert segments[1].duration is None

    def test_blank_lines_are_ignored(self) -> None:
        segments = parse_transcript_segments("[00:01] a\n\n[00:05] b\n\n")
        assert [s.text for s in segments] == ["a", "b"]
        assert segments[0].duration == 4.0

    def test_empty_text(self) -> None:
        assert parse_transcript_segments("") == []
        assert parse_transcript_segments("   \n  \n") == []

    def test_garbage_lines_are_dropped(self) -> None:
        # No timestamp before any caption; a malformed [90:99] entry; a
        # 4-part timestamp — all dropped, the rest parsed.
        text = (
            "WEBVTT\n"
            "some junk before any timestamp\n"
            "[00:05] good\n"
            "[90:99] not-a-time\n"
            "[01:02:03:04] four-part is invalid\n"
            "[01:00:00] fine again\n"
        )
        segments = parse_transcript_segments(text)
        assert [(s.start, s.text) for s in segments] == [(5.0, "good"), (3600.0, "fine again")]

    def test_timestamp_only_lines_yield_no_segment(self) -> None:
        segments = parse_transcript_segments("[00:01] \n[00:02] real")
        assert [s.text for s in segments] == ["real"]
        assert segments[0].start == 2.0

    def test_duration_is_never_negative(self) -> None:
        # Out-of-order input must not produce negative durations.
        segments = parse_transcript_segments("[00:10] a\n[00:05] b")
        assert segments[0].duration == 0.0

    def test_last_segment_has_none_duration(self) -> None:
        segments = parse_transcript_segments("[00:01] a\n[00:02] b")
        assert segments[-1].duration is None


# ========================================================================
# Provider health (disabled / configured)
# ========================================================================


class _FakeCamofoxClient:
    """Structural stand-in for the Camofox client used by the provider."""

    def __init__(
        self,
        *,
        result: CamofoxTranscript | None = None,
        raise_exc: Exception | None = None,
        health_available: bool = True,
        probe_raises: Exception | None = None,
    ) -> None:
        self._result = result
        self._raise_exc = raise_exc
        self._health_available = health_available
        self._browser_connected = None
        self._probe_raises = probe_raises
        self.requests: list[tuple[str, list[str], float | None]] = []

    async def probe_health(self) -> Any:
        if self._probe_raises is not None:
            raise self._probe_raises
        from app.camofox.types import CamofoxHealthStatus

        return CamofoxHealthStatus(
            available=self._health_available,
            reason=None if self._health_available else "camofox unreachable",
            browser_connected=self._browser_connected if self._health_available else None,
        )

    async def youtube_transcript(
        self, url: str, languages: list[str], *, timeout_seconds: float | None = None
    ) -> CamofoxTranscript:
        self.requests.append((url, list(languages), timeout_seconds))
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._result is not None
        return self._result


class TestProviderHealth:
    @pytest.mark.asyncio
    async def test_disabled_config_reports_unavailable(self) -> None:
        provider = _provider(client=None)
        health = await provider.health()
        assert health.available is False
        assert "not configured" in (health.reason or "")

    @pytest.mark.asyncio
    async def test_healthy_client_reports_available(self) -> None:
        provider = _provider(client=_FakeCamofoxClient(health_available=True))
        health = await provider.health()
        assert health.available is True
        assert health.reason is None

    @pytest.mark.asyncio
    async def test_unreachable_server_reports_unavailable(self) -> None:
        provider = _provider(client=_FakeCamofoxClient(health_available=False))
        health = await provider.health()
        assert health.available is False
        assert health.reason == "camofox unreachable"

    @pytest.mark.asyncio
    async def test_probe_failure_reports_unavailable(self) -> None:
        provider = _provider(client=_FakeCamofoxClient(probe_raises=RuntimeError("boom")))
        health = await provider.health()
        assert health.available is False

    @pytest.mark.asyncio
    async def test_probe_disabled_reports_available_without_i_o(self) -> None:
        provider = _provider(
            client=_FakeCamofoxClient(health_available=False),
            config=CamofoxTranscriptProviderConfig(health_probe=False),
        )
        health = await provider.health()
        assert health.available is True

    @pytest.mark.asyncio
    async def test_browser_not_connected_still_available(self) -> None:
        # The transcript endpoint is server-side (yt-dlp); a downed browser
        # must not take the provider out of rotation.
        provider = _provider(client=_FakeCamofoxClient(health_available=True))
        provider._client._browser_connected = False
        health = await provider.health()
        assert health.available is True
        assert "browser" in (health.reason or "")


# ========================================================================
# Provider fetch: success
# ========================================================================


class TestProviderFetchSuccess:
    @pytest.mark.asyncio
    async def test_success_normalizes_segments_and_fields(self) -> None:
        result = CamofoxTranscript(
            ok=True,
            status="ok",
            transcript="[00:01] hello\n[00:03] world\nand more",
            video_id="dQw4w9WgXcQ",
            video_title="A Title",
            language="en",
            total_words=5,
        )
        provider = _provider(client=_FakeCamofoxClient(result=result))
        response = await provider.fetch("dQw4w9WgXcQ", ["en"])

        assert response.video_id == "dQw4w9WgXcQ"
        assert response.title == "A Title"
        assert response.language == "en"
        assert response.is_generated is None  # the endpoint does not report it
        assert response.provider == "camofox"
        assert response.metadata.provider == "camofox"
        assert response.segments[0] == TranscriptSegment(start=1.0, duration=2.0, text="hello")
        assert response.segments[1].text == "world and more"
        assert response.total_words == len(response.text.split())

        # The client is called with the canonical URL, the languages and the
        # resolved timeout (provider override, else the shared timeout).
        assert provider._client.requests == [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ["en"], None)
        ]

    @pytest.mark.asyncio
    async def test_provider_timeout_override_is_forwarded(self) -> None:
        result = CamofoxTranscript(ok=True, status="ok", transcript="[00:01] hi")
        provider = _provider(
            client=_FakeCamofoxClient(result=result),
            config=CamofoxTranscriptProviderConfig(timeout_seconds=120),
            default_timeout_seconds=60,
        )
        await provider.fetch("dQw4w9WgXcQ", ["en"])
        assert provider._client.requests[0][2] == 120

    @pytest.mark.asyncio
    async def test_no_override_falls_back_to_shared_timeout(self) -> None:
        result = CamofoxTranscript(ok=True, status="ok", transcript="[00:01] hi")
        provider = _provider(client=_FakeCamofoxClient(result=result), default_timeout_seconds=60)
        await provider.fetch("dQw4w9WgXcQ", ["en"])
        assert provider._client.requests[0][2] == 60


# ========================================================================
# Provider fetch: error mapping
# ========================================================================


class TestProviderFetchErrorMapping:
    @pytest.mark.asyncio
    async def test_timeout_maps_to_provider_timeout(self) -> None:
        from app.core.errors import CamofoxTimeoutError

        exc = CamofoxTimeoutError("Camofox /youtube/transcript request timed out after 60s")
        provider = _provider(client=_FakeCamofoxClient(raise_exc=exc))
        with pytest.raises(TranscriptProviderTimeoutError) as err:
            await provider.fetch("dQw4w9WgXcQ", ["en"])
        assert err.value.details["provider"] == "camofox"

    @pytest.mark.asyncio
    async def test_auth_maps_to_blocked(self) -> None:
        from app.core.errors import CamofoxAuthError

        exc = CamofoxAuthError("Camofox rejected the configured credentials")
        provider = _provider(client=_FakeCamofoxClient(raise_exc=exc))
        with pytest.raises(TranscriptProviderBlockedError):
            await provider.fetch("dQw4w9WgXcQ", ["en"])

    @pytest.mark.asyncio
    async def test_unavailable_maps_to_provider_unavailable(self) -> None:
        from app.core.errors import CamofoxUnavailableError

        exc = CamofoxUnavailableError("Camofox service is unreachable")
        provider = _provider(client=_FakeCamofoxClient(raise_exc=exc))
        with pytest.raises(TranscriptProviderUnavailableError):
            await provider.fetch("dQw4w9WgXcQ", ["en"])

    @pytest.mark.asyncio
    async def test_invalid_response_maps_to_provider_invalid(self) -> None:
        from app.core.errors import CamofoxInvalidResponseError

        exc = CamofoxInvalidResponseError("Camofox /youtube/transcript returned invalid JSON")
        provider = _provider(client=_FakeCamofoxClient(raise_exc=exc))
        with pytest.raises(TranscriptProviderInvalidResponseError):
            await provider.fetch("dQw4w9WgXcQ", ["en"])

    @pytest.mark.asyncio
    async def test_unexpected_exception_maps_to_provider_unavailable(self) -> None:
        provider = _provider(client=_FakeCamofoxClient(raise_exc=ValueError("odd")))
        with pytest.raises(TranscriptProviderUnavailableError):
            await provider.fetch("dQw4w9WgXcQ", ["en"])

    @pytest.mark.asyncio
    async def test_no_captions_error_result(self) -> None:
        # Verified shape from the running Camofox instance.
        result = CamofoxTranscript(
            ok=False,
            status="error",
            code=404,
            message="No captions available for this video",
            video_id="dQw4w9WgXcQ",
        )
        provider = _provider(client=_FakeCamofoxClient(result=result))
        with pytest.raises(NoTranscriptAvailableError):
            await provider.fetch("dQw4w9WgXcQ", ["en"])

    @pytest.mark.asyncio
    async def test_private_video_error_result(self) -> None:
        result = CamofoxTranscript(
            ok=False,
            status="error",
            code=500,
            message="Video unavailable: This video is private",
        )
        provider = _provider(client=_FakeCamofoxClient(result=result))
        with pytest.raises(VideoUnavailableError):
            await provider.fetch("dQw4w9WgXcQ", ["en"])

    @pytest.mark.asyncio
    async def test_unknown_error_result_maps_to_unavailable(self) -> None:
        result = CamofoxTranscript(ok=False, status="error", code=500, message="kaboom")
        provider = _provider(client=_FakeCamofoxClient(result=result))
        with pytest.raises(TranscriptProviderUnavailableError):
            await provider.fetch("dQw4w9WgXcQ", ["en"])

    @pytest.mark.asyncio
    async def test_language_fallback_is_reported_not_invented(self) -> None:
        # The server silently falls back to the first available track; the
        # provider surfaces that as a language-unavailable error so the
        # caller can retry with the real language.
        result = CamofoxTranscript(
            ok=True,
            status="ok",
            transcript="[00:01] hola",
            language="es",
        )
        provider = _provider(client=_FakeCamofoxClient(result=result))
        with pytest.raises(TranscriptLanguageUnavailableError) as err:
            await provider.fetch("dQw4w9WgXcQ", ["en"])
        assert err.value.details["available"] == ["es"]

    @pytest.mark.asyncio
    async def test_empty_transcript_maps_to_invalid_response(self) -> None:
        result = CamofoxTranscript(ok=True, status="ok", transcript="   \n  ")
        provider = _provider(client=_FakeCamofoxClient(result=result))
        with pytest.raises(TranscriptProviderInvalidResponseError):
            await provider.fetch("dQw4w9WgXcQ", ["en"])

    @pytest.mark.asyncio
    async def test_empty_languages_raises_validation(self) -> None:
        provider = _provider(client=_FakeCamofoxClient())
        with pytest.raises(ValidationError):
            await provider.fetch("dQw4w9WgXcQ", [])

    @pytest.mark.asyncio
    async def test_disabled_config_fetch_raises_unavailable(self) -> None:
        provider = _provider(client=None)
        with pytest.raises(TranscriptProviderUnavailableError):
            await provider.fetch("dQw4w9WgXcQ", ["en"])


class TestTimestampToSeconds:
    def test_minutes(self) -> None:
        assert _timestamp_to_seconds("05:30") == 330.0

    def test_hours(self) -> None:
        assert _timestamp_to_seconds("1:02:03") == 3723.0

    def test_invalid_seconds(self) -> None:
        assert _timestamp_to_seconds("90:99") is None

    def test_invalid_minutes(self) -> None:
        assert _timestamp_to_seconds("70:01") is None

    def test_non_numeric(self) -> None:
        assert _timestamp_to_seconds("ab:cd") is None

    def test_too_many_parts(self) -> None:
        assert _timestamp_to_seconds("1:02:03:04") is None

    def test_single_part_invalid(self) -> None:
        assert _timestamp_to_seconds("42") is None


class TestMapUpstreamError:
    def test_no_captions(self) -> None:
        exc = _map_upstream_error(
            "No captions available for this video", 404, video_id="v", languages=["en"]
        )
        assert isinstance(exc, NoTranscriptAvailableError)

    def test_private_video(self) -> None:
        exc = _map_upstream_error(
            "Video unavailable: This video is private", 500, video_id="v", languages=["en"]
        )
        assert isinstance(exc, VideoUnavailableError)

    def test_rate_limit_maps_to_blocked(self) -> None:
        exc = _map_upstream_error("You have been rate limited", 500, video_id="v", languages=["en"])
        assert isinstance(exc, TranscriptProviderBlockedError)

    def test_generic_error(self) -> None:
        exc = _map_upstream_error("something exploded", 500, video_id="v", languages=["en"])
        assert isinstance(exc, TranscriptProviderUnavailableError)

    def test_code_404_without_marker(self) -> None:
        exc = _map_upstream_error(None, 404, video_id="v", languages=["en"])
        assert isinstance(exc, NoTranscriptAvailableError)


class TestMapFailure:
    def test_unknown_exception_maps_to_unavailable(self) -> None:
        exc = _map_failure(RuntimeError("odd"), video_id="v", languages=["en"])
        assert isinstance(exc, TranscriptProviderUnavailableError)
        assert exc.details["provider"] == "camofox"

    def test_passthrough_for_project_errors(self) -> None:
        original = NoTranscriptAvailableError("no captions", details={"provider": "camofox"})
        assert _map_failure(original, video_id="v", languages=["en"]) is original


# ========================================================================
# Configuration + construction
# ========================================================================


class TestProviderConfig:
    def _settings(
        self, transcript_block: dict | None = None, camofox_block: dict | None = None
    ) -> Settings:
        return Settings(
            _env_file=None,
            api_key="k",
            _raw_yaml_transcript=transcript_block,
            _raw_yaml_camofox=camofox_block,
        )

    def test_defaults(self) -> None:
        config = CamofoxTranscriptProviderConfig()
        assert config.enabled is False
        assert config.timeout_seconds is None
        assert config.health_probe is True

    def test_from_settings_yaml_block(self) -> None:
        settings = self._settings(
            transcript_block={
                "providers": {
                    "camofox": {"enabled": True, "timeout_seconds": 90, "health_probe": False}
                }
            }
        )
        config = CamofoxTranscriptProviderConfig.from_settings(settings)
        assert config.enabled is True
        assert config.timeout_seconds == 90
        assert config.health_probe is False

    def test_from_settings_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPER_TRANSCRIPT_CAMOFOX_ENABLED", "true")
        settings = Settings(_env_file=None, api_key="k")
        config = CamofoxTranscriptProviderConfig.from_settings(settings)
        assert config.enabled is True

    def test_from_settings_missing_block(self) -> None:
        settings = Settings(_env_file=None, api_key="k")
        config = CamofoxTranscriptProviderConfig.from_settings(settings)
        assert config.enabled is False

    def test_from_settings_bad_block_type(self) -> None:
        settings = self._settings(transcript_block={"providers": {"camofox": "yes"}})
        with pytest.raises(ValueError):
            CamofoxTranscriptProviderConfig.from_settings(settings)


class TestBuildProvider:
    def _settings(
        self, transcript_block: dict | None = None, camofox_block: dict | None = None
    ) -> Settings:
        return Settings(
            _env_file=None,
            api_key="k",
            _raw_yaml_transcript=transcript_block,
            _raw_yaml_camofox=camofox_block,
        )

    def test_disabled_returns_none(self) -> None:
        settings = self._settings()
        provider = build_camofox_transcript_provider(settings, client="c")
        assert provider is None

    def test_enabled_returns_provider(self) -> None:
        settings = self._settings(transcript_block={"providers": {"camofox": {"enabled": True}}})
        provider = build_camofox_transcript_provider(settings, client="c")
        assert provider is not None

    def test_enabled_with_client_reuses_it(self) -> None:
        settings = self._settings(transcript_block={"providers": {"camofox": {"enabled": True}}})
        provider = build_camofox_transcript_provider(settings, client="shared-client")
        assert provider is not None
        assert provider.name == "camofox"
        assert provider.client == "shared-client"

    def test_enabled_without_client_and_camofox_disabled_degrades(self) -> None:
        settings = self._settings(
            transcript_block={"providers": {"camofox": {"enabled": True}}},
            camofox_block={"enabled": False},
        )
        provider = build_camofox_transcript_provider(settings)
        assert provider is not None
        assert provider.client is None

    def test_enabled_without_client_and_camofox_enabled_builds_client(self) -> None:
        settings = self._settings(
            transcript_block={"providers": {"camofox": {"enabled": True}}},
            camofox_block={"enabled": True, "base_url": "http://camofox:9377"},
        )
        provider = build_camofox_transcript_provider(settings)
        assert provider is not None
        assert provider.client is not None

    def test_provider_timeout_override_wins_over_shared(self) -> None:
        settings = self._settings(
            transcript_block={"providers": {"camofox": {"enabled": True, "timeout_seconds": 120}}},
            camofox_block={
                "enabled": True,
                "base_url": "http://camofox:9377",
                "timeout_seconds": 60,
            },
        )
        provider = build_camofox_transcript_provider(settings)
        assert provider is not None
        assert provider._default_timeout_seconds == 120

    def test_shared_timeout_used_when_no_override(self) -> None:
        settings = self._settings(
            transcript_block={"providers": {"camofox": {"enabled": True}}},
            camofox_block={
                "enabled": True,
                "base_url": "http://camofox:9377",
                "timeout_seconds": 90,
            },
        )
        provider = build_camofox_transcript_provider(settings)
        assert provider is not None
        assert provider._default_timeout_seconds == 90
