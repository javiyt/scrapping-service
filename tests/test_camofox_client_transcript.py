"""Tests for ``CamofoxClient.youtube_transcript`` (``POST /youtube/transcript``).

Fully offline via ``httpx.MockTransport``.  The response/error shapes are
documented from a running Camofox instance (verified 2026-11):

* success (HTTP 200):
  ``{"status": "ok", "transcript": "[00:01] ...", "video_url", "video_id",
  "video_title", "language", "total_words"}``
* no captions (HTTP 200):
  ``{"status": "error", "code": 404,
  "message": "No captions available for this video", ...}``
* missing ``url`` (HTTP 400): ``{"error": "url is required"}``
* wrong credentials (HTTP 401): ``{"error": "Unauthorized"}``

The transcript timeout is client-side (the upstream handler has its own
fixed ``HANDLER_TIMEOUT_MS``); the tests exercise the client-side timeout
and the per-request override.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.camofox.client import CamofoxClient
from app.camofox.config import CamofoxConfig
from app.core.errors import (
    CamofoxAuthError,
    CamofoxError,
    CamofoxInvalidResponseError,
    CamofoxTimeoutError,
    CamofoxUnavailableError,
)

BASE_URL = "http://camofox:9377"
VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

_OK_PAYLOAD = {
    "status": "ok",
    "transcript": "[00:01] hello\n[00:03] world",
    "video_url": VIDEO_URL,
    "video_id": "dQw4w9WgXcQ",
    "video_title": "A Title",
    "language": "en",
    "total_words": 5,
}

_NO_CAPTIONS_PAYLOAD = {
    "status": "error",
    "code": 404,
    "message": "No captions available for this video",
    "video_url": VIDEO_URL,
    "video_id": "dQw4w9WgXcQ",
    "title": "",
}


def _config(**overrides) -> CamofoxConfig:
    values = dict(
        enabled=True,
        base_url=BASE_URL,
        timeout_seconds=10.0,
        access_key="tok-access",
    )
    values.update(overrides)
    return CamofoxConfig(**values)


def _transport(handler) -> httpx.MockTransport:
    async def _handle(request: httpx.Request) -> httpx.Response:
        return handler(request)

    return httpx.MockTransport(_handle)


def _client(handler) -> CamofoxClient:
    return CamofoxClient(
        _config(), client=httpx.AsyncClient(base_url=BASE_URL, transport=_transport(handler))
    )


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )


class TestYoutubeTranscript:
    @pytest.mark.asyncio
    async def test_success_parses_all_fields(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/youtube/transcript"
            body = json.loads(request.content.decode())
            assert body["url"] == VIDEO_URL
            assert body["languages"] == ["en", "es"]
            assert request.headers.get("authorization") == "Bearer tok-access"
            return _json(_OK_PAYLOAD)

        client = _client(handler)
        result = await client.youtube_transcript(VIDEO_URL, ["en", "es"])
        assert result.ok is True
        assert result.status == "ok"
        assert result.transcript == "[00:01] hello\n[00:03] world"
        assert result.video_id == "dQw4w9WgXcQ"
        assert result.video_title == "A Title"
        assert result.language == "en"
        assert result.total_words == 5

    @pytest.mark.asyncio
    async def test_defaults_to_english_when_no_languages(self):
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return _json(_OK_PAYLOAD)

        client = _client(handler)
        await client.youtube_transcript(VIDEO_URL)
        assert captured["body"]["languages"] == ["en"]

    @pytest.mark.asyncio
    async def test_no_captions_returns_error_result(self):
        # The server returns HTTP 200 with status=error for no captions.
        client = _client(lambda request: _json(_NO_CAPTIONS_PAYLOAD))
        result = await client.youtube_transcript(VIDEO_URL, ["en"])
        assert result.ok is False
        assert result.status == "error"
        assert result.code == 404
        assert result.message == "No captions available for this video"
        assert result.transcript is None

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self):
        client = _client(lambda request: _json({"error": "Unauthorized"}, status=401))
        with pytest.raises(CamofoxAuthError):
            await client.youtube_transcript(VIDEO_URL, ["en"])

    @pytest.mark.asyncio
    async def test_403_raises_auth_error(self):
        client = _client(lambda request: _json({"error": "Forbidden"}, status=403))
        with pytest.raises(CamofoxAuthError):
            await client.youtube_transcript(VIDEO_URL, ["en"])

    @pytest.mark.asyncio
    async def test_400_raises_generic_camofox_error(self):
        client = _client(lambda request: _json({"error": "url is required"}, status=400))
        with pytest.raises(CamofoxError) as err:
            await client.youtube_transcript(VIDEO_URL, ["en"])
        assert err.value.details["status_code"] == 400

    @pytest.mark.asyncio
    async def test_500_raises_unavailable(self):
        client = _client(lambda request: _json({"error": "kaboom"}, status=500))
        with pytest.raises(CamofoxUnavailableError):
            await client.youtube_transcript(VIDEO_URL, ["en"])

    @pytest.mark.asyncio
    async def test_server_timeout_500_marker_raises_timeout(self):
        client = _client(lambda request: _json({"error": "request timed out"}, status=500))
        with pytest.raises(CamofoxTimeoutError):
            await client.youtube_transcript(VIDEO_URL, ["en"])

    @pytest.mark.asyncio
    async def test_client_side_timeout_raises_timeout(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        client = _client(handler)
        with pytest.raises(CamofoxTimeoutError) as err:
            await client.youtube_transcript(VIDEO_URL, ["en"])
        assert err.value.details["endpoint"] == "/youtube/transcript"

    @pytest.mark.asyncio
    async def test_per_request_timeout_override_is_used(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        client = _client(handler)
        with pytest.raises(CamofoxTimeoutError):
            # The override must not break the request path.
            await client.youtube_transcript(VIDEO_URL, ["en"], timeout_seconds=0.01)

    @pytest.mark.asyncio
    async def test_connection_error_raises_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = _client(handler)
        with pytest.raises(CamofoxUnavailableError):
            await client.youtube_transcript(VIDEO_URL, ["en"])

    @pytest.mark.asyncio
    async def test_invalid_json_raises_invalid_response(self):
        client = _client(
            lambda request: httpx.Response(
                200, content=b"not-json", headers={"content-type": "application/json"}
            )
        )
        with pytest.raises(CamofoxInvalidResponseError):
            await client.youtube_transcript(VIDEO_URL, ["en"])

    @pytest.mark.asyncio
    async def test_non_object_json_raises_invalid_response(self):
        client = _client(lambda request: _json(["a", "list"]))
        with pytest.raises(CamofoxInvalidResponseError):
            await client.youtube_transcript(VIDEO_URL, ["en"])

    @pytest.mark.asyncio
    async def test_missing_transcript_text_raises_invalid_response(self):
        payload = dict(_OK_PAYLOAD)
        del payload["transcript"]
        client = _client(lambda request: _json(payload))
        with pytest.raises(CamofoxInvalidResponseError):
            await client.youtube_transcript(VIDEO_URL, ["en"])

    @pytest.mark.asyncio
    async def test_unknown_status_raises_invalid_response(self):
        client = _client(lambda request: _json({"status": "weird", "message": "no idea"}))
        with pytest.raises(CamofoxInvalidResponseError):
            await client.youtube_transcript(VIDEO_URL, ["en"])
