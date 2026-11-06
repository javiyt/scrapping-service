"""Contract test helpers for scrapping-service plugins.

This package provides reusable pytest fixtures and helper functions
to run the same provider contract suites used by the built-in providers
against third-party plugins.
"""

from app.plugin_testing.search_contract import (
    assert_search_contract,
    run_search_contract_suite,
)
from app.plugin_testing.transcript_contract import (
    assert_transcript_contract,
    run_transcript_contract_suite,
)

__all__ = [
    "assert_search_contract",
    "assert_transcript_contract",
    "run_search_contract_suite",
    "run_transcript_contract_suite",
]
