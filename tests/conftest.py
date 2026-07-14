"""Shared test fixtures for the scraper API tests."""

import os

import pytest

from app.auth.resolver import reset_profile_resolver


def pytest_configure(config):
    """Set test API key before any test collection or imports.

    This hook runs before conftest fixtures, ensuring the env var is set
    when app modules are imported during test collection.
    """
    os.environ["SCRAPER_API_KEY"] = "test-key-for-tests"


@pytest.fixture(autouse=True)
def _reset_profile_resolver():
    """Reset the global ProfileResolver singleton before each test.

    This ensures tests start with a clean auth state and don't leak
    resolver instances between test cases.
    """
    reset_profile_resolver()
    yield
    reset_profile_resolver()
