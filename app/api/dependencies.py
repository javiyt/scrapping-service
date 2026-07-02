"""FastAPI dependency-injection providers for shared services."""

import logging
from collections.abc import AsyncGenerator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.cache.sqlite_cache import SqliteCache
from app.core.config import Settings
from app.jobs.service import JobService
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


# ------------------------------------------------------------------- cache


async def get_cache(request: Request) -> AsyncGenerator[SqliteCache, None]:
    """Yield the SQLite cache singleton."""
    cache = _get_cache(request)
    yield cache


# -------------------------------------------------------------- scraper


def _get_scraper(request: Request) -> ScraperService:
    """Return the scraper service singleton."""
    scraper: ScraperService | None = getattr(request.app.state, "scraper", None)
    if scraper is None:
        cache = _get_cache(request)
        settings = get_settings()
        scraper = ScraperService(settings=settings, cache=cache)
        request.app.state.scraper = scraper
    return scraper


async def get_scraper(request: Request) -> AsyncGenerator[ScraperService, None]:
    """Yield the scraper service singleton."""
    yield _get_scraper(request)


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
) -> None:
    """Require a valid API key unless ``api_key_required`` is ``False``.

    The ``/health`` endpoint is excluded from this check (it uses a separate
    router that does not include this dependency).
    """
    if not settings.server_api_key_required:
        return

    if credentials is None:
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

    if credentials.credentials != settings.scraper_api_key:
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
