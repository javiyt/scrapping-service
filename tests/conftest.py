"""Shared test fixtures for the scraper API tests."""

import asyncio
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


@pytest.fixture(autouse=True)
def _clean_event_loop():
    """Reset the asyncio event loop policy before each test.

    pytest-asyncio creates a fresh event loop per async test and closes it on
    teardown.  In Python 3.11 the default event loop *policy* caches the loop
    on the first ``get_event_loop()`` call, so after an async test *before* a
    sync test, that sync test's ``get_event_loop()`` returns the now-closed
    loop and ``run_until_complete()`` fails with "no current event loop".

    This guard proactively installs a fresh, open loop before *every* test.
    For a following sync test that is exactly the fix; for an async test
    pytest-asyncio installs its own loop on top (conftest autouse fixtures run
    before the test function, and ``get_event_loop()`` inside the running loop
    returns it, so the cache-maintenance-style sync tests still get a working
    loop either way).
    """
    policy = asyncio.get_event_loop_policy()
    try:
        # Force a fresh, open loop so a sync test after an async one (which
        # closes its loop) still gets a working ``get_event_loop()``.
        policy.set_event_loop(asyncio.new_event_loop())
    except Exception:  # noqa: BLE001 - never fail a test over loop hygiene
        pass
    yield
    try:
        current = policy.get_event_loop()
        if current.is_running():
            current.close()
        policy.set_event_loop(None)
    except Exception:  # noqa: BLE001
        pass
