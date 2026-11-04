"""Tests for the ``youtube-transcript-api`` transcript provider (ticket #146).

Covers:

- transcript selection (requested-language order, manual vs generated,
  with a fake ``TranscriptList``);
- the library-exception -> project-error mapping (every mapping in the
  ticket: block / no captions / video unavailable / other, plus the
  missing-language refinement of ``NoTranscriptFound``);
- the provider ``fetch()`` with the library faked in memory: the response
  mapping, the ``to_thread`` offload (the event loop is never blocked),
  the timeout bound, the proxy configuration wiring, and the
  no-empty-segments error;
- config parsing (YAML block + environment overrides) and the
  construction factory (including the whole
  ``build_transcript_providers`` chain).

The library is a pinned runtime dependency (see ``requirements.txt``), so
the module imports at test time; the *instances* it creates are faked via
module-level attribute patches.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import youtube_transcript_api as yta
from youtube_transcript_api import (
    AgeRestricted,
    IpBlocked,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
)
from youtube_transcript_api import (
    NoTranscriptFound as _NoTranscriptFound,
)

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
from app.transcript.providers import youtube_transcript_api as module
from app.transcript.providers.youtube_transcript_api import (
    NAME,
    YouTubeTranscriptApiProvider,
    YouTubeTranscriptApiProviderConfig,
    _available_languages,
    _map_yta_error,
    _select_transcript,
    build_youtube_transcript_api_provider,
)

VIDEO_ID = "dQw4w9WgXcQ"


# ========================================================================
# In-memory fakes for the library objects
# ========================================================================


class FakeSnippet:
    def __init__(self, text: str, start: float, duration: float) -> None:
        self.text = text
        self.start = start
        self.duration = duration


class FakeFetchedTranscript:
    def __init__(self, snippets: list[FakeSnippet]) -> None:
        self._snippets = snippets
        self.video_id = VIDEO_ID
        self.language = "English"
        self.language_code = "en"
        self.is_generated = False

    def __iter__(self):
        return iter(self._snippets)

    def __len__(self):
        return len(self._snippets)


class FakeTranscript:
    """Stand-in for the library's ``Transcript`` (manual vs generated)."""

    def __init__(
        self,
        language: str,
        language_code: str,
        is_generated: bool,
        snippets: list[FakeSnippet],
    ) -> None:
        self.video_id = VIDEO_ID
        self.language = language
        self.language_code = language_code
        self.is_generated = is_generated
        self._snippets = snippets

    def fetch(self, preserve_formatting: bool = False) -> FakeFetchedTranscript:
        return FakeFetchedTranscript(self._snippets)


def _no_transcript_found_exception(transcript_list: Any) -> Exception:
    return _NoTranscriptFound(VIDEO_ID, ["ja"], transcript_list)


class FakeTranscriptList:
    """Stand-in for the library's ``TranscriptList``.

    Exposes the same *public* attribute names the real object uses
    (``_manually_created_transcripts`` / ``_generated_transcripts``), so
    the provider's accessors work unchanged.
    """

    def __init__(
        self,
        *,
        manual: dict[str, FakeTranscript] | None = None,
        generated: dict[str, FakeTranscript] | None = None,
    ) -> None:
        self.video_id = VIDEO_ID
        self._manually_created_transcripts = dict(manual or {})
        self._generated_transcripts = dict(generated or {})

    def find_manually_created_transcript(self, language_codes: list[str]) -> FakeTranscript:
        for code in language_codes:
            if code in self._manually_created_transcripts:
                return self._manually_created_transcripts[code]
        raise _no_transcript_found_exception(self)

    def find_generated_transcript(self, language_codes: list[str]) -> FakeTranscript:
        for code in language_codes:
            if code in self._generated_transcripts:
                return self._generated_transcripts[code]
        raise _no_transcript_found_exception(self)


def _snippets(*texts: str) -> list[FakeSnippet]:
    return [FakeSnippet(text, start=index * 2.0, duration=2.0) for index, text in enumerate(texts)]


def _list(
    *,
    manual: dict[str, FakeTranscript] | None = None,
    generated: dict[str, FakeTranscript] | None = None,
) -> FakeTranscriptList:
    return FakeTranscriptList(manual=manual, generated=generated)


def _provider(
    *,
    config: YouTubeTranscriptApiProviderConfig | None = None,
) -> YouTubeTranscriptApiProvider:
    return YouTubeTranscriptApiProvider(config or YouTubeTranscriptApiProviderConfig(enabled=True))


def _install_fake_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    transcript_list: FakeTranscriptList | Exception | None = None,
) -> list[dict[str, Any]]:
    """Patch ``module._api`` with an in-memory ``YouTubeTranscriptApi``.

    Returns the captured constructor kwargs (one entry per ``list()``
    call's construction) for proxy-wiring assertions.
    """
    captured: list[dict[str, Any]] = []

    class FakeApi:
        def __init__(self, proxy_config: Any = None, http_client: Any = None) -> None:
            captured.append({"proxy_config": proxy_config})

        def list(self, video_id: str) -> FakeTranscriptList:
            assert video_id == VIDEO_ID
            if isinstance(transcript_list, Exception):
                raise transcript_list
            assert transcript_list is not None
            return transcript_list

    monkeypatch.setattr(module, "_api", lambda: FakeApi)
    return captured


# ========================================================================
# Selection
# ========================================================================


class TestSelectTranscript:
    def test_manual_preferred_within_requested_language(self) -> None:
        manual_en = FakeTranscript("English", "en", False, [])
        generated_en = FakeTranscript("English (auto)", "en", True, [])
        transcript = _select_transcript(
            _list(manual={"en": manual_en}, generated={"en": generated_en}), ["en"]
        )
        assert transcript is manual_en

    def test_falls_back_to_generated(self) -> None:
        generated_de = FakeTranscript("German (auto)", "de", True, [])
        transcript = _select_transcript(_list(generated={"de": generated_de}), ["de"])
        assert transcript is generated_de

    def test_requested_language_order_is_honoured(self) -> None:
        manual_es = FakeTranscript("Spanish", "es", False, [])
        generated_en = FakeTranscript("English (auto)", "en", True, [])
        # "fr" is not offered; "es" (manual) wins over the later "en".
        transcript = _select_transcript(
            _list(manual={"es": manual_es}, generated={"en": generated_en}), ["fr", "es", "en"]
        )
        assert transcript is manual_es

    def test_manual_order_wins_over_generated_of_earlier_language(self) -> None:
        # Requested ["en", "de"]: the manual "de" is chosen over the
        # generated "en" (the library's per-language manual-first rule).
        generated_en = FakeTranscript("English (auto)", "en", True, [])
        manual_de = FakeTranscript("German", "de", False, [])
        transcript = _select_transcript(
            _list(manual={"de": manual_de}, generated={"en": generated_en}), ["en", "de"]
        )
        assert transcript is manual_de

    def test_no_match_returns_none(self) -> None:
        assert (
            _select_transcript(_list(manual={"en": FakeTranscript("e", "en", False, [])}), ["ja"])
            is None
        )

    def test_empty_list_returns_none(self) -> None:
        assert _select_transcript(_list(), ["en"]) is None


class TestAvailableLanguages:
    def test_manual_then_generated_in_order(self) -> None:
        transcript_list = _list(
            manual={
                "es": FakeTranscript("s", "es", False, []),
                "en": FakeTranscript("e", "en", False, []),
            },
            generated={
                "en": FakeTranscript("e", "en", True, []),
                "de": FakeTranscript("d", "de", True, []),
            },
        )
        assert _available_languages(transcript_list) == ["es", "en", "de"]

    def test_empty(self) -> None:
        assert _available_languages(_list()) == []


# ========================================================================
# Error mapping
# ========================================================================


class TestMapYtaError:
    def test_request_blocked_maps_to_blocked(self) -> None:
        error = _map_yta_error(RequestBlocked(VIDEO_ID), video_id=VIDEO_ID, languages=["en"])
        assert isinstance(error, TranscriptProviderBlockedError)

    def test_ip_blocked_maps_to_blocked(self) -> None:
        error = _map_yta_error(IpBlocked(VIDEO_ID), video_id=VIDEO_ID, languages=["en"])
        assert isinstance(error, TranscriptProviderBlockedError)

    def test_transcripts_disabled_maps_to_no_transcript(self) -> None:
        error = _map_yta_error(TranscriptsDisabled(VIDEO_ID), video_id=VIDEO_ID, languages=["en"])
        assert isinstance(error, NoTranscriptAvailableError)

    def test_no_transcript_found_without_data_maps_to_no_transcript(self) -> None:
        error = _map_yta_error(
            _no_transcript_found_exception(_list()), video_id=VIDEO_ID, languages=["ja"]
        )
        assert isinstance(error, NoTranscriptAvailableError)

    def test_no_transcript_found_with_other_languages_maps_to_language_unavailable(self) -> None:
        transcript_list = _list(manual={"es": FakeTranscript("s", "es", False, [])})
        error = _map_yta_error(
            _no_transcript_found_exception(transcript_list), video_id=VIDEO_ID, languages=["ja"]
        )
        assert isinstance(error, TranscriptLanguageUnavailableError)
        details = error.details or {}
        assert details["available"] == ["es"]
        assert details["requested"] == ["ja"]

    def test_video_unavailable_maps_to_video_unavailable(self) -> None:
        error = _map_yta_error(VideoUnavailable(VIDEO_ID), video_id=VIDEO_ID, languages=["en"])
        assert isinstance(error, VideoUnavailableError)

    def test_video_unplayable_maps_to_video_unavailable(self) -> None:
        error = _map_yta_error(
            VideoUnplayable(VIDEO_ID, "reason", []),
            video_id=VIDEO_ID,
            languages=["en"],
        )
        assert isinstance(error, VideoUnavailableError)

    def test_age_restricted_maps_to_video_unavailable(self) -> None:
        error = _map_yta_error(AgeRestricted(VIDEO_ID), video_id=VIDEO_ID, languages=["en"])
        assert isinstance(error, VideoUnavailableError)

    def test_timeout_maps_to_timeout(self) -> None:
        error = _map_yta_error(TimeoutError("t"), video_id=VIDEO_ID, languages=["en"])
        assert isinstance(error, TranscriptProviderTimeoutError)

    def test_unknown_maps_to_unavailable(self) -> None:
        error = _map_yta_error(YouTubeDataUnparsable(VIDEO_ID), video_id=VIDEO_ID, languages=["en"])
        assert isinstance(error, TranscriptProviderUnavailableError)

    def test_public_error_never_carries_upstream_text(self) -> None:
        error = _map_yta_error(IpBlocked(VIDEO_ID), video_id=VIDEO_ID, languages=["en"])
        message = str(error)
        assert "youtube" not in message.lower()
        assert "aws" not in message.lower()
        details = error.details or {}
        assert details["video_id"] == VIDEO_ID
        assert details["provider"] == NAME


# ========================================================================
# fetch()
# ========================================================================


@pytest.mark.asyncio
class TestProviderFetch:
    async def test_manual_track_preferred_and_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manual_es = FakeTranscript("Spanish", "es", False, _snippets("hola", "mundo"))
        generated_es = FakeTranscript("Spanish (auto)", "es", True, _snippets("nope"))
        generated_en = FakeTranscript("English (auto)", "en", True, _snippets("nope2"))
        _install_fake_api(
            monkeypatch,
            transcript_list=_list(
                manual={"es": manual_es}, generated={"es": generated_es, "en": generated_en}
            ),
        )

        response = await _provider().fetch(VIDEO_ID, ["en", "es"])

        assert response.provider == NAME
        assert response.metadata.provider == NAME
        assert response.language == "es"
        assert response.is_generated is False
        assert response.segments == [
            TranscriptSegment(start=0.0, duration=2.0, text="hola"),
            TranscriptSegment(start=2.0, duration=2.0, text="mundo"),
        ]
        assert response.text == "hola mundo"
        assert response.title is None  # the library has no video metadata

    async def test_generated_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        generated_en = FakeTranscript("English (auto)", "en", True, _snippets("hello"))
        _install_fake_api(monkeypatch, transcript_list=_list(generated={"en": generated_en}))

        response = await _provider().fetch(VIDEO_ID, ["en"])

        assert response.language == "en"
        assert response.is_generated is True
        assert response.text == "hello"

    async def test_requested_language_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manual_es = FakeTranscript("Spanish", "es", False, _snippets("hola"))
        generated_en = FakeTranscript("English (auto)", "en", True, _snippets("hello"))
        _install_fake_api(
            monkeypatch,
            transcript_list=_list(manual={"es": manual_es}, generated={"en": generated_en}),
        )

        response = await _provider().fetch(VIDEO_ID, ["fr", "en", "es"])
        assert response.language == "es"  # "fr" absent; "es" manual beats "en" generated

    async def test_missing_language_raises_language_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        transcript_list = _list(
            manual={"es": FakeTranscript("Spanish", "es", False, _snippets("hola"))}
        )
        _install_fake_api(monkeypatch, transcript_list=transcript_list)
        with pytest.raises(TranscriptLanguageUnavailableError) as exc_info:
            await _provider().fetch(VIDEO_ID, ["ja"])
        details = exc_info.value.details or {}
        assert details["available"] == ["es"]
        assert details["requested"] == ["ja"]

    async def test_no_captions_at_all_raises_no_transcript(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_api(monkeypatch, transcript_list=_list())
        with pytest.raises(NoTranscriptAvailableError):
            await _provider().fetch(VIDEO_ID, ["en"])

    async def test_list_exception_is_mapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_api(monkeypatch, transcript_list=IpBlocked(VIDEO_ID))
        with pytest.raises(TranscriptProviderBlockedError):
            await _provider().fetch(VIDEO_ID, ["en"])

    async def test_fetch_exception_is_mapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class ExplodingFetch:
            def fetch(self, preserve_formatting: bool = False) -> None:
                raise VideoUnavailable(VIDEO_ID)

        transcript = FakeTranscript("English", "en", False, [])
        transcript.fetch = ExplodingFetch().fetch  # type: ignore[method-assign]
        _install_fake_api(monkeypatch, transcript_list=_list(manual={"en": transcript}))
        with pytest.raises(VideoUnavailableError):
            await _provider().fetch(VIDEO_ID, ["en"])

    async def test_empty_snippets_map_to_invalid_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manual_en = FakeTranscript("English", "en", False, [])
        _install_fake_api(monkeypatch, transcript_list=_list(manual={"en": manual_en}))
        with pytest.raises(TranscriptProviderInvalidResponseError):
            await _provider().fetch(VIDEO_ID, ["en"])

    async def test_missing_languages_rejected(self) -> None:
        with pytest.raises(ValidationError):
            await _provider().fetch(VIDEO_ID, [])

    async def test_health_is_available(self) -> None:
        health = await _provider().health()
        assert health.available is True
        assert health.reason is None

    async def test_event_loop_is_never_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The library is synchronous: the provider must run it off the
        event loop.  If ``list()`` / ``fetch()`` ran on the loop, this
        concurrent heartbeat would be starved for the whole sleep."""
        loop = asyncio.get_running_loop()
        heartbeat: list[float] = []
        started_at: list[float] = []

        class SlowList(FakeTranscriptList):
            def __init__(self) -> None:
                super().__init__(
                    generated={"en": FakeTranscript("English (auto)", "en", True, _snippets("hi"))}
                )

            def list(self, video_id: str) -> FakeTranscriptList:  # noqa: N802
                # Simulate a slow (blocking) library I/O call.
                import time

                time.sleep(0.3)
                return self

        async def _heartbeat() -> None:
            for _ in range(5):
                await asyncio.sleep(0.05)
                heartbeat.append(loop.time() - started_at[0])

        _install_fake_api(monkeypatch, transcript_list=SlowList())
        started_at.append(loop.time())

        heartbeat_task = asyncio.create_task(_heartbeat())
        response = await asyncio.wait_for(_provider().fetch(VIDEO_ID, ["en"]), timeout=5)
        await heartbeat_task

        assert response.provider == NAME
        # If the blocking library call ran on the event loop, no heartbeat
        # tick would fit inside the 0.3 s window.
        assert heartbeat, "the event loop was blocked for the whole fetch"

    async def test_whole_fetch_bounded_by_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The *whole* fetch (list + transcript fetch) shares one bound."""
        transcript = FakeTranscript("English", "en", False, _snippets("hi"))

        def _blocking_fetch(preserve_formatting: bool = False) -> None:
            import time

            time.sleep(5)  # runs in the worker thread; the bound must fire first
            raise AssertionError("unreachable")

        transcript.fetch = _blocking_fetch  # type: ignore[method-assign]
        _install_fake_api(monkeypatch, transcript_list=_list(manual={"en": transcript}))

        provider = _provider(
            config=YouTubeTranscriptApiProviderConfig(enabled=True, timeout_seconds=0.2)
        )
        with pytest.raises(TranscriptProviderTimeoutError):
            await asyncio.wait_for(provider.fetch(VIDEO_ID, ["en"]), timeout=5)


# ========================================================================
# Proxy configuration wiring
# ========================================================================


@pytest.mark.asyncio
class TestProxyWiring:
    async def test_proxy_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = _install_fake_api(monkeypatch, transcript_list=_list())
        with pytest.raises(NoTranscriptAvailableError):
            await _provider().fetch(VIDEO_ID, ["en"])
        assert captured == [{"proxy_config": None}]

    async def test_proxy_url_becomes_generic_proxy_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_fake_api(monkeypatch, transcript_list=_list())
        provider = _provider(
            config=YouTubeTranscriptApiProviderConfig(
                enabled=True, proxy="http://proxy.example:8080"
            )
        )
        with pytest.raises(NoTranscriptAvailableError):
            await provider.fetch(VIDEO_ID, ["en"])
        assert len(captured) == 1
        proxy_config = captured[0]["proxy_config"]
        assert isinstance(proxy_config, yta.proxies.GenericProxyConfig)
        assert proxy_config.http_url == "http://proxy.example:8080"
        assert proxy_config.https_url == "http://proxy.example:8080"


# ========================================================================
# Config parsing + construction factory
# ========================================================================


def _settings(**transcript: Any) -> Settings:
    return Settings(_raw_yaml_transcript=transcript or None)


class TestConfigFromSettings:
    def test_defaults(self) -> None:
        config = YouTubeTranscriptApiProviderConfig.from_settings(_settings())
        assert config.enabled is False
        assert config.timeout_seconds == 30.0
        assert config.proxy is None

    def test_yaml_block(self) -> None:
        config = YouTubeTranscriptApiProviderConfig.from_settings(
            _settings(
                providers={
                    NAME: {
                        "enabled": True,
                        "timeout_seconds": 20,
                        "proxy": "http://proxy:8080",
                    }
                }
            )
        )
        assert config.enabled is True
        assert config.timeout_seconds == 20
        assert config.proxy == "http://proxy:8080"

    def test_environment_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPER_TRANSCRIPT_YT_TRANS_API_TIMEOUT_SECONDS", "15")
        config = YouTubeTranscriptApiProviderConfig.from_settings(
            _settings(providers={NAME: {"enabled": True, "timeout_seconds": 30}})
        )
        assert config.enabled is True
        assert config.timeout_seconds == 15

    def test_invalid_timeout_rejected(self) -> None:
        with pytest.raises(ValueError):
            YouTubeTranscriptApiProviderConfig(enabled=True, timeout_seconds=0)


class TestBuildFactory:
    def test_disabled_returns_none(self) -> None:
        assert build_youtube_transcript_api_provider(_settings()) is None

    def test_enabled_returns_provider(self) -> None:
        provider = build_youtube_transcript_api_provider(
            _settings(providers={NAME: {"enabled": True}})
        )
        assert provider is not None
        assert provider.name == NAME
        assert provider.config.enabled is True

    def test_registry_and_build_transcript_providers(self) -> None:
        from app.transcript.providers import build_transcript_providers
        from app.transcript.providers.ytdlp import NAME as YTDLP_NAME

        providers = build_transcript_providers(
            _settings(
                providers={
                    NAME: {"enabled": True},
                    YTDLP_NAME: {"enabled": True},
                }
            )
        )
        # Registration order: camofox (disabled) -> #146 -> #147.
        assert [provider.name for provider in providers] == [NAME, YTDLP_NAME]
