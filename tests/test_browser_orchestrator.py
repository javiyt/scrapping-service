"""Tests for the :class:`BrowserBackendOrchestrator`.

Covers the fallback policy (which errors trigger a fallback, which don't),
the usable-result heuristic (status / length / block signals), the bounded
metrics (labelled counters), and the "raise the primary's error when all
backends fail" semantics.

Uses scripted fake backends (duck-typed) so no real browser engine is
required.
"""

import pytest

from app.core.errors import (
    BrowserError,
    CamofoxUnavailableError,
    SecurityError,
    TimeoutError,
)
from app.metrics.prometheus import get_metrics
from app.scraper.browser_backends import BrowserBackendOrchestrator
from app.scraper.http_fetcher import FetchResult

# --------------------------------------------------------------------------- fakes


def _ok_result(
    html: str = "<html>" + "x" * 600 + "</html>",
    status: int = 200,
    backend: str = "primary",
) -> FetchResult:
    return FetchResult(
        html=html,
        status_code=status,
        final_url="https://example.com",
        backend_name=backend,
        backend_metadata={"via": backend},
    )


class _FakeBackend:
    """A scripted backend that either succeeds or raises."""

    def __init__(
        self,
        name: str,
        *,
        result: FetchResult | None = None,
        error: Exception | None = None,
        calls: list | None = None,
    ) -> None:
        self._name = name
        self._result = result
        self._error = error
        self.calls = calls if calls is not None else []
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    async def fetch(self, url: str, **kwargs) -> FetchResult:
        self.calls.append(url)
        if self._error is not None:
            raise self._error
        if self._result is not None:
            # Stamp this backend's name onto the result it returns.
            self._result.backend_name = self._name
            return self._result
        raise BrowserError("no result scripted")

    async def health(self) -> bool:
        return True

    def close(self) -> None:
        self._closed = True


def _reset_metrics() -> None:
    get_metrics().reset()


@pytest.fixture(autouse=True)
def _clean_metrics():
    _reset_metrics()
    yield
    _reset_metrics()


# --------------------------------------------------------------------------- fallback policy


async def test_primary_success_no_fallback():
    primary = _FakeBackend("botasaurus", result=_ok_result(backend="botasaurus"))
    fallback = _FakeBackend("camofox")
    orch = BrowserBackendOrchestrator([primary, fallback])
    result = await orch.fetch("https://example.com")
    assert result.backend_name == "botasaurus"
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 0  # fallback never tried


async def test_primary_timeout_falls_back_to_camofox():
    primary = _FakeBackend("botasaurus", error=TimeoutError("timed out"))
    fallback = _FakeBackend("camofox", result=_ok_result(backend="camofox"))
    orch = BrowserBackendOrchestrator([primary, fallback])
    result = await orch.fetch("https://example.com")
    assert result.backend_name == "camofox"
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1


async def test_primary_browser_error_falls_back():
    primary = _FakeBackend("botasaurus", error=BrowserError("driver failed"))
    fallback = _FakeBackend("camofox", result=_ok_result(backend="camofox"))
    orch = BrowserBackendOrchestrator([primary, fallback])
    result = await orch.fetch("https://example.com")
    assert result.backend_name == "camofox"


async def test_primary_camofox_error_falls_back():
    primary = _FakeBackend("botasaurus", error=CamofoxUnavailableError("unreachable"))
    fallback = _FakeBackend("camofox", result=_ok_result(backend="camofox"))
    orch = BrowserBackendOrchestrator([primary, fallback])
    result = await orch.fetch("https://example.com")
    assert result.backend_name == "camofox"


async def test_security_error_does_not_fall_back():
    primary = _FakeBackend("botasaurus", error=SecurityError("blocked private IP"))
    fallback = _FakeBackend("camofox")
    orch = BrowserBackendOrchestrator([primary, fallback])
    with pytest.raises(SecurityError):
        await orch.fetch("https://10.0.0.1")
    # The fallback must NOT be tried for a security/invalid-input failure.
    assert len(fallback.calls) == 0


async def test_validation_error_does_not_fall_back():
    from app.core.errors import ValidationError

    primary = _FakeBackend("botasaurus", error=ValidationError("bad url"))
    fallback = _FakeBackend("camofox")
    orch = BrowserBackendOrchestrator([primary, fallback])
    with pytest.raises(ValidationError):
        await orch.fetch("not a url")
    assert len(fallback.calls) == 0


async def test_import_error_falls_back():
    primary = _FakeBackend("botasaurus", error=ImportError("no driver"))
    fallback = _FakeBackend("camofox", result=_ok_result(backend="camofox"))
    orch = BrowserBackendOrchestrator([primary, fallback])
    result = await orch.fetch("https://example.com")
    assert result.backend_name == "camofox"


async def test_all_fail_raises_primary_error():
    primary = _FakeBackend("botasaurus", error=BrowserError("primary failed"))
    fallback = _FakeBackend("camofox", error=CamofoxUnavailableError("fallback failed"))
    orch = BrowserBackendOrchestrator([primary, fallback])
    with pytest.raises(BrowserError) as exc_info:
        await orch.fetch("https://example.com")
    # The *primary*'s error is raised (preserves historical semantics),
    # not the fallback's.
    assert "primary failed" in exc_info.value.message


async def test_usable_result_heuristic_rejects_block_page():
    # Primary returns a 403 block page → treated as unusable → fallback tried.
    primary = _FakeBackend(
        "botasaurus",
        result=_ok_result(html="<html>Just a moment</html>", status=403, backend="botasaurus"),
    )
    fallback = _FakeBackend("camofox", result=_ok_result(backend="camofox"))
    orch = BrowserBackendOrchestrator([primary, fallback])
    result = await orch.fetch("https://example.com")
    assert result.backend_name == "camofox"


async def test_usable_result_heuristic_rejects_empty_page():
    # Primary returns a 200 but with too little content → unusable → fallback.
    primary = _FakeBackend(
        "botasaurus",
        result=_ok_result(html="<html>tiny</html>", status=200, backend="botasaurus"),
    )
    fallback = _FakeBackend("camofox", result=_ok_result(backend="camofox"))
    orch = BrowserBackendOrchestrator([primary, fallback])
    result = await orch.fetch("https://example.com")
    assert result.backend_name == "camofox"


# --------------------------------------------------------------------------- metrics


async def test_metrics_recorded_on_fallback():
    primary = _FakeBackend("botasaurus", error=TimeoutError("timed out"))
    fallback = _FakeBackend("camofox", result=_ok_result(backend="camofox"))
    orch = BrowserBackendOrchestrator([primary, fallback])
    await orch.fetch("https://example.com")

    metrics = get_metrics()
    # Both backends were attempted.
    assert metrics.get("browser_backend_attempt_total", backend="botasaurus") == 1
    assert metrics.get("browser_backend_attempt_total", backend="camofox") == 1
    # Primary errored, fallback succeeded.
    assert metrics.get("browser_backend_error_total", backend="botasaurus") == 1
    assert metrics.get("browser_backend_success_total", backend="camofox") == 1
    # One fallback was used (labelled by the fallback backend's name).
    assert metrics.get("browser_backend_fallback_total", backend="camofox") == 1


async def test_metrics_no_fallback_on_primary_success():
    primary = _FakeBackend("botasaurus", result=_ok_result(backend="botasaurus"))
    fallback = _FakeBackend("camofox")
    orch = BrowserBackendOrchestrator([primary, fallback])
    await orch.fetch("https://example.com")

    metrics = get_metrics()
    assert metrics.get("browser_backend_attempt_total", backend="botasaurus") == 1
    assert metrics.get("browser_backend_attempt_total", backend="camofox") == 0
    assert metrics.get("browser_backend_success_total", backend="botasaurus") == 1
    assert metrics.get("browser_backend_fallback_total", backend="camofox") == 0


async def test_metrics_error_on_security_error_no_fallback():
    primary = _FakeBackend("botasaurus", error=SecurityError("blocked"))
    fallback = _FakeBackend("camofox")
    orch = BrowserBackendOrchestrator([primary, fallback])
    with pytest.raises(SecurityError):
        await orch.fetch("https://10.0.0.1")

    metrics = get_metrics()
    assert metrics.get("browser_backend_attempt_total", backend="botasaurus") == 1
    assert metrics.get("browser_backend_error_total", backend="botasaurus") == 1
    assert metrics.get("browser_backend_attempt_total", backend="camofox") == 0
    assert metrics.get("browser_backend_fallback_total", backend="camofox") == 0


# --------------------------------------------------------------------------- multi-fallback


async def test_two_fallbacks_tried_in_order():
    primary = _FakeBackend("botasaurus", error=TimeoutError("t"))
    f1 = _FakeBackend("camofox", error=CamofoxUnavailableError("down"))
    f2 = _FakeBackend("other", result=_ok_result(backend="other"))
    orch = BrowserBackendOrchestrator([primary, f1, f2])
    result = await orch.fetch("https://example.com")
    assert result.backend_name == "other"
    assert len(primary.calls) == 1
    assert len(f1.calls) == 1
    assert len(f2.calls) == 1


async def test_close_closes_all_backends():
    b1 = _FakeBackend("a")
    b2 = _FakeBackend("b")
    orch = BrowserBackendOrchestrator([b1, b2])
    orch.close()
    assert b1._closed
    assert b2._closed


async def test_name_is_primary_name():
    primary = _FakeBackend("botasaurus")
    fallback = _FakeBackend("camofox")
    orch = BrowserBackendOrchestrator([primary, fallback])
    assert orch.name == "botasaurus"
