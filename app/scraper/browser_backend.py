"""Browser-backend contract for the scraper service.

The service orchestrates caching, domain policy, and mode fallback; it must
not depend on Botasaurus (or any other browser engine) implementation
details.  This module defines the small structural contract every browser
backend satisfies:

* :class:`BrowserBackend` — a structural (duck-typing) protocol with a
  ``name``, an async :meth:`fetch`, an async :meth:`health`, and a
  synchronous :meth:`close`.  It is intentionally *not*
  ``runtime_checkable`` because ``fetch``'s signature is overloaded
  (per-request overrides are optional); the fake backends used in tests
  satisfy it structurally.

* :class:`BrowserBackendResult` — the backend-specific part of a fetch
  result.  Browser backends return a :class:`app.scraper.http_fetcher.FetchResult`
  with ``backend_name`` and ``backend_metadata`` set; those two fields are
  also gathered here so the service never has to reach into backend-
  specific objects.

The :mod:`app.scraper.browser_backends` package adapts the concrete
backends to this contract (see ``.botasaurus`` and ``.camofox``).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.scraper.http_fetcher import FetchResult


@runtime_checkable
class BrowserBackend(Protocol):
    """Structural contract every browser backend must satisfy.

    Implementations must be simple to fake in unit tests: any object with a
    ``name`` property plus ``fetch()``, ``health()`` and ``close()``
    satisfies this protocol (structural duck-typing).

    ``fetch`` raises a :class:`app.core.errors.ScraperError` (typically
    :class:`~app.core.errors.BrowserError` or
    :class:`~app.core.errors.CamofoxError`) on failure.  The service maps
    those failures onto the fallback policy; implementations never return
    a broken result for a failure.
    """

    @property
    def name(self) -> str:
        """Stable, unique backend identifier (e.g. ``"botasaurus"``)."""
        ...

    async def fetch(
        self,
        url: str,
        timeout_seconds: int | None = None,
        wait_until: str = "networkidle",
        wait_selector: str | None = None,
        scroll_config: dict[str, Any] | None = None,
        screenshot_path: str | None = None,
        proxy_url: str | None = None,
        cookies: list[dict] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> FetchResult:
        """Fetch *url* with browser rendering and return the rendered HTML."""
        ...

    async def health(self) -> bool:
        """Return ``True`` if the backend is currently usable.

        Implementations should make this cheap (a configuration check or a
        cheap probe) and never block the event loop.
        """
        ...

    def close(self) -> None:
        """Release backend resources.  Safe to call multiple times."""
        ...


class BrowserBackendResult:
    """A :class:`FetchResult` plus the backend-specific result metadata.

    The service reads :attr:`backend_name` and :attr:`backend_metadata` to
    (a) stamp ``metadata.fetch_backend`` on the cache entry and (b) record
    bounded backend metrics.  A :meth:`to_fetch_result` method projects this
    wrapper back to a plain :class:`FetchResult` with the backend name and
    metadata stamped on the result, so the service never has to reach into
    backend-specific objects.

    Backends return a :class:`FetchResult` directly (the contract's
    ``fetch()`` signature is ``-> FetchResult``).  This class is the *logical*
    view of the backend-specific part of that result and is used by the
    service when it needs to inspect or project the backend name/metadata.
    """

    def __init__(
        self,
        result: FetchResult,
        backend_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.result = result
        self.backend_name = backend_name or result.backend_name or "browser"
        if metadata is None:
            metadata = result.backend_metadata or {}
        self.backend_metadata = dict(metadata)

    @property
    def name(self) -> str:
        return self.backend_name

    def to_fetch_result(self) -> FetchResult:
        """Project this wrapper back to a :class:`FetchResult` with the
        backend name and metadata stamped on it.

        The returned object is a *new* :class:`FetchResult` sharing the
        original's fields; it is safe for the service to cache or serialise.
        """
        fr = self.result
        fr.backend_name = self.backend_name
        fr.backend_metadata = self.backend_metadata
        return fr


class ScrapeRequest:
    """A fully-resolved browser fetch request.

    Bundles every per-request override the service can pass to a
    :class:`BrowserBackend.fetch` call.  Using a dataclass keeps the call
    site readable and makes it easy to extend (e.g. adding a ``wait_timeout``
    or a per-request ``headers`` map) without changing the contract's method
    signature.

    Backends may accept a :class:`ScrapeRequest` *or* the individual keyword
    arguments — the contract's ``fetch()`` overload is permissive by design.
    """

    def __init__(
        self,
        url: str,
        timeout_seconds: int | None = None,
        wait_until: str = "networkidle",
        wait_selector: str | None = None,
        scroll_config: dict[str, Any] | None = None,
        screenshot_path: str | None = None,
        proxy_url: str | None = None,
        cookies: list[dict] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.wait_until = wait_until
        self.wait_selector = wait_selector
        self.scroll_config = dict(scroll_config) if scroll_config else {}
        self.screenshot_path = screenshot_path
        self.proxy_url = proxy_url
        self.cookies = cookies
        self.extra_headers = extra_headers

    def to_kwargs(self) -> dict[str, Any]:
        """Project this request into the keyword arguments a backend's
        ``fetch()`` accepts (excluding ``url``, which is a positional)."""
        return {
            "timeout_seconds": self.timeout_seconds,
            "wait_until": self.wait_until,
            "wait_selector": self.wait_selector,
            "scroll_config": self.scroll_config,
            "screenshot_path": self.screenshot_path,
            "proxy_url": self.proxy_url,
            "cookies": self.cookies,
            "extra_headers": self.extra_headers,
        }


def assert_contract(result: BrowserBackendResult) -> None:
    """Assert that *result* is a valid :class:`BrowserBackendResult`.

    Useful in tests and in the service's debug paths to catch a backend that
    returned a plain :class:`FetchResult` instead of a
    :class:`BrowserBackendResult`.  Raises :class:`AssertionError` when the
    object does not carry the backend-specific metadata.
    """
    assert isinstance(result, BrowserBackendResult), (
        f"Expected a BrowserBackendResult, got {type(result).__name__!r}"
    )


__all__ = ["BrowserBackend", "BrowserBackendResult", "ScrapeRequest", "assert_contract"]
