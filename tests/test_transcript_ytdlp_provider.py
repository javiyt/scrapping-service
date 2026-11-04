"""Tests for the yt-dlp transcript provider (ticket #147).

Covers:

- the json3 / WebVTT parsers with real-world fixtures (including the
  rolling-caption duplicates automatic captions produce, inline tags,
  ``HH:MM:SS`` timestamps, and malformed lines);
- the provider's track selection (manual vs automatic, requested-language
  order, json3 vs vtt) and DownloadError -> project-error mapping, with
  ``yt_dlp.YoutubeDL`` and the caption HTTP download faked in memory;
- the config parsing (YAML block + environment overrides) and the
  construction factory;
- no temp files left behind (the provider creates none by design — the
  temp-dir contents are asserted before and after a fetch).

``yt-dlp`` is imported at module import time, so a missing installation
fails the whole test session; it is a pinned runtime dependency
(see ``requirements.txt``).
"""

from __future__ import annotations

import json
import tempfile
from typing import Any

import httpx
import pytest
import yt_dlp

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
from app.transcript.providers.ytdlp import (
    NAME,
    YtDlpTranscriptProvider,
    YtDlpTranscriptProviderConfig,
    _map_ytdlp_error,
    _select_track,
    _vtt_timestamp_to_seconds,
    build_ytdlp_transcript_provider,
    parse_json3,
    parse_webvtt,
)


def _provider(
    *,
    config: YtDlpTranscriptProviderConfig | None = None,
) -> YtDlpTranscriptProvider:
    return YtDlpTranscriptProvider(config or YtDlpTranscriptProviderConfig(enabled=True))


def _download_info(**info: Any) -> dict[str, Any]:
    return dict(info)


def _install_fake_ytdlp(monkeypatch: pytest.MonkeyPatch, extract_info: Any) -> None:
    """Patch ``yt_dlp.YoutubeDL`` with an in-memory stand-in.

    The real class is never touched; ``extract_info(url, download=...)`` is
    a plain function (or exception factory) supplied per test.
    """

    class FakeYoutubeDL:
        def __init__(self, params: dict[str, Any]) -> None:
            self.params = dict(params)

        def __enter__(self) -> FakeYoutubeDL:
            return self

        def __exit__(self, *exc_info: Any) -> None:
            return None

        def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
            return extract_info(url, download)

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)


def _install_fake_downloader(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes | Exception | Any,
    *,
    url: str = "https://www.youtube.com/api/timedtext?lang=en",
) -> None:
    """Patch ``httpx.AsyncClient.get`` to return ``payload`` (or raise).

    ``payload`` may also be a ``callable(request_url) -> bytes`` for tests
    that verify which caption URL was requested.
    """

    async def fake_get(self: Any, request_url: str, **kwargs: Any) -> httpx.Response:
        if isinstance(payload, Exception):
            raise payload
        content = payload(request_url) if callable(payload) else payload
        return httpx.Response(
            200,
            content=content,
            request=httpx.Request("GET", request_url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


# ========================================================================
# parse_json3
# ========================================================================


class TestParseJson3:
    def test_simple_single_event(self) -> None:
        payload = json.dumps(
            {
                "events": [
                    {
                        "tStartMs": 1000,
                        "dDurationMs": 2500,
                        "seg": [{"utf8": "héllo", "s": 0}, {"utf8": "wörld", "s": 1200}],
                    }
                ]
            }
        ).encode("utf-8")
        segments = parse_json3(payload)
        assert segments == [TranscriptSegment(start=1.0, duration=2.5, text="héllo wörld")]

    def test_accepts_str_and_bytes(self) -> None:
        document = {"events": [{"tStartMs": 0, "dDurationMs": 100, "seg": [{"utf8": "hi"}]}]}
        assert parse_json3(json.dumps(document)) == parse_json3(
            json.dumps(document).encode("utf-8")
        )

    def test_multiple_events_preserve_order_and_gaps(self) -> None:
        payload = json.dumps(
            {
                "events": [
                    {"tStartMs": 0, "dDurationMs": 3000, "seg": [{"utf8": "one"}, {"utf8": "two"}]},
                    {"tStartMs": 5000, "dDurationMs": 1000, "seg": [{"utf8": "three"}]},
                ]
            }
        ).encode("utf-8")
        segments = parse_json3(payload)
        assert [segment.text for segment in segments] == ["one two", "three"]
        assert [segment.start for segment in segments] == [0.0, 5.0]

    def test_missing_duration_yields_none(self) -> None:
        payload = json.dumps({"events": [{"tStartMs": 2000, "seg": [{"utf8": "text"}]}]}).encode(
            "utf-8"
        )
        assert parse_json3(payload) == [TranscriptSegment(start=2.0, duration=None, text="text")]

    def test_empty_seg_event_is_skipped(self) -> None:
        payload = json.dumps(
            {
                "events": [
                    {"tStartMs": 0, "dDurationMs": 100, "seg": []},
                    {"tStartMs": 0, "dDurationMs": 100, "seg": [{"utf8": ""}, {"utf8": "  "}]},
                    {"tStartMs": 1000, "seg": [{"utf8": "kept"}]},
                ]
            }
        ).encode("utf-8")
        assert parse_json3(payload) == [TranscriptSegment(start=1.0, duration=None, text="kept")]

    def test_non_utf8_bytes_yield_empty_list(self) -> None:
        assert parse_json3(b"\xff\xfe\x00\x01") == []

    def test_invalid_json_yields_empty_list(self) -> None:
        assert parse_json3(b"this is not json") == []

    def test_missing_events_key_yields_empty_list(self) -> None:
        assert parse_json3(b"{}") == []
        assert parse_json3(b'"just a string"') == []

    def test_malformed_event_fields_are_skipped(self) -> None:
        payload = json.dumps(
            {
                "events": [
                    "not-a-dict",
                    {"tStartMs": "not-a-number", "seg": [{"utf8": "x"}]},
                    {"tStartMs": 0, "seg": "not-a-list"},
                    {"tStartMs": 0, "dDurationMs": "bad", "seg": [{"utf8": "ok"}]},
                ]
            }
        ).encode("utf-8")
        assert parse_json3(payload) == [TranscriptSegment(start=0.0, duration=None, text="ok")]


# ========================================================================
# parse_webvtt
# ========================================================================

#: A realistic fragment of YouTube *automatic* captions: the rolling ~3 s
#: windows repeat the previous phrase, inline style tags are present, and
#: the second cue is a verbatim repeat of the first.
_ROLLING_VTT = """WEBVTT
Kind: captions
Language: en-US

00:00:00.000 --> 00:00:03.000
<c>hello</c> world
00:00:03.000 --> 00:00:06.000
hello world
00:00:06.000 --> 00:00:09.000
hello world this
00:00:09.000 --> 00:00:12.000
hello world this is
00:00:12.000 --> 00:00:15.000
world this is new
00:00:15.000 --> 00:00:18.000
this is new to
00:00:18.000 --> 00:00:21.000
is new to me
"""

_ROLLING_EXPECTED = [
    (0.0, "hello world"),
    (6.0, "hello world this"),
    (9.0, "hello world this is"),
    (12.0, "world this is new"),
    (15.0, "this is new to"),
    (18.0, "is new to me"),
]


class TestParseWebvtt:
    def test_rolling_dedup(self) -> None:
        segments = parse_webvtt(_ROLLING_VTT)
        got = [(segment.start, segment.text) for segment in segments]
        assert got == _ROLLING_EXPECTED
        # The first cue ("hello") was a prefix of the next and the second
        # ("hello world") a prefix of the third: both dropped.
        assert all(segment.text != "hello" for segment in segments)

    def test_durations_are_gaps_to_next_kept_cue(self) -> None:
        segments = parse_webvtt(_ROLLING_VTT)
        for index, segment in enumerate(segments):
            if index + 1 < len(segments):
                assert segment.duration == pytest.approx(segments[index + 1].start - segment.start)
            else:
                assert segment.duration is None

    def test_strips_header_and_tags(self) -> None:
        payload = (
            "WEBVTT\n"
            "Kind: captions\n"
            "Language: es\n"
            "\n"
            "1\n"
            "00:01:00.000 --> 00:01:03.000 align: middle\n"
            "<c>hola</c> <c color='#ff0000'>mundo</c>\n"
        )
        segments = parse_webvtt(payload)
        assert segments == [TranscriptSegment(start=60.0, duration=None, text="hola mundo")]

    def test_hours_timestamps(self) -> None:
        payload = "WEBVTT\n\n01:02:03.500 --> 01:02:06.000\nlate cue\n"
        segments = parse_webvtt(payload)
        assert segments == [TranscriptSegment(start=3723.5, duration=None, text="late cue")]

    def test_multiline_cue(self) -> None:
        payload = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nfirst line\nsecond line\n"
        segments = parse_webvtt(payload)
        assert segments == [
            TranscriptSegment(start=1.0, duration=None, text="first line second line")
        ]

    def test_malformed_timestamps_are_skipped(self) -> None:
        payload = (
            "WEBVTT\n\n"
            "999.5 --> 00:00:02.000\n"
            "dropped cue\n"
            "\n"
            "00:00:05.000 --> 00:00:08.000\n"
            "kept cue\n"
        )
        segments = parse_webvtt(payload)
        assert segments == [TranscriptSegment(start=5.0, duration=None, text="kept cue")]

    def test_text_before_any_cue_is_ignored(self) -> None:
        payload = "WEBVTT\n\nstray line with no cue\n\n00:00:02.000 --> 00:00:05.000\nreal cue\n"
        segments = parse_webvtt(payload)
        assert segments == [TranscriptSegment(start=2.0, duration=None, text="real cue")]

    def test_note_block_is_ignored(self) -> None:
        payload = (
            "WEBVTT\n\nNOTE a comment\nNOTE more\n\n00:00:01.000 --> 00:00:02.000\nafter note\n"
        )
        segments = parse_webvtt(payload)
        assert segments == [TranscriptSegment(start=1.0, duration=None, text="after note")]

    def test_empty_payload(self) -> None:
        assert parse_webvtt("") == []
        assert parse_webvtt("WEBVTT\n\nKind: captions\n") == []
        assert parse_webvtt(b"WEBVTT") == []

    def test_bytes_input(self) -> None:
        assert parse_webvtt(_ROLLING_VTT.encode("utf-8")) == parse_webvtt(_ROLLING_VTT)

    def test_adjacent_verbatim_repeats_collapse(self) -> None:
        payload = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:03.000\n"
            "same text\n"
            "00:00:03.000 --> 00:00:06.000\n"
            "same text\n"
            "00:00:06.000 --> 00:00:09.000\n"
            "different\n"
        )
        segments = parse_webvtt(payload)
        assert [segment.text for segment in segments] == ["same text", "different"]

    def test_cue_containing_next_is_kept(self) -> None:
        # A cue that *contains* the next one (the window scrolled a phrase
        # out) is not a suffix of it: both are kept, nothing is lost.
        payload = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:03.000\n"
            "alpha beta gamma\n"
            "00:00:03.000 --> 00:00:06.000\n"
            "alpha beta\n"
        )
        segments = parse_webvtt(payload)
        assert [segment.text for segment in segments] == ["alpha beta"]


class TestVttTimestamp:
    def test_minutes_form(self) -> None:
        assert _vtt_timestamp_to_seconds("01:30.500") == pytest.approx(90.5)

    def test_hours_form(self) -> None:
        assert _vtt_timestamp_to_seconds("01:02:03.500") == pytest.approx(3723.5)

    def test_malformed(self) -> None:
        assert _vtt_timestamp_to_seconds("99:99.5") is None
        assert _vtt_timestamp_to_seconds("1:2:3:4") is None
        assert _vtt_timestamp_to_seconds("abc") is None


# ========================================================================
# Track selection from the yt-dlp info dict
# ========================================================================


class TestSelectTrack:
    INFO = {
        "subtitles": {
            "es": {"vtt": "url-es-vtt", "json3": "url-es-json3", "srv1": "url-es-srv1"},
            "de-DE": {"vtt": "url-de-vtt"},
        },
        "automatic_captions": {
            "en-US": {"vtt": "url-en-vtt"},
            "fr": {"json3": "url-fr-json3"},
        },
    }

    def test_manual_preferred_over_automatic(self) -> None:
        assert _select_track(self.INFO, ["es"]) == ("es", "json3", False)

    def test_automatic_when_no_manual(self) -> None:
        assert _select_track(self.INFO, ["en"]) == ("en-US", "vtt", True)

    def test_language_order_is_honoured(self) -> None:
        assert _select_track(self.INFO, ["de", "en"]) == ("de-DE", "vtt", False)
        assert _select_track(self.INFO, ["en", "de"]) == ("en-US", "vtt", True)

    def test_prefix_matching(self) -> None:
        assert _select_track(self.INFO, ["de"]) == ("de-DE", "vtt", False)

    def test_json3_preferred_over_vtt(self) -> None:
        assert _select_track(self.INFO, ["fr"]) == ("fr", "json3", True)

    def test_no_match_returns_none(self) -> None:
        assert _select_track(self.INFO, ["ja"]) is None
        assert _select_track({}, ["en"]) is None

    def test_empty_tables(self) -> None:
        assert _select_track({"subtitles": None, "automatic_captions": None}, ["en"]) is None


# ========================================================================
# Error mapping
# ========================================================================


class FakeDownloadError(Exception):
    """Stand-in for ``yt_dlp.utils.DownloadError`` (string is the only
    thing the mapping consumes)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class TestMapYtdlpError:
    def test_timeout(self) -> None:
        exc = _map_ytdlp_error(TimeoutError("timed out"), video_id="a" * 11, languages=["en"])
        assert isinstance(exc, TranscriptProviderTimeoutError)

    def test_timeout_marker_in_message(self) -> None:
        exc = _map_ytdlp_error(
            FakeDownloadError("E: [youtube] xxx: Request to ... timed out"),
            video_id="a" * 11,
            languages=["en"],
        )
        assert isinstance(exc, TranscriptProviderTimeoutError)

    def test_block_markers(self) -> None:
        for message in (
            "E: [youtube] xxx: The video is only available for signed-in users",
            "ERROR: 403 Forbidden",
            "rate limit exceeded",
            "E: [youtube] xxx: blocked",
        ):
            exc = _map_ytdlp_error(FakeDownloadError(message), video_id="a" * 11, languages=["en"])
            assert isinstance(exc, TranscriptProviderBlockedError), message

    def test_video_gone_markers(self) -> None:
        for message in (
            "E: [youtube] xxx: Video unavailable",
            "The video is private",
            "This video has been removed",
        ):
            exc = _map_ytdlp_error(FakeDownloadError(message), video_id="a" * 11, languages=["en"])
            assert isinstance(exc, VideoUnavailableError), message

    def test_no_captions_markers(self) -> None:
        for message in (
            "E: [youtube] xxx: unable to download webpage: no subtitles",
            "no captions found for this video",
        ):
            exc = _map_ytdlp_error(FakeDownloadError(message), video_id="a" * 11, languages=["en"])
            assert isinstance(exc, NoTranscriptAvailableError), message

    def test_unknown_degrades_to_unavailable(self) -> None:
        exc = _map_ytdlp_error(
            FakeDownloadError("E: something entirely unexpected"),
            video_id="a" * 11,
            languages=["en"],
        )
        assert isinstance(exc, TranscriptProviderUnavailableError)

    def test_public_error_never_carries_raw_output(self) -> None:
        raw = "E: [youtube] dQw4w9WgXcQ: some secret upstream text proxy=http://u:p@internal:1"
        exc = _map_ytdlp_error(FakeDownloadError(raw), video_id="a" * 11, languages=["en"])
        assert "internal" not in str(exc)
        assert "secret" not in str(exc)


# ========================================================================
# Provider fetch (YoutubeDL + caption download faked)
# ========================================================================


class TestProviderFetch:
    VIDEO_ID = "dQw4w9WgXcQ"

    def _info(
        self,
        *,
        subtitles: dict[str, Any] | None = None,
        automatic_captions: dict[str, Any] | None = None,
        title: str | None = "Test Title",
    ) -> dict[str, Any]:
        return _download_info(
            title=title, subtitles=subtitles, automatic_captions=automatic_captions
        )

    @pytest.mark.asyncio
    async def test_manual_track_preferred_and_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_ytdlp(
            monkeypatch,
            lambda url, download: self._info(
                subtitles={"es": {"vtt": "u-es-vtt", "json3": "u-es-json3"}},
                automatic_captions={"en-US": {"vtt": "u-en-vtt"}},
            ),
        )
        seen_urls: list[str] = []
        payloads = {
            "u-es-json3": json.dumps(
                {"events": [{"tStartMs": 1000, "dDurationMs": 2000, "seg": [{"utf8": "hola"}]}]}
            ).encode("utf-8"),
            "u-es-vtt": b"WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nunwanted vtt\n",
            "u-en-vtt": b"WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nunwanted auto\n",
        }

        def _by_url(request_url: str) -> bytes:
            seen_urls.append(request_url)
            return payloads[request_url]

        _install_fake_downloader(monkeypatch, _by_url)

        # The request is ["en", "es"]: the *first* requested language that
        # the video offers wins ("en", via the automatic track).  Manual
        # preference applies *within* a requested language (see
        # TestSelectTrack), it does not override the language order.
        response = await _provider().fetch(self.VIDEO_ID, ["en", "es"])

        assert seen_urls == ["u-en-vtt"]  # the automatic "en" track
        assert response.provider == NAME
        assert response.title == "Test Title"
        assert response.language == "en-US"  # the first requested language offered
        assert response.is_generated is True
        assert response.text == "unwanted auto"
        assert response.metadata.provider == NAME

    @pytest.mark.asyncio
    async def test_automatic_fallback_with_vtt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_ytdlp(
            monkeypatch,
            lambda url, download: self._info(
                subtitles={"de": {"vtt": "u-de"}},
                automatic_captions={"en-US": {"vtt": "u-en"}},
                title=None,
            ),
        )
        vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:03.000\n<c>hi</c> there\n"
        _install_fake_downloader(monkeypatch, vtt.encode("utf-8"))

        response = await _provider().fetch(self.VIDEO_ID, ["en"])

        assert response.language == "en-US"
        assert response.is_generated is True
        assert response.title is None
        assert response.text == "hi there"

    @pytest.mark.asyncio
    async def test_requested_language_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_ytdlp(
            monkeypatch,
            lambda url, download: self._info(
                subtitles={"es": {"vtt": "u-es"}, "fr": {"vtt": "u-fr"}},
            ),
        )
        seen_urls: list[str] = []

        def _by_url(request_url: str) -> bytes:
            seen_urls.append(request_url)
            return b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nop\n"

        _install_fake_downloader(monkeypatch, _by_url)

        response = await _provider().fetch(self.VIDEO_ID, ["fr", "es"])
        assert response.language == "fr"
        assert seen_urls == ["u-fr"]

    @pytest.mark.asyncio
    async def test_missing_language_raises_language_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_ytdlp(
            monkeypatch,
            lambda url, download: self._info(
                subtitles={"es": {"vtt": "u-es"}},
                automatic_captions={"en-US": {"vtt": "u-en"}},
            ),
        )
        with pytest.raises(TranscriptLanguageUnavailableError) as exc_info:
            await _provider().fetch(self.VIDEO_ID, ["ja"])
        details = exc_info.value.details or {}
        assert details["available"] == ["es", "en-US"]
        assert details["requested"] == ["ja"]

    @pytest.mark.asyncio
    async def test_no_captions_at_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_ytdlp(monkeypatch, lambda url, download: self._info())
        with pytest.raises(NoTranscriptAvailableError):
            await _provider().fetch(self.VIDEO_ID, ["en"])

    @pytest.mark.asyncio
    async def test_download_error_is_mapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(url: str, download: bool) -> dict[str, Any]:
            raise FakeDownloadError("E: [youtube] xxx: Video unavailable")

        _install_fake_ytdlp(monkeypatch, _boom)
        with pytest.raises(VideoUnavailableError):
            await _provider().fetch(self.VIDEO_ID, ["en"])

    @pytest.mark.asyncio
    async def test_unexpected_download_error_maps_to_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(url: str, download: bool) -> dict[str, Any]:
            raise RuntimeError("something internal exploded")

        _install_fake_ytdlp(monkeypatch, _boom)
        with pytest.raises(TranscriptProviderUnavailableError):
            await _provider().fetch(self.VIDEO_ID, ["en"])

    @pytest.mark.asyncio
    async def test_metadata_timeout_maps_to_timeout_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(url: str, download: bool) -> dict[str, Any]:
            raise TimeoutError("metadata request timed out")

        _install_fake_ytdlp(monkeypatch, _boom)
        provider = _provider(config=YtDlpTranscriptProviderConfig(enabled=True, timeout_seconds=30))
        with pytest.raises(TranscriptProviderTimeoutError):
            await provider.fetch(self.VIDEO_ID, ["en"])

    @pytest.mark.asyncio
    async def test_whole_fetch_bounded_by_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole fetch (metadata *and* caption download) is bounded.

        The metadata step returns quickly; the *caption download* sleeps
        past the timeout, so the timeout must fire there and surface
        :class:`TranscriptProviderTimeoutError` (503), not hang and not
        502.
        """
        import asyncio

        async def _metadata(url: str, download: bool = True) -> dict[str, Any]:
            await asyncio.sleep(0.05)
            return self._info(subtitles={"en": {"vtt": "u"}})

        _install_fake_ytdlp(monkeypatch, _metadata)

        async def _slow_get(self: Any, request_url: str, **kwargs: Any) -> httpx.Response:
            await asyncio.sleep(5)  # the download is what exceeds the budget
            return httpx.Response(
                200, content=b"WEBVTT\n", request=httpx.Request("GET", request_url)
            )

        monkeypatch.setattr(httpx.AsyncClient, "get", _slow_get)
        provider = _provider(
            config=YtDlpTranscriptProviderConfig(enabled=True, timeout_seconds=0.1)
        )
        with pytest.raises(TranscriptProviderTimeoutError):
            await provider.fetch(self.VIDEO_ID, ["en"])

    @pytest.mark.asyncio
    async def test_caption_download_failure_maps_to_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_ytdlp(
            monkeypatch,
            lambda url, download: self._info(subtitles={"en": {"vtt": "u-en"}}),
        )
        _install_fake_downloader(monkeypatch, httpx.ConnectError("refused"))
        with pytest.raises(TranscriptProviderUnavailableError):
            await _provider().fetch(self.VIDEO_ID, ["en"])

    @pytest.mark.asyncio
    async def test_unparseable_payload_maps_to_invalid_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_ytdlp(
            monkeypatch,
            lambda url, download: self._info(subtitles={"en": {"vtt": "u-en"}}),
        )
        _install_fake_downloader(monkeypatch, b"not a vtt file at all")
        with pytest.raises(TranscriptProviderInvalidResponseError):
            await _provider().fetch(self.VIDEO_ID, ["en"])

    @pytest.mark.asyncio
    async def test_empty_vtt_payload_maps_to_invalid_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_ytdlp(
            monkeypatch,
            lambda url, download: self._info(subtitles={"en": {"vtt": "u-en"}}),
        )
        _install_fake_downloader(monkeypatch, b"WEBVTT\nKind: captions\n")
        with pytest.raises(TranscriptProviderInvalidResponseError):
            await _provider().fetch(self.VIDEO_ID, ["en"])

    @pytest.mark.asyncio
    async def test_missing_languages_rejected(self) -> None:
        with pytest.raises(ValidationError):
            await _provider().fetch(self.VIDEO_ID, [])

    @pytest.mark.asyncio
    async def test_health_is_available(self) -> None:
        health = await _provider().health()
        assert health.available is True
        assert health.reason is None

    @pytest.mark.asyncio
    async def test_no_temp_files_left_behind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The provider must not create temp files: the temp dir is empty
        before and after a full fetch."""
        before = set(tempfile.gettempdir())

        _install_fake_ytdlp(
            monkeypatch,
            lambda url, download: self._info(subtitles={"en": {"vtt": "u-en"}}),
        )
        vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:03.000\nhi\n"
        _install_fake_downloader(monkeypatch, vtt.encode("utf-8"))

        response = await _provider().fetch(self.VIDEO_ID, ["en"])
        assert response.text == "hi"

        after = set(tempfile.gettempdir())
        assert after - before == set()

    @pytest.mark.asyncio
    async def test_ytdlp_options_never_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The options handed to YoutubeDL disable all file writing."""
        captured: dict[str, dict[str, Any]] = {}

        class FakeYoutubeDL:
            def __init__(self, params: dict[str, Any]) -> None:
                captured["params"] = dict(params)

            def __enter__(self) -> FakeYoutubeDL:
                return self

            def __exit__(self, *exc_info: Any) -> None:
                return None

            def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
                assert download is False
                info = _download_info(subtitles={"en": {"vtt": "u-en"}})
                return info

        monkeypatch.setattr(yt_dlp, "YoutubeDL", FakeYoutubeDL)
        _install_fake_downloader(monkeypatch, b"WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nx\n")

        await _provider().fetch(self.VIDEO_ID, ["en"])

        params = captured["params"]
        assert params["quiet"] is True
        assert params["no_warnings"] is True
        assert params["no_playlist"] is True
        assert params["noplaylist"] is True  # deprecated alias for old releases
        assert params["skip_download"] is True
        assert params["subtitleslangs"] == ["en"]
        assert "cookiefile" not in params  # no cookies by default
        assert "proxy" not in params


# ========================================================================
# Config parsing + construction factory
# ========================================================================


def _settings(**transcript: Any) -> Settings:
    return Settings(_raw_yaml_transcript=transcript or None)


class TestConfigFromSettings:
    def test_defaults(self) -> None:
        config = YtDlpTranscriptProviderConfig.from_settings(_settings())
        assert config.enabled is False
        assert config.timeout_seconds == 60.0
        assert config.proxy is None
        assert config.cookies is None

    def test_yaml_block(self) -> None:
        config = YtDlpTranscriptProviderConfig.from_settings(
            _settings(
                providers={
                    NAME: {
                        "enabled": True,
                        "timeout_seconds": 30,
                        "proxy": "http://proxy:8080",
                        "cookies": "/tmp/cookies.txt",
                    }
                }
            )
        )
        assert config.enabled is True
        assert config.timeout_seconds == 30
        assert config.proxy == "http://proxy:8080"
        assert config.cookies == "/tmp/cookies.txt"

    def test_environment_overrides_yaml(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPER_TRANSCRIPT_YT_DLP_TIMEOUT_SECONDS", "15")
        config = YtDlpTranscriptProviderConfig.from_settings(
            _settings(providers={NAME: {"enabled": True, "timeout_seconds": 30}})
        )
        assert config.enabled is True
        assert config.timeout_seconds == 15

    def test_invalid_timeout_rejected(self) -> None:
        with pytest.raises(ValueError):
            YtDlpTranscriptProviderConfig(enabled=True, timeout_seconds=0)


class TestBuildFactory:
    def test_disabled_returns_none(self) -> None:
        assert build_ytdlp_transcript_provider(_settings()) is None

    def test_enabled_returns_provider(self) -> None:
        provider = build_ytdlp_transcript_provider(_settings(providers={NAME: {"enabled": True}}))
        assert provider is not None
        assert provider.name == NAME
        assert provider.config.enabled is True

    def test_registry_and_build_transcript_providers(self) -> None:
        from app.transcript.providers import build_transcript_providers

        providers = build_transcript_providers(_settings(providers={NAME: {"enabled": True}}))
        assert [provider.name for provider in providers] == [NAME]
