# Issue #219 — Fix MCP 2.x Adapter Compatibility

**Issue:** #219  
**Branch:** `219-mcp-2x-compat`  
**Status:** Plan created

## Goal

Migrate the MCP adapter from mcp 1.x API to mcp 2.x SDK. The current `add_request_handler` method was removed in mcp 2.x.

## Root Cause

The `Server` class in mcp 2.x removed the `add_request_handler` method. The new pattern uses different handler registration.

## Current Implementation

File: `app/mcp/server.py`  
Lines 90, 141:
```python
server.add_request_handler("tools/list", types.PaginatedRequestParams, _list_tools)
```

## Affected Files

1. **`app/mcp/server.py`** — MCP server handler registration
2. **`app/mcp/entrypoints.py`** — Entry points that call `create_server()`
3. **`tests/test_mcp_entrypoints.py`** — Failing tests (6 failures)
4. **`tests/test_mcp_http_routes.py`** — Failing tests (5 failures)

## Implementation Steps

### Step 1: Review mcp 2.x API

Research the mcp 2.x migration guide to understand:
- How `Server` registers handlers in 2.x
- Event handler pattern replacement for `add_request_handler`
- Any other breaking changes

### Step 2: Update server.py

Replace `add_request_handler` with the mcp 2.x equivalent:
- Likely uses `@server.tool` decorator or `server.register_tool`
- Or event-driven pattern with callbacks

### Step 3: Verify entrypoints

Check `app/mcp/entrypoints.py` for other 1.x-only API calls.

### Step 4: Run failing tests

```bash
python3 -m pytest tests/test_mcp_entrypoints.py::TestConfiguredStartup::test_main_starts_and_serves_stdio \
  tests/test_mcp_http_routes.py \
  --no-cov -v
```

### Step 5: Add regression tests

Add tests that specifically verify:
- Server can be created with valid config
- Tools/list handler responds correctly
- Health endpoint reports adapter state
- Metrics render in Prometheus format

## Test Strategy

**Failing tests to fix:**
1. `test_mcp_entrypoints.py::TestConfiguredStartup::test_main_starts_and_serves_stdio`
2. `test_mcp_http_routes.py::test_health_reports_adapter_and_service_state`
3. `test_mcp_http_routes.py::test_health_degrades_when_service_down`
4. `test_mcp_http_routes.py::test_metrics_renders_prometheus`
5. `test_mcp_http_routes.py::test_create_server_reads_environment`
6. `test_mcp_http_routes.py::test_create_server_explicit_config`

**New tests to add:**
- `test_mcp_2x_compatibility.py` — explicit regression tests for mcp 2.x API

## Local Verification

```bash
# 1. Ruff clean
python3 -m ruff check . && python3 -m ruff format .

# 2. MCP tests
python3 -m pytest tests/test_mcp_entrypoints.py tests/test_mcp_http_routes.py --no-cov -v

# 3. Full test suite
python3 -m pytest tests/ --no-cov

# 4. Coverage
python3 -m pytest tests/ --ignore=tests/test_mcp_adapter_e2e.py --no-cov
```

## Risks

- **mcp 2.x API breakage:** The migration guide may reveal additional breaking changes beyond `add_request_handler`.
- **Backward compatibility:** If the library still needs to support mcp 1.x, version-specific code paths may be needed.
- **Testing complexity:** Testing async server handlers requires async test patterns.

## Acceptance Criteria

- [ ] All 6 failing MCP tests pass
- [ ] No other tests broken
- [ ] Ruff clean (no lint/format issues)
- [ ] MCP tools register and respond correctly (tools/list endpoint)
- [ ] Health endpoint works with mcp 2.x server
- [ ] Metrics endpoint renders Prometheus format

## Related Issues

- **Blocked by #179:** Markdown output format PR needs this fixed to pass CI (6 test failures)
- **Blocks:** #179 merge

## Dependencies

- `mcp>=2.3.0` (already in `pyproject.toml` as optional dependency)
- `beautifulsoup4>=4.15.0` (for parsing, already present)

## Rollback Plan

If mcp 2.x migration reveals too many breaking changes:
1. Pin mcp to 1.x (`mcp>=1.24.1,<2.0`)
2. Update tests to use 1.x API
3. Create separate issue for 2.x migration

## Timeline

- **Step 1:** 10 min (API research)
- **Step 2:** 30 min (server.py changes)
- **Step 3:** 10 min (entrypoints check)
- **Step 4:** 5 min (test execution)
- **Step 5:** 15 min (new tests)

**Total:** ~70 minutes