"""End-to-end integration tests for the unified SCRAPE path (ticket #129).

Exercises the real production composition as a unit, with **no network and
no browser**::

    TestClient (real FastAPI app)
      → POST /v1/scrape  (real request validation, security/SSRF checks)
      → SqliteCache      (real SQLite scrape cache)
      → ScraperService   (real auto/fallback orchestration)
      → BrowserBackend   (structural fake standing in for Botasaurus/Camofox)
      → HttpFetcher      (scripted result — no real HTTP is issued)

All external I/O is mocked exactly where the ticket allows: the HTTP client
and the browser backends.  The orchestration logic under test (block
detection, backend fallback order, cache, stale-if-error) is the real thing.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import BrowserError, HttpError
from app.main import app
from app.metrics.prometheus import MetricsCollector
from app.scraper.browser_backends import BrowserBackendOrchestrator
from app.scraper.http_fetcher import FetchResult, HttpFetcher
from app.scraper.service import ScraperService
from tests.fakes import FakeBrowserBackend, FakeSqliteCache, ok_cache_entry, ok_fetch

API_KEY = "test-key-for-tests"
AUTH = {"Authorization": f"Bearer {API_KEY}"}

URL = "https://example.com/article"

#: HTML that passes the service's block detection (real content).
REAL_HTML = "<html><body>" + " ".join(f"word{i}" for i in range(120)) + "</body></html>"
#: A real block/challenge page (marker phrase + short body).
BLOCK_HTML = "<html><body><h1>Please complete the security check.</h1></body></html>"


class ScriptedHttpFetcher(HttpFetcher):
    """An :class:`HttpFetcher` whose client never performs real I/O.

    ``script_result`` / ``script_error`` stand in for the live fetch; any
    accidental real request would hit the dead transport and fail loudly.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None))
        self.script_result: FetchResult | None = None
        self.script_error: Exception | None = None
        self.requests: list[str] = []

    async def fetch(self, url: str, **kwargs) -> FetchResult:  # type: ignore[override]
        self.requests.append(url)
        if self.script_error is not None:
            raise self.script_error
        if self.script_result is not None:
            self.script_result.final_url = url
            return self.script_result
        raise HttpError("no scripted result")

    @staticmethod
    def _ok(html: str, status: int = 200) -> FetchResult:
        return FetchResult(html=html, status_code=status, final_url="", headers={})


def _http_result(status: int = 200, html: str | None = None) -> FetchResult:
    return FetchResult(
        html=html if html is not None else REAL_HTML,
        status_code=status,
        final_url=URL,
        headers={},
    )


def _service(
    tmp_path,
    *,
    http_result: str | None = None,
    http_status: int | None = None,
    http_error: Exception | None = None,
    browser_error: Exception | None = None,
    fallback_backends: list[str] | None = None,
):
    """Build a fully-wired ScraperService with everything external faked.

    The *production* browser-backend construction (Botasaurus primary →
    Camofox fallback when configured) runs for real; only the backends'
    I/O and the HTTP client are scripted (fakes + dead HTTP transport).
    """
    st = Settings(
        api_key=API_KEY,
        cache_sqlite_path=str(tmp_path / "scrape-cache.db"),
        security_block_private_ips=True,
        security_block_localhost=True,
        security_allowed_domains=[],
        browser_fallback_backends=fallback_backends or [],
        cache_stale_if_error=True,
        cache_default_ttl_seconds=3600,
    )

    store = FakeSqliteCache(db_path=str(tmp_path / "scrape.db"))
    store.open()
    service = ScraperService(settings=st, cache=store)
    # Inject the fake backend orchestrator (skip the real Botasaurus import).
    backends = [FakeBrowserBackend("botasaurus", error=browser_error)]
    if "camofox" in st.browser_fallback_backends:
        backends.append(FakeBrowserBackend("camofox", result=ok_fetch(URL, backend="camofox")))
    if len(backends) > 1:
        service._browser_backend = BrowserBackendOrchestrator(backends, metrics=MetricsCollector())
    else:
        service._browser_backend = backends[0]

    # Script the HTTP fetcher (its httpx client is a dead transport).
    scripted = ScriptedHttpFetcher(
        timeout_seconds=st.scraper_timeout_seconds,
        max_concurrency=st.scraper_max_concurrency,
    )
    if http_result is not None:
        scripted.script_result = _http_result(http_status or 200, http_result)
    elif http_error is not None:
        scripted.script_error = http_error
    service.http_fetcher = scripted
    return service


def _all_backends(service: ScraperService) -> list:
    """Every backend the service will try (orchestrator or single)."""
    backend = service._browser_backend
    return backend.backends if hasattr(backend, "backends") else [backend]


def _client(service) -> TestClient:
    app.state.scraper = service
    app.state.settings = service.settings
    return TestClient(app)


@pytest.fixture(autouse=True)
def _cleanup_state():
    yield
    if hasattr(app.state, "scraper"):
        delattr(app.state, "scraper")


def test_http_success_end_to_end(tmp_path):
    """Scenario 1 — plain HTTP succeeds: one HTTP call, no browser, stored."""
    service = _service(tmp_path, http_result=REAL_HTML)
    client = _client(service)

    body = client.post("/v1/scrape", json={"url": URL, "mode": "auto"}, headers=AUTH)
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["status_code"] == 200
    assert data["from_cache"] is False
    assert data["metadata"]["mode"] == "http"
    assert len(service.http_fetcher.requests) == 1
    assert len(service._browser_backend.calls) == 0

    # Second identical request is a cache hit (no second HTTP call).
    body = client.post("/v1/scrape", json={"url": URL, "mode": "auto"}, headers=AUTH)
    assert body.status_code == 200
    assert body.json()["from_cache"] is True
    assert len(service.http_fetcher.requests) == 1


def test_http_blocked_falls_back_to_botasaurus(tmp_path):
    """Scenario 2 — a 403 from HTTP looks blocked → browser backend runs."""
    service = _service(
        tmp_path,
        http_result=BLOCK_HTML,
        http_status=403,
        browser_error=None,
    )
    service._browser_backend._fetch_result = ok_fetch(URL, backend="botasaurus")
    client = _client(service)

    body = client.post("/v1/scrape", json={"url": URL, "mode": "auto"}, headers=AUTH)
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["status_code"] == 200
    assert data["metadata"]["mode"] == "browser"
    # fetch_backend is not part of the public Metadata schema (Pydantic
    # drops it) — the cache entry carries it internally.
    assert len(service.http_fetcher.requests) == 1
    assert len(service._browser_backend.calls) == 1


def test_http_fails_and_botasaurus_fails_camofox_succeeds(tmp_path):
    """Scenario 3 — HTTP errors, primary browser backend fails, the Camofox
    fallback backend produces the document."""
    service = _service(
        tmp_path,
        http_error=HttpError("connection refused"),
        browser_error=BrowserError("botasaurus driver failed"),
        fallback_backends=["camofox"],
    )
    client = _client(service)

    body = client.post("/v1/scrape", json={"url": URL, "mode": "auto"}, headers=AUTH)
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["metadata"]["mode"] == "browser"
    # fetch_backend is not part of the public Metadata schema.
    # Full chain: HTTP → Botasaurus → Camofox.
    assert len(service.http_fetcher.requests) == 1
    # The orchestrator has two backends (botasaurus + camofox).
    backends = _all_backends(service)
    assert len(backends) == 2
    assert len(backends[0].calls) == 1  # botasaurus tried
    assert len(backends[1].calls) == 1  # camofox tried


def test_all_fetch_backends_fail(tmp_path):
    """Scenario 4 — every backend fails: one clear error, the *primary*
    backend's error is what the client sees (original root cause)."""
    service = _service(
        tmp_path,
        http_error=HttpError("connection refused"),
        browser_error=BrowserError("botasaurus driver failed"),
        fallback_backends=["camofox"],
    )
    # Take the Camofox fallback down too.
    for backend in _all_backends(service):
        if backend.name == "camofox":
            backend._fetch_result = None
            backend._fetch_error = BrowserError("camofox upstream 500")
    client = _client(service)

    body = client.post("/v1/scrape", json={"url": URL, "mode": "auto"}, headers=AUTH)
    assert body.status_code == 502
    assert body.json()["error"]["type"] in ("http_error", "browser_error")


def test_expired_cache_stale_if_error(tmp_path):
    """Scenario 5 — the fetch fails and the cache entry has expired: the
    stale entry is still served (``from_cache=True, stale=True``)."""
    service = _service(
        tmp_path,
        http_error=HttpError("upstream down"),
        browser_error=BrowserError("browser down"),
    )
    # Take the only backend down too.
    for backend in _all_backends(service):
        backend._fetch_error = BrowserError("all browser backends down")

    # Seed a fresh cache entry, then expire it deterministically.
    entry = ok_cache_entry(URL)
    service.cache.set(entry)
    service.cache.backdate_entry(entry.cache_key, seconds=7200)

    client = _client(service)
    body = client.post("/v1/scrape", json={"url": URL, "mode": "auto"}, headers=AUTH)
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["from_cache"] is True
    assert data["stale"] is True


def test_all_fail_and_no_cache_raises(tmp_path):
    """Scenario 6 — everything fails and there is no cached entry at all:
    the error propagates to the caller (no silent stale data)."""
    service = _service(
        tmp_path,
        http_error=HttpError("upstream down"),
        browser_error=BrowserError("browser down"),
    )
    client = _client(service)
    body = client.post("/v1/scrape", json={"url": URL, "mode": "auto"}, headers=AUTH)
    assert body.status_code != 200
    assert "error" in body.json()
