"""Direct unit tests for FastAPI dependency functions."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request

from app.api.dependencies import verify_api_key
from app.core.config import Settings


class TestVerifyApiKey:
    """Direct tests for the verify_api_key dependency."""

    @pytest.mark.asyncio
    async def test_auth_disabled_returns_none(self):
        """When server_api_key_required is False, verify_api_key returns None."""
        request = AsyncMock(spec=Request)
        settings = Settings(server_api_key_required=False, _env_file=None)
        result = await verify_api_key(request, credentials=None, settings=settings)
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_auth_raises_401(self):
        """When auth is required and no credentials provided, raise 401."""
        request = AsyncMock(spec=Request)
        settings = Settings(_env_file=None)
        with pytest.raises(HTTPException) as excinfo:
            await verify_api_key(request, credentials=None, settings=settings)
        assert excinfo.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_key_raises_403(self):
        """When credentials are present but invalid, raise 403."""
        request = AsyncMock(spec=Request)
        credentials = MagicMock()
        credentials.credentials = "wrong-key"
        settings = Settings(_env_file=None)
        with pytest.raises(HTTPException) as excinfo:
            await verify_api_key(request, credentials=credentials, settings=settings)
        assert excinfo.value.status_code == 403
