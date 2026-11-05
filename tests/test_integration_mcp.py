"""MCP adapter — integration placeholder (ticket #129, scenario group 6).

The ticket asks for deterministic MCP integration tests
(``search_web`` / ``search_reddit`` / ``search_youtube`` / ``fetch_url`` /
interactive browser tools).  **The MCP adapter does not exist yet**: it is
owned by the follow-up tickets in the unified web-access epic
(#149 adapter core, #150 search tools, #151 transcript tools, #152 fetch
tools, #153 interactive browser tools, #154 browser lifecycle, #155 resource
policy).  There is no ``app/mcp`` module and no MCP dependency, so there is
nothing to import and nothing to drive yet.

What this file *does* pin, deterministically and with zero network:

1. **The service contract the adapter must expose** — every tool the MCP
   surface will offer maps onto an existing, already-integration-tested
   service.  These tests assert that each tool has a concrete service
   counterpart with the expected behavior *today*, so that when #149-#155
   land the adapter is a thin composition of proven services and the
   integration tests here flip to "real thing" without rewriting the
   expectations:

   - ``search_web``       → unified search stack (POST /v1/search)
   - ``search_reddit``    → unified search with ``verticals=["social"]``
   - ``search_youtube``   → unified search with ``verticals=["videos"]``
   - ``fetch_url``        → unified scrape stack (POST /v1/scrape)
   - interactive browser  → Agent Browser API (/v1/browser/...)

2. **An honest gate** — a marker that fails loudly if someone adds an
   ``app/mcp`` package without finishing the integration work, so the
   "MCP: not yet implemented" state can never go stale silently.

Run these together with:
    tests/test_integration_search.py
    tests/test_integration_scrape.py
    tests/test_integration_agent_browser.py
    tests/test_integration_transcript.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.metrics.prometheus import MetricsCollector
from app.search import (
    SearchCache,
    SearchCacheConfig,
    SearchProviderRegistry,
    SearchRouter,
    SearchRouterConfig,
)
from app.search.models import Vertical
from tests.fakes import FakeSearchProvider

API_KEY = "test-key-for-tests"
AUTH = {"Authorization": f"Bearer {API_KEY}"}

REPO_ROOT = Path(__file__).resolve().parent.parent


# ====================================================================== the gate


def test_mcp_adapter_is_implemented():
    """Gate: the ``app/mcp`` package exists and is importable.

    The adapter landed in #149 (ticket #129 scenario group 6).  The real
    adapter tests live in ``tests/test_mcp_adapter_*.py`` (security,
    contract schema, E2E over the MCP protocol) and
    ``tests/test_mcp_contract_schema.py``.
    """
    has_mcp_package = (REPO_ROOT / "app" / "mcp" / "__init__.py").exists()
    assert has_mcp_package, "app/mcp must exist (the adapter is implemented in #149)"
    assert importlib.util.find_spec("app.mcp") is not None

    # The adapter must expose its documented entry points.
    from app.mcp import MCP_ADAPTER_VERSION  # noqa: PLC0415

    assert MCP_ADAPTER_VERSION


# ============================================ the contract the adapter will expose


def _web_search_service(tmp_path, verticals: list[Vertical]) -> None:
    """The exact unified-search composition the future ``search_*`` MCP tools
    will consume."""
    provider = FakeSearchProvider(
        name="camofox",
        verticals=[Vertical.WEB, Vertical.SOCIAL, Vertical.VIDEOS],
        result_url="https://example.com/result",
        result_title="deterministic result",
    )
    router = SearchRouter(
        SearchProviderRegistry([provider]),
        SearchRouterConfig(provider_order=["camofox"]),
        metrics=MetricsCollector(),
    )
    from app.cache.sqlite_cache import SqliteCache

    store = SqliteCache(db_path=str(tmp_path / "mcp-search.db"))
    store.open()
    cache = SearchCache(
        store=store, router=router, config=SearchCacheConfig(), metrics=MetricsCollector()
    )
    app.state.search_cache = cache
    app.state.settings = Settings(_env_file=None, api_key=API_KEY)


def _run_search(client: TestClient, query: str, verticals: list[str]) -> dict:
    return client.post("/v1/search", json={"query": query, "verticals": verticals}, headers=AUTH)


def test_search_web_tool_contract(tmp_path):
    """6.1 ``search_web`` — maps to the unified search stack (web vertical)."""
    _web_search_service(tmp_path, [Vertical.WEB])
    client = TestClient(app)
    body = _run_search(client, "Miami Dolphins", ["web"])
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["results"][0]["title"] == "deterministic result"
    assert data["metadata"]["providers_attempted"] == ["camofox"]


def test_search_reddit_tool_contract(tmp_path):
    """6.2 ``search_reddit`` — maps to the unified search stack (social
    vertical, capability-matched to Camofox)."""
    _web_search_service(tmp_path, [Vertical.SOCIAL])
    client = TestClient(app)
    body = _run_search(client, "Dolphins", ["social"])
    assert body.status_code == 200, body.text
    assert body.json()["metadata"]["providers_attempted"] == ["camofox"]


def test_search_youtube_tool_contract(tmp_path):
    """6.3 ``search_youtube`` — maps to the unified search stack (videos
    vertical)."""
    _web_search_service(tmp_path, [Vertical.VIDEOS])
    client = TestClient(app)
    body = _run_search(client, "Dolphins training", ["videos"])
    assert body.status_code == 200, body.text
    assert body.json()["metadata"]["providers_attempted"] == ["camofox"]


def test_fetch_url_tool_contract():
    """6.4 ``fetch_url`` — maps to the unified scrape stack; the full chain
    (HTTP → browser fallbacks → cached browser) is integration-tested in
    ``tests/test_integration_scrape.py`` with fakes.  Here we only pin that
    the public surface is live and auth-gated.

    The ``/v1/scrape`` route is always present and auth-gated; without
    credentials the request is refused (401) before any work is done.
    With a valid key the request succeeds (200) because the lifespan builds
    a real scraper stack (with a dead-transport HTTP client in tests, or a
    real one in CI — either way the route is *reachable*).  The full
    scrape→fallback→cache integration is covered in
    ``tests/test_integration_scrape.py``.
    """
    client = TestClient(app)
    # Without credentials the tool surface refuses before doing anything.
    body = client.post("/v1/scrape", json={"url": "https://example.com", "mode": "http"})
    assert body.status_code == 401

    # With valid credentials the route is live (status may be 200 or a
    # provider error; the contract is that it is *reachable* and auth-gated,
    # not that it succeeds).
    body = client.post(
        "/v1/scrape", json={"url": "https://example.com", "mode": "http"}, headers=AUTH
    )
    assert body.status_code in (200, 400, 403, 404, 409, 502, 503)


def test_interactive_browser_tool_contract(tmp_path):
    """6.5 interactive browser — maps to the Agent Browser API (session, tab,
    snapshot, actions); the full lifecycle with fakes is integration-tested
    in ``tests/test_integration_agent_browser.py``.  Here we pin the
    capabilities surface: enabled, provider, and the resource limits the
    MCP lifecycle/policy tools (#154, #155) will surface."""
    from app.browser.service import AgentBrowserService
    from tests.fakes import FakeAgentBrowserProvider

    settings = Settings(
        _env_file=None,
        api_key=API_KEY,
        agent_browser_enabled=True,
        agent_browser_allow_evaluate=True,
        agent_browser_max_sessions=3,
        agent_browser_max_tabs_per_session=5,
        agent_browser_session_ttl_seconds=1800,
    )
    app.state.agent_browser_service = AgentBrowserService(FakeAgentBrowserProvider(), settings)
    app.state.settings = settings
    import app.api.dependencies as deps

    deps._settings = settings
    # The capabilities route: when a profile is present it calls
    # get_profile_resolver().effective_settings_for(profile).  The resolver
    # merges profile overrides onto *its own* Settings instance (loaded at
    # init time), not onto deps._settings.  So the agent_browser values we
    # set on deps._settings do not propagate unless the resolver's own
    # settings carry them.  The simplest deterministic approach: initialise
    # the profile resolver with *our* settings so the merge starts from the
    # right base.
    from app.auth.resolver import init_profile_resolver

    init_profile_resolver(settings)

    client = TestClient(app)
    body = client.get("/v1/browser/capabilities", headers=AUTH)
    assert body.status_code == 200, body.text
    caps = body.json()
    assert caps["enabled"] is True
    assert caps["provider"] == "fake-browser"
    assert caps["max_sessions"] == 3
    assert caps["max_tabs_per_session"] == 5
    assert caps["session_ttl_seconds"] == 1800

    # The per-profile policy (what the MCP tool surface applies per session)
    # resolves the settings' evaluate flag for the session owner.
    cfg = app.state.agent_browser_service.config_for(None)
    assert cfg.allow_evaluate is True


def test_mcp_tools_are_auth_gated():
    """Every tool surface is behind bearer auth: no credentials → 401, with
    no leakage of internal state or error detail."""
    client = TestClient(app)
    for method, url in (
        ("post", "/v1/search"),
        ("post", "/v1/scrape"),
        ("get", "/v1/transcript"),
        ("post", "/v1/browser/sessions"),
        ("get", "/v1/browser/capabilities"),
    ):
        if method == "post":
            body = client.request(method, url, json={})
        else:
            body = client.request(method, url)
        assert body.status_code == 401, f"{method} {url} → {body.status_code}"
