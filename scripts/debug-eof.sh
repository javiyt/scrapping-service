#!/usr/bin/env bash
# =============================================================================
# debug-eof.sh — Diagnose EOF errors when scraping heavy pages
# =============================================================================
# This script reproduces the exact HTTP request the scraper API makes, with
# configurable timeouts and verbose logging.  Run it on the Raspberry Pi to
# isolate whether the issue is a timeout, a blocked response, or a network
# problem.
#
# Usage:
#   # Quick test with the Fanatics URL (default 90s timeout)
#   ./scripts/debug-eof.sh
#
#   # Test with specific timeout (simulate the 45s default that caused EOF)
#   ./scripts/debug-eof.sh --timeout 45
#
#   # Verbose output including response headers and first 2KB of body
#   ./scripts/debug-eof.sh --verbose
#
#   # Test a simple URL for comparison
#   ./scripts/debug-eof.sh --url "https://www.example.com"
#
#   # Specify API key + host for end-to-end API test
#   ./scripts/debug-eof.sh --api http://raspberry:9090 --key "$SCRAPER_API_KEY"
# =============================================================================

set -euo pipefail

TEST_URL="${TEST_URL:-https://www.example.com}"
TIMEOUT=90
VERBOSE=false
API_BASE=""
API_KEY=""

# ------------------------------------------------------------------- parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --timeout)    TIMEOUT="$2";     shift 2 ;;
        --verbose)    VERBOSE=true;     shift ;;
        --url)        TEST_URL="$2"; shift 2 ;;
        --api)        API_BASE="$2";    shift 2 ;;
        --key)        API_KEY="$2";     shift 2 ;;
        --help)       echo "Usage: $0 [--timeout N] [--verbose] [--url URL] [--api URL --key KEY]"; exit 0 ;;
        *)            echo "Unknown: $1"; exit 1 ;;
    esac
done

# ------------------------------------------------------------------- helpers
red()   { echo -e "\033[31m$1\033[0m"; }
green() { echo -e "\033[32m$1\033[0m"; }
bold()  { echo -e "\033[1m$1\033[0m"; }

# ===========================================================================
# TEST 1: Direct httpx test (replicates the exact Python request)
# ===========================================================================
echo ""
bold "═══ TEST 1: Direct HTTP request with httpx (timeout=${TIMEOUT}s) ═══"
echo ""

PYTHON_SCRIPT=$(cat <<'PYEOF'
import asyncio
import sys
import time

try:
    import httpx
except ImportError:
    print("ERROR: httpx is not installed. Run: pip install httpx")
    sys.exit(1)

async def test_fetch(url, timeout, verbose):
    # Replicate the exact headers from HttpFetcher._do_fetch
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    t = httpx.Timeout(
        connect=min(15.0, timeout * 0.2),
        read=timeout * 0.7,
        write=10.0,
        pool=10.0,
    )

    client_kwargs = {
        "timeout": t,
        "follow_redirects": True,
        "max_redirects": 10,
    }

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(url, headers=headers)
    except httpx.ReadTimeout as e:
        elapsed = int((time.monotonic() - start) * 1000)
        print(f"RESULT: READ TIMEOUT after {elapsed}ms (timeout={timeout}s)")
        print(f"ERROR: {e}")
        return False
    except httpx.ConnectTimeout as e:
        elapsed = int((time.monotonic() - start) * 1000)
        print(f"RESULT: CONNECT TIMEOUT after {elapsed}ms")
        print(f"ERROR: {e}")
        return False
    except httpx.TimeoutException as e:
        elapsed = int((time.monotonic() - start) * 1000)
        print(f"RESULT: TIMEOUT after {elapsed}ms")
        print(f"ERROR: {e}")
        return False
    except httpx.HTTPError as e:
        elapsed = int((time.monotonic() - start) * 1000)
        print(f"RESULT: HTTP ERROR after {elapsed}ms")
        print(f"ERROR: {e}")
        return False
    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        print(f"RESULT: UNEXPECTED ERROR after {elapsed}ms")
        print(f"ERROR: {type(e).__name__}: {e}")
        return False

    elapsed = int((time.monotonic() - start) * 1000)
    body_len = len(response.text)
    print(f"RESULT: SUCCESS")
    print(f"  Status:  {response.status_code}")
    print(f"  Elapsed: {elapsed} ms")
    print(f"  Body:    {body_len} bytes")
    print(f"  Final:   {response.url}")

    if verbose:
        print(f"\n--- Response Headers ---")
        for k, v in response.headers.items():
            print(f"  {k}: {v}")
        print(f"\n--- Body Preview (first 2000 chars) ---")
        print(response.text[:2000])
        print("... (truncated)")

    if response.status_code == 429:
        print(f"\n⚠  RATE LIMITED (429) — server is blocking the scraper")
        retry_after = response.headers.get("retry-after", "unknown")
        print(f"   Retry-After: {retry_after}s")
    elif response.status_code == 403:
        print(f"\n⚠  FORBIDDEN (403) — anti-bot / WAF block")
    elif body_len < 500:
        print(f"\n⚠  SUSPICIOUS: body is only {body_len} bytes — possible block page")
        print(f"   Body preview: {response.text[:300]}")

    return True

url = sys.argv[1]
timeout = int(sys.argv[2])
verbose = sys.argv[3].lower() == "true"

asyncio.run(test_fetch(url, timeout, verbose))
PYEOF

python3 -c "$PYTHON_SCRIPT" "$TEST_URL" "$TIMEOUT" "$VERBOSE" && HTTP_OK=true || HTTP_OK=false

# ===========================================================================
# TEST 2: End-to-end API test (if --api was provided)
# ===========================================================================
if [[ -n "$API_BASE" && -n "$API_KEY" ]]; then
    echo ""
    bold "═══ TEST 2: API endpoint test (${API_BASE}/v1/scrape) ═══"
    echo ""

    echo "  URL: $TEST_URL"
    echo "  Timeout: ${TIMEOUT}s"
    echo ""

    PAYLOAD=$(cat <<JSON
{"url": "$TEST_URL", "timeout_seconds": $TIMEOUT}
JSON
)

    # Use --max-time to avoid hanging forever
    CURL_START=$(date +%s%N)
    RESPONSE=$(curl -s --max-time $((TIMEOUT + 10)) \
        -H "Authorization: Bearer $API_KEY" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" \
        "${API_BASE}/v1/scrape" 2>&1) || CURL_EXIT=$?
    CURL_ELAPSED=$(( ($(date +%s%N) - CURL_START) / 1000000 ))

    if [[ -z "${CURL_EXIT:-0}" || "${CURL_EXIT:-0}" -eq 0 ]]; then
        echo "RESULT: API call succeeded in ${CURL_ELAPSED}ms"
        STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status_code','?'))" 2>/dev/null || echo "parse-error")
        echo "  HTTP status code from scrape: $STATUS"
        BODY_LEN=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('html','')))" 2>/dev/null || echo "?")
        echo "  HTML body length: $BODY_LEN bytes"
        MODE=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('metadata',{}).get('mode','?'))" 2>/dev/null || echo "?")
        echo "  Mode used: $MODE"

        if echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); assert 'error' not in d, d['error'].get('message','')" 2>/dev/null; then
            green "  ✓ No error in response"
        else
            ERROR_MSG=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error',{}).get('message','unknown'))" 2>/dev/null)
            red "  ✗ Error: $ERROR_MSG"
        fi
    else
        red "RESULT: API call FAILED after ${CURL_ELAPSED}ms (exit=${CURL_EXIT:-$?})"
        if [[ -n "$RESPONSE" ]]; then
            echo "  Response: $RESPONSE"
        fi
    fi
fi

# ===========================================================================
# TEST 3: Memory check (via podman stats)
# ===========================================================================
echo ""
bold "═══ TEST 4: Container memory usage ═══"
echo ""
if command -v podman &>/dev/null; then
    if podman ps --filter name=scraper-api --format "{{.Names}}" 2>/dev/null | grep -q scraper-api; then
        echo "  scraper-api container stats:"
        podman stats --no-stream scraper-api 2>&1 || echo "  (stats unavailable)"
        echo ""
        echo "  System memory:"
        free -h 2>/dev/null || cat /proc/meminfo 2>/dev/null | head -5 || echo "  (unavailable)"
    else
        echo "  scraper-api container is not running"
    fi
else
    echo "  podman not available on this host"
fi

# ===========================================================================
# Summary
# ===========================================================================
echo ""
bold "═══ DIAGNOSTIC SUMMARY ═══"
echo ""
echo "  URL:          $TEST_URL"
echo "  Timeout:      ${TIMEOUT}s"
echo "  HTTP test:    $($HTTP_OK && green "PASS" || red "FAIL")"
if [[ -n "$API_BASE" ]]; then
    echo "  API test:     ${API_BASE}/v1/scrape"
fi
echo ""
echo "  If the HTTP test PASSED but your API call fails, the issue is likely:"
echo "    1. Container resource limits (check TEST 4 output)"
echo "    2. Request body too large → check the calling service"
echo "    3. Proxy / reverse-proxy timeout between services"
echo ""
echo "  If the HTTP test FAILED:"
echo "    - With timeout: increase timeout_seconds in your scrape request"
echo "    - With DNS error: check network connectivity from the Pi"
echo "    - With 403/429: the site is blocking automated requests"
echo ""
