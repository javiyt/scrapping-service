---
name: debug-eof-errors
description: Diagnose and fix EOF / timeout errors in Python async HTTP services (httpx, FastAPI, uvicorn) — especially containerized scraper APIs on resource-constrained hosts like Raspberry Pi.
source: auto-skill
extracted_at: '2026-07-13T13:14:04.945Z'
---

# Debugging EOF / Timeout Errors in HTTP Scraper Services

When a caller receives `EOF` or `connection reset` instead of a structured HTTP error response from a Python async HTTP service (FastAPI/uvicorn), the root cause is almost always at the **transport layer**: an exception kills the connection before uvicorn can send a response body.

## Diagnostic Checklist

### 1. Identify where the EOF originates

Run a controlled reproduction **from the same host** as the failing service:

```bash
# From the container itself or the Pi
python3 -c "
import httpx, sys, time, asyncio
async def test():
    t = httpx.Timeout(connect=15, read=90, write=10, pool=10)
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=t, follow_redirects=True) as c:
            r = await c.get(sys.argv[1])
        print(f'OK status={r.status_code} body={len(r.text)}b elapsed={int((time.monotonic()-start)*1000)}ms')
    except Exception as e:
        print(f'FAIL {type(e).__name__}: {e} elapsed={int((time.monotonic()-start)*1000)}ms')
asyncio.run(test())
" 'https://target-url.com'
```

- **Timeout?** → Increase `read` timeout (see Fix 2 below)
- **DNS / connect failure?** → Check network from the Pi (`dig`, `nslookup`)
- **403 / 429?** → Anti-bot block (check headers, use browser mode, add proxy)

### 2. Check if the exception is caught

Look at the fetch/request method in your service — are httpx exceptions caught?

**Common structural bug:**
```python
# ❌ BAD: no exception handling at the httpx call site
async with httpx.AsyncClient(...) as client:
    response = await client.get(url)  # ReadTimeout → unhandled → corrupts connection
```

**Fix:**
```python
# ✅ GOOD: catch httpx exceptions and convert to app-level errors
try:
    async with httpx.AsyncClient(...) as client:
        response = await client.get(url)
except httpx.TimeoutException as exc:
    raise MyTimeoutError(...) from exc
except httpx.HTTPError as exc:
    raise MyHttpError(...) from exc
```

### 3. Check uvicorn connection lifecycle

Uvicorn defaults can cause premature connection drops with long-running requests:

- Add `--timeout-keep-alive 30` (default 5s is too short for scraper workloads)
- Add `--limit-max-requests 5000` to gracefully recycle workers (prevents memory creep)
- Consider `--workers 1` (no benefit from multiple workers for I/O-bound async)

### 4. Check container health check timeouts

On slow-starting hosts (Raspberry Pi), the deploy script's health check may time out **before the app binds the port**:

- Increase retry count from 12 → 24 (60s → 120s budget with 5s intervals)
- Show per-attempt HTTP status code to distinguish `connection refused` vs non-200
- Set Docker `HEALTHCHECK --start-period=60s` (not 15s) for Pi cold starts
- Add `podman stats` and `verbose curl` to the failure debug output

### 5. Check for OOM kills (silent container death)

Heavy pages + browser mode (Chromium) can OOM a Raspberry Pi 5 mid-request:

```bash
# Run alongside the failing request
podman stats scraper-api

# Look for OOM in logs
journalctl --user -u scraper-api.service | grep -i "oom\|killed\|memory"
dmesg | grep -i "oom\|killed" | tail -5
```

## Fixes to Apply

### Fix 1: Split httpx timeouts (separate connect / read / write / pool)

Never use a combined timeout: `httpx.Timeout(90)` applies the same limit to connect, read, write, and pool — a slow connect eats into read budget.

```python
# ✅ Separate timeouts
timeout = httpx.Timeout(
    connect=min(15.0, raw_timeout * 0.2),   # connect: 15s max
    read=raw_timeout * 0.7,                  # read: bulk of budget
    write=10.0,                              # write: rarely the bottleneck
    pool=10.0,                               # pool: quick
)
```

### Fix 2: Increase default timeouts for heavy pages

- e-commerce product listing pages can take >60s to download on a Pi
- Set a sensible default (90s) and allow per-request override up to 180s
- Add per-domain timeout overrides in config for known-heavy sites

### Fix 3: Log the outgoing request before it fails

Add a `logger.debug(...)` with URL and timeout values **before** the HTTP call, and log response size + elapsed time **after** success. This lets you see whether the request ever left the container.

### Fix 4: Log non-2xx responses with body preview

When a scraper gets 403/429, the first 500 characters of the body usually tell you *why* (Cloudflare challenge, rate limit page, etc.):

```python
if response.status_code >= 400:
    logger.warning("Non-2xx for %s: %d — body: %s", url, response.status_code, response.text[:500])
```

### Fix 5: Create a standalone reproduction script

A self-contained Python script that replicates the **exact** outgoing request (same headers, same httpx config, same timeout model) — run this on the target host before debugging any service-level issue:

```bash
./scripts/debug-eof.sh --timeout 90 --verbose
./scripts/debug-eof.sh --api http://host:9090 --key $KEY --timeout 90
```

## Deployment Checklist

| Item | Before | After |
|------|--------|-------|
| Default timeout | 45s | 90s |
| Max request timeout | 120s | 180s |
| httpx exception handling | None | Caught → TimeoutError/HttpError |
| Liveness start-period | 15s | 60s |
| Deploy health check retries | 12 | 24 |
| Failure debug output | tail 10 logs | logs + stats + verbose curl |
