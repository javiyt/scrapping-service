"""Contract tests for TranscriptProvider instances.

These tests are reused across the service's built-in providers
and third-party plugins.

Usage::

    from app.plugin_testing.transcript_contract import run_transcript_contract_suite

    # After registering your provider
    provider = MyTranscriptProvider()
    run_transcript_contract_suite(provider)
"""

from __future__ import annotations

import asyncio

from app.core.errors import ScraperError, TranscriptProviderUnavailableError
from app.transcript import TranscriptHealth, TranscriptProvider
from app.transcript.models import TranscriptResponse, TranscriptSegment


def assert_transcript_contract(provider: TranscriptProvider) -> None:
    """Assert that a TranscriptProvider satisfies the contract.

    This function runs all contract checks synchronously (awaiting
    async methods via asyncio.run). It raises AssertionError if any
    contract requirement is violated.

    Checks:
    - provider.name is a non-empty string
    - health() returns TranscriptHealth with boolean available
    - fetch(video_id, ...) returns TranscriptResponse with provider.name
      in metadata.provider and segments with text, start, end
    - Errors raised are ScraperError subclasses

    Args:
        provider: An instance of TranscriptProvider (or any object with
          the required attributes/methods).

    Raises:
        AssertionError: if any contract requirement is violated.
    """
    # 1. Name check
    name = getattr(provider, "name", None)
    if not isinstance(name, str) or not name:
        raise AssertionError("TranscriptProvider.name must be a non-empty string")

    # 2. Health check
    async def _check_health() -> None:
        health = await provider.health()
        if not isinstance(health, TranscriptHealth):
            raise AssertionError(f"{name}.health() must return TranscriptHealth")
        if not isinstance(health.available, bool):
            raise AssertionError(f"{name}.health().available must be bool")

    asyncio.run(_check_health())

    # 3. Fetch check
    async def _check_fetch() -> None:
        # Use a known-valid video ID for the provider
        test_id = "dQw4w9WgXcQ"  # Rick Astley
        segments = ["test"]
        response = await provider.fetch(test_id, segments)
        if not isinstance(response, TranscriptResponse):
            raise AssertionError(f"{name}.fetch() must return TranscriptResponse")
        if response.metadata.provider != name:
            raise AssertionError(f"{name}.fetch().metadata.provider must equal provider name")
        if not response.segments:
            raise AssertionError(f"{name}.fetch() must return at least one segment")
        for seg in response.segments:
            if not isinstance(seg, TranscriptSegment):
                raise AssertionError(f"{name}.fetch() segments must be TranscriptSegment instances")
            if not isinstance(seg.text, str) or not seg.text:
                raise AssertionError(f"{name}.fetch() segment must have non-empty text")
            if not isinstance(seg.start, (int, float)):
                raise AssertionError(f"{name}.fetch() segment must have numeric start")
            # end is computed as start + duration
            end = seg.start + (seg.duration if seg.duration is not None else 0)
            if not isinstance(end, (int, float)):
                raise AssertionError(f"{name}.fetch() segment must have numeric end")

    asyncio.run(_check_fetch())

    # 4. Error check (unavailable provider should raise ScraperError)
    async def _check_unavailable() -> None:
        original_health = provider.health

        async def _unavailable_health() -> TranscriptHealth:
            return TranscriptHealth(available=False, reason="unavailable for test")

        provider.health = _unavailable_health  # type: ignore[method-assign]
        try:
            await provider.fetch("test-id", ["test"])
        except (TranscriptProviderUnavailableError, ScraperError):
            pass  # Expected
        except Exception as exc:
            raise AssertionError(
                f"{name}.fetch() with unavailable=True must raise ScraperError, "
                f"got {type(exc).__name__}"
            ) from None
        finally:
            provider.health = original_health  # type: ignore[method-assign]

    asyncio.run(_check_unavailable())


async def _run_transcript_contract_async(provider: TranscriptProvider) -> None:
    """Async version of assert_transcript_contract for await from test functions."""
    assert_transcript_contract(provider)


def run_transcript_contract_suite(provider: TranscriptProvider) -> None:
    """Run the full transcript provider contract suite.

    This is a helper for test suites that wants to run the contract checks
    with a single call. It calls assert_transcript_contract internally.

    Example::

        def test_my_plugin_transcript_contract():
            provider = MyPlugin().create_transcript_provider()
            run_transcript_contract_suite(provider)

    Args:
        provider: An instance of TranscriptProvider.
    """
    assert_transcript_contract(provider)
