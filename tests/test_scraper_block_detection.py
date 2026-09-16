"""Tests for block-page heuristics in the scraper service."""

from app.scraper.http_fetcher import FetchResult
from app.scraper.service import ScraperService


def _service() -> ScraperService:
    return object.__new__(ScraperService)


def _result(html: str, status_code: int = 200) -> FetchResult:
    return FetchResult(
        html=html,
        status_code=status_code,
        final_url="https://example.com",
        headers={},
        elapsed_ms=10,
    )


def test_plain_blocked_word_does_not_mark_large_response_as_blocked():
    html = "<html><body>" + ("valid content " * 80) + "ad slot blocked by browser setting</body></html>"

    assert _service()._looks_blocked(_result(html)) is False


def test_specific_block_page_phrase_is_detected():
    html = "<html><body>You have been blocked. Please contact the site owner.</body></html>"

    assert _service()._looks_blocked(_result(html)) is True


def test_block_status_code_is_detected():
    html = "<html><body>" + ("valid content " * 80) + "</body></html>"

    assert _service()._looks_blocked(_result(html, status_code=403)) is True
