#!/usr/bin/env bash
# =============================================================================
# smoke-test.sh — Quick functional test for the scraper API.
# =============================================================================
# Usage:
#   ./scripts/smoke-test.sh http://localhost:8080              # no auth
#   ./scripts/smoke-test.sh http://localhost:8080 my-api-key   # with auth
# =============================================================================

set -euo pipefail

BASE_URL="${1:-http://localhost:8080}"
API_KEY="${2:-}"

PASS=0
FAIL=0

header() {
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo "  $1"
    echo "═══════════════════════════════════════════════════════════════"
}

check() {
    local label="$1"
    local expected="$2"
    local actual="$3"
    if echo "$actual" | grep -q "$expected"; then
        echo "  ✓ $label"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $label  (expected: $expected)"
        echo "    Response: $actual"
        FAIL=$((FAIL + 1))
    fi
}

AUTH_FLAG=""
[[ -n "$API_KEY" ]] && AUTH_FLAG="-H Authorization: Bearer $API_KEY"

# ============================================================= Health
header "Health endpoint"

HEALTH=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health")
check "/health returns 200" "200" "$HEALTH"

HEALTH_BODY=$(curl -s "$BASE_URL/health")
check "/health body has status=ok" '"status":"ok"' "$HEALTH_BODY"
check "/health body has service" '"service":"scraper-api"' "$HEALTH_BODY"

# ============================================================= Readiness
header "Readiness endpoint"

READY_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/ready" $AUTH_FLAG)
check "/ready returns 200 with auth" "200" "$READY_CODE"

# ============================================================= Auth
header "Authentication"

NOAUTH_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/v1/scrape" \
    -H "Content-Type: application/json" \
    -d '{"url":"https://example.com"}')
check "/v1/scrape without auth returns 401/403" "40[13]" "$NOAUTH_CODE"

WRONG_KEY_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/v1/scrape" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer wrong-key" \
    -d '{"url":"https://example.com"}')
check "/v1/scrape with wrong key returns 40x" "40[13]" "$WRONG_KEY_CODE"

# ============================================================= Scrape
header "Scrape endpoint"

if [[ -n "$API_KEY" ]]; then
    SCRAPE=$(curl -s "$BASE_URL/v1/scrape" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $API_KEY" \
        -d '{"url":"https://example.com","mode":"http"}')

    check "Scrape returns status 200" '"status_code":200' "$SCRAPE"
    check "Scrape returns html" '"html":' "$SCRAPE"
    check "Scrape returns metadata" '"metadata":' "$SCRAPE"
    check "Scrape reports mode" '"mode":"http"' "$SCRAPE"
else
    echo "  (skip — no API key provided)"
fi

# ============================================================= Summary
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Results:  $PASS passed,  $FAIL failed"
echo "═══════════════════════════════════════════════════════════════"

[[ "$FAIL" -gt 0 ]] && exit 1
exit 0
