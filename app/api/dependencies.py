"""FastAPI dependency-injection providers for shared services."""

import logging
from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.models import AuthContext
from app.auth.resolver import ProfileResolver, get_profile_resolver
from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.jobs.service import JobService
from app.metrics.prometheus import get_metrics
from app.scraper.service import ScraperService

logger = logging.getLogger("scraper-api.deps")


# ------------------------------------------------------------------ helpers


def _get_cache(request: Request) -> SqliteCache:
    """Return the cache instance stored on app.state by the lifespan."""
    cache: SqliteCache | None = getattr(request.app.state, "cache", None)
    if cache is None:
        # Fallback: create on demand (shouldn't happen if lifespan runs).
        settings = get_settings()
        cache = SqliteCache(
            db_path=settings.cache_sqlite_path,
            max_size_mb=settings.cache_max_html_size_mb,
        )
        cache.open()
        request.app.state.cache = cache
    return cache


# ---------------------------------------------------------------- settings


def get_settings() -> Settings:
    """Return the application settings singleton."""
    return Settings.load()


# -------------------------------------------------------- profile resolver


def get_profile_resolver_dep() -> ProfileResolver:
    """Return the global ProfileResolver singleton."""
    return get_profile_resolver()


# ------------------------------------------------------------------- cache


async def get_cache(request: Request) -> AsyncGenerator[SqliteCache, None]:
    """Yield the SQLite cache singleton."""
    cache = _get_cache(request)
    yield cache


# ------------------------------------------------------------- auth context


def get_auth_context(request: Request) -> AuthContext | None:
    """Return the :class:`AuthContext` stored on the request by
    the :func:`verify_api_key` dependency.

    Returns ``None`` when auth is disabled.
    """
    return getattr(request.state, "auth_context", None)


def get_expose_profile(request: Request) -> bool:
    """Return whether the authenticated response should include the
    profile name."""
    resolver = get_profile_resolver()
    return resolver.expose_profile_in_response


# -------------------------------------------------------- effective settings


def get_effective_settings(
    request: Request,
) -> Settings:
    """Compute a per-request :class:`Settings` by merging the authenticated
    profile's overrides on top of the global settings.

    The global settings are never mutated.
    """
    resolver = get_profile_resolver()
    ctx: AuthContext | None = getattr(request.state, "auth_context", None)
    if ctx is not None:
        return resolver.effective_settings_for(ctx.profile_name)
    return resolver.effective_settings_for(None)


# -------------------------------------------------------------- scraper


def _get_scraper(
    request: Request,
    cache: SqliteCache,
    effective_settings: Settings,
) -> ScraperService:
    """Return a per-request :class:`ScraperService`, or the mock on
    ``app.state.scraper`` (used for test injection)."""
    # Test-injection fallback — if a mock is set on app.state, use it.
    mock: ScraperService | None = getattr(request.app.state, "scraper", None)
    if mock is not None:
        return mock
    return ScraperService(settings=effective_settings, cache=cache)


async def get_scraper(
    request: Request,
    cache: SqliteCache = Depends(get_cache),
    effective_settings: Settings = Depends(get_effective_settings),
) -> AsyncGenerator[ScraperService, None]:
    """Yield a per-request scraper built with profile-aware settings."""
    scraper = _get_scraper(request, cache, effective_settings)
    yield scraper


# ---------------------------------------------------------------- jobs


def get_job_service(request: Request) -> JobService:
    """Return the JobService singleton from app.state."""
    service: JobService | None = getattr(request.app.state, "job_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "internal_error",
                    "message": "Job service is not available (jobs may be disabled)",
                    "details": {},
                }
            },
        )
    return service


# ------------------------------------------------------------- auth scheme

bearer_scheme = HTTPBearer(auto_error=False)


async def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
    metrics=None,
) -> None:
    """Require a valid API key unless ``server_api_key_required`` is ``False``.

    On success, stores an :class:`AuthContext` on
    ``request.state.auth_context`` for downstream dependencies.

    The ``/health`` endpoint is excluded from this check (it uses a
    separate router that does not include this dependency).

    Uses constant-time comparison for API keys.
    """
    if metrics is None:
        metrics = get_metrics()

    if not settings.server_api_key_required:
        return

    if credentials is None:
        metrics.inc("auth_failures_total")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "type": "authentication_error",
                    "message": "Missing Authorization header. Use: Authorization: Bearer <API_KEY>",
                    "details": {},
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    resolver = get_profile_resolver()
    ctx = resolver.authenticate(credentials.credentials)

    if ctx is None:
        metrics.inc("auth_failures_total")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "type": "authentication_error",
                    "message": "Invalid API key",
                    "details": {},
                }
            },
        )

    # Store auth context on request state for downstream consumers.
    request.state.auth_context = ctx
    metrics.inc("auth_requests_total")
