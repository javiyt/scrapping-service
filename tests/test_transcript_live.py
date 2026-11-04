"""Optional live integration test for the transcript API (ticket #148).

Hits a real, public, permanently-captioned YouTube video through the full
stack (route -> cache -> router -> the *real* configured provider(s)).
This exercises the part no fake can: provider construction from the
``transcript`` config, the live health probe, and a genuine fetch.

Skipped by default; enabled with the ``SCRAPER_TEST_TRANSCRIPT_LIVE=true``
environment variable (CI never sets it, so the suite stays hermetic).  The
target video can be overridden with ``SCRAPER_TEST_TRANSCRIPT_VIDEO``.

Note: a live run needs *some* transcript provider enabled in the running
configuration (``transcript.providers.*``); with none enabled the single
test fails with a clear 503 rather than being skipped — that is the
correct signal that the deployment is missing a transcript backend.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.test_transcript_api import AUTH_HEADER, VALID_API_KEY

#: A public video that has had stable, non-generated English captions since
#: 2012 — a safe, well-known target for a smoke test.
LIVE_VIDEO_URL = os.environ.get(
    "SCRAPER_TEST_TRANSCRIPT_VIDEO", "https://www.youtube.com/watch?v=jNQXAC9IVRw"
)


def _live_enabled() -> bool:
    return os.environ.get("SCRAPER_TEST_TRANSCRIPT_LIVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


@pytest.mark.skipif(
    not _live_enabled(),
    reason="live transcript integration test is opt-in (SCRAPER_TEST_TRANSCRIPT_LIVE=true)",
)
class TestLiveTranscript:
    def test_live_fetch_returns_normalized_transcript(self) -> None:
        from app.core.config import Settings

        app.state.settings = Settings(_env_file=None, api_key=VALID_API_KEY)

        client = TestClient(app)
        response = client.get(
            f"/v1/transcript?url={LIVE_VIDEO_URL}&lang=en&force_refresh=true",
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200, (
            f"live transcript failed (a transcript provider must be enabled): {response.text[:500]}"
        )
        body = response.json()
        assert body["video_id"]
        assert body["provider"]
        assert len(body["segments"]) > 0
        assert body["text"].strip()
        assert body["total_words"] > 0
