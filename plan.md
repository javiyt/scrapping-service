# Markdown Output Format Implementation Plan

**Issue:** #179  
**Branch:** `179-markdown-output`  
**Status:** Implementation complete, waiting for coverage threshold

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

## Coverage Gaps

Current coverage: 75.78% (threshold: 77%)

**Remaining coverage needed:** ~1.22%

**New tests added (100% coverage):**
- `test_html_to_markdown_preserves_headings` ✅
- `test_html_to_markdown_converts_links` ✅
- `test_html_to_markdown_removes_scripts` ✅
- `test_html_to_markdown_strips_zero_width_chars` ✅
- `test_html_to_markdown_with_main_content` ✅
- `test_html_to_markdown_includes_images_by_default` ✅
- `test_html_to_markdown_excludes_images_when_disabled` ✅
- `test_html_to_markdown_applies_absolute_urls` ✅
- `test_html_to_markdown_title_extraction` ✅
- `test_html_to_markdown_fallback_to_h1` ✅

**Pre-existing failures (unrelated to markdown):**
- 6 MCP adapter tests fail due to `mcp 2.x` API incompatibility (`Server.add_request_handler` not found)

## Local Verification Status

| Check | Status |
|-------|--------|
| Ruff lint | ✅ Pass |
| Ruff format | ✅ Pass |
| Redocly lint | ✅ Pass |
| MCP contract tests | ✅ Pass |
| All tests (excluding MCP pre-existing) | ✅ 1597 passed |
| Coverage | ❌ 75.78% (needs 77%) |

## Blocking Issues

1. **Coverage threshold:** Need ~1.22% more coverage
   - Options: Add more markdown-specific tests or include HTML normalization tests in main suite
2. **MCP pre-existing failures:** Not caused by markdown implementation

## Next Steps

1. Add additional tests to reach 77% coverage threshold
2. Commit and push changes
3. Push branch and update PR
4. Wait for CI validation
5. Squash merge when all checks pass