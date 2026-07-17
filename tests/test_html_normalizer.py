"""Unit tests for the HTML normaliser.

Tests all normalisation features in isolation and verifies that the
raw HTML is returned unchanged when normalisation is disabled.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app

# ===================================================================== helpers

VALID_API_KEY = "test-key-for-tests"
AUTH_HEADER = {"Authorization": f"Bearer {VALID_API_KEY}"}

SAMPLE_RESULT_WITH_LINKS = {
    "url": "https://example.com/page",
    "final_url": "https://example.com/page",
    "status_code": 200,
    "from_cache": False,
    "stale": False,
    "fetched_at": "2026-06-30T10:00:00+00:00",
    "expires_at": "2026-06-30T16:00:00+00:00",
    "html": (
        "<html><head>"
        '<meta charset="utf-8">'
        '<meta name="description" content="test">'
        "</head><body>"
        "<h1>  Hello   World  </h1>"
        "<p>This is   a    test</p>"
        '<script>alert("xss")</script>'
        "<style>body { color: red; }</style>"
        "<!-- comment here -->"
        "<noscript>JS required</noscript>"
        '<div data-reactroot="" data-id="42">tracked</div>'
        "<div hidden>hidden attr</div>"
        '<div aria-hidden="true">aria hidden</div>'
        '<div style="display:none">style hidden</div>'
        "<template>template hidden</template>"
        '<a href="/relative">link</a>'
        '<img src="/img.png" srcset="/img-320.png 320w, /img-640.png 640w">'
        '<picture><source srcset="/hero.webp"><img src="/hero.png"></picture>'
        "<svg><path></path></svg>"
        '<iframe src="/frame"></iframe>'
        '<audio src="/clip.mp3"></audio>'
        '<form action="/submit"></form>'
        '<video poster="/poster.jpg"></video>'
        "</body></html>"
    ),
    "metadata": {
        "mode": "http",
        "elapsed_ms": 120,
        "content_length": 438,
        "cache_key": "abc123",
    },
}

SAMPLE_RESULT_SIMPLE = {
    "url": "https://example.com",
    "final_url": "https://example.com",
    "status_code": 200,
    "from_cache": False,
    "stale": False,
    "fetched_at": "2026-06-30T10:00:00+00:00",
    "expires_at": "2026-06-30T16:00:00+00:00",
    "html": "<html><body><h1>Hello</h1></body></html>",
    "metadata": {
        "mode": "http",
        "elapsed_ms": 120,
        "content_length": 43,
        "cache_key": "abc123",
    },
}


@pytest.fixture(autouse=True)
def _clean_app_state():
    """Reset app.state before each test."""
    for attr in ("scraper", "cache", "settings"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def _mock_scraper(return_value=None):
    mock = AsyncMock()
    if return_value is not None:
        mock.scrape.return_value = return_value
    return mock


def _patch_scraper(mock):
    app.state.scraper = mock
    app.state.cache = MagicMock()
    return TestClient(app)


# ========================================================== Disabled normalisation


class TestDisabledNormalization:
    """Normalisation disabled → original HTML returned untouched."""

    def test_no_normalize_field_returns_original_html(self):
        """When normalise is not in the request body, raw HTML is returned."""
        mock = _mock_scraper(return_value=SAMPLE_RESULT_SIMPLE)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["html"] == "<html><body><h1>Hello</h1></body></html>"

    def test_normalize_enabled_false_returns_original_html(self):
        """Explicit ``enable: false`` returns unmodified HTML."""
        mock = _mock_scraper(return_value=SAMPLE_RESULT_SIMPLE)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": False},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["html"] == "<html><body><h1>Hello</h1></body></html>"

    def test_normalize_all_false_returns_original_html(self):
        """Every feature disabled, even though ``enabled`` is true, the caller
        has explicitly set everything to false → nothing happens.  This is a
        degenerate case; the normaliser treats ``enabled=true`` with no active
        subfeatures the same as enabled=false."""
        mock = _mock_scraper(return_value=SAMPLE_RESULT_SIMPLE)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {
                    "enabled": True,
                    "absolute_urls": False,
                    "remove_scripts": False,
                    "remove_styles": False,
                    "remove_comments": False,
                    "remove_meta": False,
                    "remove_noscript": False,
                    "collapse_whitespace": False,
                    "minify": False,
                },
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["html"] == "<html><body><h1>Hello</h1></body></html>"


# ========================================================= Remove features


class TestRemoveScripts:
    def test_scripts_removed(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "remove_scripts": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        assert "<script>" not in response.json()["html"]
        assert '<a href="/relative">link</a>' in response.json()["html"]


class TestRemoveStyles:
    def test_style_tags_removed(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "remove_styles": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        html = response.json()["html"]
        assert "<style>" not in html
        assert "color: red" not in html


class TestRemoveComments:
    def test_comments_removed(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "remove_comments": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        assert "<!--" not in response.json()["html"]


class TestRemoveMeta:
    def test_meta_tags_removed(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "remove_meta": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        html = response.json()["html"]
        assert "<meta" not in html


class TestRemoveNoscript:
    def test_noscript_removed(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "remove_noscript": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        assert "<noscript>" not in response.json()["html"]


class TestRemoveDataAttrs:
    def test_data_attrs_removed(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "remove_data_attrs": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        html = response.json()["html"]
        assert "data-reactroot" not in html
        assert "data-id" not in html
        assert "tracked" in html


class TestRemoveHidden:
    def test_hidden_nodes_removed(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "remove_hidden": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        html = response.json()["html"]
        assert "hidden attr" not in html
        assert "aria hidden" not in html
        assert "style hidden" not in html
        assert "template hidden" not in html


class TestRemoveMedia:
    def test_media_removed(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "remove_media": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        html = response.json()["html"]
        for tag_name in ("img", "picture", "source", "svg", "iframe", "audio", "video"):
            assert f"<{tag_name}" not in html


class TestPresets:
    def test_content_preset_enables_common_content_cleanup_without_enabled(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com", "normalize": {"preset": "content"}},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        data = response.json()
        html = data["html"]
        assert "<script" not in html
        assert "<style" not in html
        assert "<meta" not in html
        assert "<noscript" not in html
        assert "data-reactroot" not in html
        assert "hidden attr" not in html
        assert "<img" in html
        assert data["metadata"]["normalized"] is True
        assert data["metadata"]["normalization"]["remove_data_attrs"] is True


# ============================================================== Absolute URLs


class TestAbsoluteUrls:
    def test_relative_links_converted(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "absolute_urls": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        html = response.json()["html"]
        assert 'href="https://example.com/relative"' in html
        assert 'src="https://example.com/img.png"' in html
        assert 'action="https://example.com/submit"' in html
        assert 'poster="https://example.com/poster.jpg"' in html

    def test_srcset_absolute(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "absolute_urls": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        html = response.json()["html"]
        assert "https://example.com/img-320.png 320w" in html
        assert "https://example.com/img-640.png 640w" in html


# ============================================================== Whitespace collapse


class TestCollapseWhitespace:
    def test_whitespace_collapsed(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "collapse_whitespace": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        html = response.json()["html"]
        assert "  " not in html or all(
            # NavigableString whitespace may appear in tag boundaries
            w not in html
            for w in ["Hello   World", "This is   a    test"]
        )


# ===================================================================== Minify


class TestMinify:
    def test_minify(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_SIMPLE)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com", "normalize": {"enabled": True, "minify": True}},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        html = response.json()["html"]
        # Minification should not break the HTML structure
        assert "<html>" in html
        assert "<body>" in html
        assert "<h1>Hello</h1>" in html


# ============================================================ Response format


class TestResponseFormat:
    def test_v2_text_response_format_returns_visible_text_content(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v2/scrape",
            json={"url": "https://example.com", "response_format": "text"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        data = response.json()
        assert "html" not in data
        assert "<" not in data["content"]
        assert "Hello World" in data["content"]
        assert "alert" not in data["content"]
        assert data["metadata"]["response_format"] == "text"
        assert data["metadata"]["content_length"] == len(data["content"])

    def test_v2_html_response_format_returns_html_content(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_SIMPLE)
        client = _patch_scraper(mock)

        response = client.post(
            "/v2/scrape",
            json={"url": "https://example.com", "response_format": "html"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        data = response.json()
        assert "html" not in data
        assert data["content"] == "<html><body><h1>Hello</h1></body></html>"
        assert data["metadata"]["response_format"] == "html"


# ==================================================== Metadata in response


class TestNormalizationMetadata:
    def test_normalized_flag_in_metadata(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={
                "url": "https://example.com",
                "normalize": {"enabled": True, "remove_scripts": True, "absolute_urls": True},
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        metadata = response.json()["metadata"]
        assert metadata["normalized"] is True
        assert metadata["normalization"]["remove_scripts"] is True
        assert metadata["normalization"]["absolute_urls"] is True

    def test_no_normalize_no_metadata_change(self):
        """Without normalise, metadata should not contain normalisation keys,
        but the Pydantic model will still serialise their defaults."""
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        client = _patch_scraper(mock)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        metadata = response.json()["metadata"]
        # Default fields are always present after Pydantic serialisation.
        assert "normalized" in metadata
        assert metadata["normalized"] is False
        # existing keys intact
        assert metadata["mode"] == "http"
        assert metadata["cache_key"] == "abc123"


# ============================================================ Batch scrape


class TestBatchNormalization:
    def test_batch_with_normalize(self):
        mock = _mock_scraper(return_value=SAMPLE_RESULT_WITH_LINKS)
        app.state.scraper = mock
        app.state.cache = MagicMock()
        client = TestClient(app)

        response = client.post(
            "/v1/scrape/batch",
            json={
                "items": [
                    {
                        "url": "https://example.com/1",
                        "normalize": {"enabled": True, "remove_scripts": True},
                    },
                    {"url": "https://example.com/2"},
                ],
            },
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert data["succeeded"] == 2
        assert data["failed"] == 0


# ==================================================== Existing behaviour unchanged


class TestExistingScrapeBehaviour:
    """Regression: existing requests without normalise must behave as before."""

    EXISTING_SAMPLE = {
        "url": "https://example.com",
        "final_url": "https://example.com",
        "status_code": 200,
        "from_cache": False,
        "stale": False,
        "fetched_at": "2026-06-30T10:00:00+00:00",
        "expires_at": "2026-06-30T16:00:00+00:00",
        "html": "<html>Hello World</html>",
        "metadata": {
            "mode": "http",
            "elapsed_ms": 120,
            "content_length": 25,
            "cache_key": "abc123",
        },
    }

    def test_scrape_without_normalize(self):
        mock = _mock_scraper(return_value=self.EXISTING_SAMPLE)
        app.state.scraper = mock
        app.state.cache = MagicMock()
        client = TestClient(app)

        response = client.post(
            "/v1/scrape",
            json={"url": "https://example.com"},
            headers=AUTH_HEADER,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["html"] == "<html>Hello World</html>"
        assert data["url"] == "https://example.com"
        assert data["status_code"] == 200
        assert data["metadata"]["mode"] == "http"
        assert data["metadata"]["cache_key"] == "abc123"
