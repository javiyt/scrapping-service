"""Shared test fixtures for the scraper API tests."""

import pytest

from app.auth.resolver import reset_profile_resolver


@pytest.fixture(autouse=True)
def _reset_profile_resolver():
    """Reset the global ProfileResolver singleton before each test.

    This ensures tests start with a clean auth state and don't leak
    resolver instances between test cases.
    """
    reset_profile_resolver()
    yield
    reset_profile_resolver()
