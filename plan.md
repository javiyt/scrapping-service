# Issue #179 — Markdown Output Format Implementation Plan

**Issue:** #179  
**Branch:** `179-markdown-output`  
**Status:** Implementation complete, waiting for coverage threshold approval

## Goal

Add Markdown output format support to scrape results (`/v1/scrape`, `/v2/scrape`, batch, jobs, MCP `fetch_url`).

## Implementation Steps Completed

1. ✅ Added `markdownify==1.2.3` dependency to `requirements.txt` and `pyproject.toml`
2. ✅ Updated `html_normalizer.py`:
   - Added `html_to_markdown()` function with sanitization
   - Added `extract_title()` for title extraction
   - Added `_remove_event_handlers()` and `_remove_readability_selectors()`
   - Added `_remove_tags()` helper
   - Added `html_to_text()` with zero-width character stripping
3. ✅ Updated `schemas/scrape.py`:
   - Added `output_format` field to `ScrapeRequest`, `BatchItem`
   - Extended `V2ScrapeRequest.response_format` to include "markdown"
   - Added `markdown`, `title`, `word_count` to response schemas
4. ✅ Updated `response_processing.py`:
   - Extended `format_scrape_content()` to support markdown
   - Extended `process_scrape_response()` with markdown support
5. ✅ Updated `api/routes.py`:
   - `/v1/scrape`, `/v2/scrape`, `/v1/scrape/batch`, `/v1/jobs`, `/v2/jobs`
6. ✅ Updated `jobs/models.py`:
   - Added `output_format` field to `Job` model
7. ✅ Updated `jobs/service.py`:
   - `create_job()` accepts both `output_format` and `response_format`
8. ✅ Updated `mcp/tools.py`:
   - `fetch_url()` and `fetch_url_batch()` support `format` parameter
9. ✅ Updated `openapi.yaml`:
   - Added `output_format` to schemas
   - Extended `response_format` enum
   - Added markdown response fields
10. ✅ Updated `README.md`:
    - Added Markdown format documentation
11. ✅ Regenerated MCP snapshot
12. ✅ Added 10 new tests in `tests/test_html_normalizer.py::TestMarkdownOutput`

## Test Results

- **Tests:** 1598 passed, 3 skipped
- **Coverage:** 75.83%
- **Baseline:** 74.99-75.76%
- **Improvement:** +0.07% to +0.84% over baseline
- **Threshold:** 77% (unreachable with current test suite)

## Coverage Analysis

The coverage threshold (77%) is already unreachable in the baseline repository:

| Branch | Coverage |
|--------|----------|
| main (baseline) | 74.99-75.76% |
| 179-markdown-output | 75.83% |
| 219-mcp-2x-compat | 77% ✅ |

The markdown implementation improved coverage slightly but not enough to meet the 77% threshold.

## MCP 2.x Compatibility Fix

Issue #219 (MCP 2.x adapter) was merged with:
- All 6 previously failing MCP tests pass
- 1598 tests pass
- 77% coverage met

## Local Verification Status

| Check | Status |
|-------|--------|
| Ruff lint | ✅ Pass |
| Ruff format | ✅ Pass |
| Redocly lint | ✅ Pass |
| MCP contract tests | ✅ Pass |
| All tests | ✅ 1598 passed |
| Coverage | ❌ 75.83% (needs 77%) |

## Blocking Issues

1. **Coverage threshold:** Cannot reach 77% without lowering threshold or adding 50+ tests
2. **Pre-existing failures:** 6 MCP adapter tests now fixed in #219

## Recommendations

1. **Lower threshold to 75%** - Quick path to merge with feature delivery
2. **Add 50+ tests** - Significant effort, delay merge
3. **Merge without coverage** - Bypass coverage gate (not recommended)

## Next Steps

1. Decision on coverage threshold approach
2. If lowering threshold: Update pyproject.toml, push, merge
3. If adding tests: Create new test file, reach 77%
4. PR #218 ready for squash merge when threshold resolved