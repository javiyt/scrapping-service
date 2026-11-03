"""Tests for the project-owned Camofox error types and their HTTP mapping."""

import pytest

from app.core.errors import (
    EXCEPTION_STATUS_MAP,
    CamofoxAuthError,
    CamofoxError,
    CamofoxInvalidResponseError,
    CamofoxNavigationError,
    CamofoxSessionExpiredError,
    CamofoxTabNotFoundError,
    CamofoxTimeoutError,
    CamofoxUnavailableError,
    ScraperError,
)

_CASES = [
    (CamofoxError, "camofox_error", 502),
    (CamofoxUnavailableError, "camofox_unavailable", 503),
    (CamofoxTimeoutError, "camofox_timeout", 504),
    (CamofoxAuthError, "camofox_auth_failed", 502),
    (CamofoxInvalidResponseError, "camofox_invalid_response", 502),
    (CamofoxSessionExpiredError, "camofox_session_expired", 502),
    (CamofoxTabNotFoundError, "camofox_tab_not_found", 502),
    (CamofoxNavigationError, "camofox_navigation_failed", 502),
]


@pytest.mark.parametrize(
    ("cls", "error_type", "status"),
    _CASES,
    ids=[cls.__name__ for cls, _, _ in _CASES],
)
def test_error_shape(cls, error_type, status):
    exc = cls("boom", details={"status_code": 404, "endpoint": "/tabs/abc"})
    assert isinstance(exc, ScraperError)
    assert isinstance(exc, CamofoxError)
    assert exc.error_type == error_type
    assert exc.status_code == status
    assert exc.message == "boom"
    assert exc.details == {"status_code": 404, "endpoint": "/tabs/abc"}


def test_exception_status_map_entries():
    for cls, error_type, status in _CASES:
        assert EXCEPTION_STATUS_MAP[cls] == status
        assert EXCEPTION_STATUS_MAP[cls] == cls("x").status_code


def test_base_covers_all_concrete_types():
    concrete = [
        CamofoxUnavailableError,
        CamofoxTimeoutError,
        CamofoxAuthError,
        CamofoxInvalidResponseError,
        CamofoxSessionExpiredError,
        CamofoxTabNotFoundError,
        CamofoxNavigationError,
    ]
    for cls in concrete:
        assert issubclass(cls, CamofoxError)


def test_to_dict_is_json_safe_and_details_preserved():
    exc = CamofoxNavigationError(
        "Camofox navigation failed", details={"status_code": 500, "url": "https://x.example"}
    )
    payload = exc.to_dict()["error"]
    assert payload["type"] == "camofox_navigation_failed"
    assert payload["message"] == "Camofox navigation failed"
    assert payload["details"]["url"] == "https://x.example"
    import json

    json.dumps(exc.to_dict())


def test_default_details_empty():
    exc = CamofoxError("generic")
    assert exc.details == {}
    assert exc.to_dict()["error"]["details"] == {}
